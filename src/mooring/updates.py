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
        if set(policy) - {
            "level",
            "tag_pattern",
            "minimum_age_seconds",
            "minimum_major_age_seconds",
            "enabled",
        }:
            raise Error("tag_policy", "Unknown update policy fields")
        age = policy.get("minimum_age_seconds", 43200)
        if type(age) not in (int, float) or age < 0 or (isinstance(age, float) and not math.isfinite(age)):
            raise Error("tag_policy", "minimum_age_seconds must be finite and non-negative")
        major_age = policy.get("minimum_major_age_seconds", age)
        if type(major_age) not in (int, float) or major_age < 0 or not math.isfinite(major_age):
            raise Error("tag_policy", "minimum_major_age_seconds must be finite and non-negative")
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
        expression = re.compile(policy.get("tag_pattern", r"^v?(?P<version>\d+\.\d+\.\d+)$"))
        current_major = Version(expression.fullmatch(current)["version"]).major
        newest = candidates[0]
        # While the newest major matures, a same-major fix can still be installed.
        same_major = next(
            (
                tag
                for tag in candidates
                if Version(expression.fullmatch(tag)["version"]).major == current_major
            ),
            None,
        )
        choices = [newest] + ([same_major] if same_major and same_major != newest else [])
        path = deployment.root / "first-seen.json"
        seen = read_json(path, {})
        next_seen = {}
        now = timestamp()
        waiting = None
        for tag in choices:
            target = repository + ":" + tag
            metadata = json.loads(run(["skopeo", "inspect", *tls, "docker://" + target]))
            digest = metadata["Digest"]
            key = target + "@" + digest
            first_observed = seen.get(key, now)
            next_seen[key] = first_observed
            required_age = (
                major_age if Version(expression.fullmatch(tag)["version"]).major != current_major else age
            )
            candidate_result = dict(
                candidate=target,
                digest=digest,
                first_observed=first_observed,
                minimum_age_seconds=required_age,
            )
            if now - first_observed >= required_age:
                result.update(candidate_result)
                break
            waiting = waiting or candidate_result
        else:
            write_json(path, next_seen)
            return {**result, **waiting, "result": "maturing"}
        write_json(path, next_seen)
        if waiting:
            result["maturing_candidate"] = waiting
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
