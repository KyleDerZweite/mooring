"""Semver tag discovery using Skopeo's registry/auth implementation."""

import copy
import json
import math
import re

from semantic_version import Version

from .common import Error, fingerprint, read_json, run, timestamp, write_json


def split_image(image):
    if "@" in image or ":" not in image.rsplit("/", 1)[-1]:
        raise Error("version_required", "Automatic updates require an explicit version tag")
    return image.rsplit(":", 1)


def eligible_tags(current, tags, policy):
    expression = re.compile(policy.get("tag_pattern", r"^v?(?P<version>\d+\.\d+\.\d+)$"))
    match = expression.fullmatch(current)
    if not match or "version" not in expression.groupindex:
        raise Error("tag_policy", "Current tag must match tag_pattern with a named version group")
    version = Version(match["version"])
    family = (current[: match.start("version")], current[match.end("version") :])
    level = policy.get("level", "patch")
    if level not in {"patch", "minor", "major"}:
        raise Error("tag_policy", "Update level must be patch, minor or major")
    result = []
    for tag in tags:
        other = expression.fullmatch(tag)
        if not other or family != (tag[: other.start("version")], tag[other.end("version") :]):
            continue
        try:
            candidate = Version(other["version"])
        except ValueError:
            continue
        if candidate.prerelease or candidate <= version:
            continue
        if level == "patch" and (candidate.major, candidate.minor) != (version.major, version.minor):
            continue
        if level == "minor" and candidate.major != version.major:
            continue
        result.append((candidate, tag))
    return [tag for _, tag in sorted(result, reverse=True)]


def update(deployment, *, commit=False, expected_revision=None, automatic=False):
    with deployment.locks(maintenance=False):
        if deployment.state().get("active"):
            raise Error(
                "recovery_required", "Resolve the interrupted deployment before publishing another version"
            )
        policy = deployment.cfg.get("update")
        if not isinstance(policy, dict):
            raise Error("update_policy_required", "Configure an explicit version update policy")
        if set(policy) - {"level", "tag_pattern", "minimum_age_seconds", "enabled"}:
            raise Error("tag_policy", "Unknown update policy fields")
        age = policy.get("minimum_age_seconds", 43200)
        if type(age) not in (int, float) or age < 0 or (isinstance(age, float) and not math.isfinite(age)):
            raise Error("tag_policy", "minimum_age_seconds must be finite and non-negative")
        plan, rendered = deployment._plan()
        if automatic:
            deployment.check_automatic_authority(plan, deployment.state())
            if plan["change"] == "image":
                return {
                    "service": deployment.cfg["name"],
                    "revision": plan["revision"],
                    "result": "pending_deployment",
                    "committed": False,
                }
        if expected_revision and plan["revision"] != expected_revision:
            raise Error("source_changed", "Desired branch advanced; plan the update again", retryable=True)
        repository, current = split_image(plan["image"])
        tls = ["--tls-verify=false"] if deployment.cfg.get("insecure_registry") else []
        tags = json.loads(run(["skopeo", "list-tags", *tls, "docker://" + repository]))["Tags"]
        candidates = eligible_tags(current, tags, policy)
        result = {
            "service": deployment.cfg["name"],
            "revision": plan["revision"],
            "current": plan["image"],
            "candidates": candidates[:20],
            "age_basis": "first_observed_digest",
            "committed": False,
        }
        if not candidates:
            return {**result, "result": "up_to_date"}
        # Inspect only the newest eligible tag. Wait for it to mature rather than
        # publishing an older candidate or querying every available manifest.
        target = repository + ":" + candidates[0]
        metadata = json.loads(run(["skopeo", "inspect", *tls, "docker://" + target]))
        digest = metadata["Digest"]
        path = deployment.root / "first-seen.json"
        seen = read_json(path, {})
        now = timestamp()
        key = target + "@" + digest
        # A publisher moving the tag to different bytes restarts its delay.
        first_observed = seen.get(key, now)
        write_json(path, {key: first_observed})
        result.update(candidate=target, digest=digest, first_observed=first_observed)
        if now - first_observed < age:
            return {**result, "result": "maturing"}
        result["result"] = "available"
        if commit:
            desired = copy.deepcopy(rendered)
            desired["services"][deployment.cfg["service"]]["image"] = target
            # Persist the approved bytes before publishing Git. A crash after push
            # must not let the subsequent deployment pull a newly moved tag.
            write_json(
                deployment.root / "approved-update.json",
                {
                    "image": target,
                    "digest": digest,
                    "config_hash": fingerprint(desired),
                },
            )
            result["revision"] = deployment.source.commit_image(plan["revision"], plan["image"], target)
            result.update(committed=True, result="committed")
        return result
