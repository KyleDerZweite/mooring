"""One explicitly manual image update, with a durable publication receipt."""

import copy
import json

from .common import Error, fingerprint, read_json, run, write_json
from .updates import eligible_tags, split_image, update_policy


def immediate_update(deployment, *, version=None, expected_revision=None):
    receipt_path = deployment.root / "manual-update.json"
    progress = {"service": deployment.cfg["name"], "phase": "preflight", "published_revision": None}
    try:
        with deployment.locks():
            state = deployment.state()
            receipt = read_json(receipt_path, {})
            if receipt:
                progress.update(receipt)
            if state.get("active"):
                raise Error("recovery_required", "An interrupted operation requires explicit recovery")
            plan, rendered = deployment._plan()
            previous = deployment.check_automatic_authority(plan, state)
            policy, _, _ = update_policy(deployment.cfg)
            if expected_revision and plan["revision"] != expected_revision:
                raise Error("source_changed", "Desired branch advanced; inspect the plan", retryable=True)
            if (
                receipt
                and (
                    plan["revision"] != receipt.get("revision")
                    or (version is not None and version != receipt["version"])
                )
                and deployment.retire_completed_update()
            ):
                if (version is None or version == receipt["version"]) and plan["config_hash"] == receipt[
                    "config_hash"
                ]:
                    if not deployment.runtime.healthy(previous["image_id"], previous["config_hash"]):
                        raise Error("unhealthy", "Repairing the current version requires explicit apply")
                    return {
                        **progress,
                        "phase": "completed",
                        "result": "unchanged",
                        "deployment": {**plan, "result": "unchanged"},
                    }
                receipt = {}
                progress = {
                    "service": deployment.cfg["name"],
                    "phase": "preflight",
                    "published_revision": None,
                }
            repository, current = split_image(previous["image"])
            tls = ["--tls-verify=false"] if deployment.cfg.get("insecure_registry") else []

            if receipt:
                if version is not None and version != receipt["version"]:
                    raise Error(
                        "pending_update", "Finish the retained manual target before selecting another version"
                    )
                if receipt["authority_hash"] != fingerprint(deployment.cfg):
                    raise Error(
                        "authority_changed", "Restore the retained update's host policy before retrying"
                    )
                if plan["revision"] not in {receipt["base_revision"], receipt.get("revision")}:
                    raise Error(
                        "source_changed", "Branch changed; inspect the retained manual update", retryable=True
                    )
                if plan["revision"] == receipt.get("revision"):
                    if plan["config_hash"] != receipt["config_hash"]:
                        raise Error(
                            "manual_change", "Retained update configuration changed; inspect before retrying"
                        )
                    receipt.update(phase="published", published_revision=receipt["revision"])
                elif plan["config_hash"] != receipt["base_config_hash"]:
                    raise Error("manual_change", "Base configuration changed; inspect before retrying")
            else:
                desired_repository, desired_tag = split_image(plan["image"])
                if desired_repository != repository:
                    raise Error("invalid_version", "Manual updates must stay in the applied image repository")
                if plan["change"] == "image":
                    # A pending Git image is the target, not a baseline for discovery.
                    if version is not None and version != desired_tag:
                        raise Error(
                            "pending_update",
                            "Desired Git image differs from the requested version; inspect the plan",
                        )
                    target_tag = desired_tag
                elif version == current:
                    if not deployment.runtime.healthy(previous["image_id"], previous["config_hash"]):
                        raise Error("unhealthy", "Repairing the current version requires explicit apply")
                    return {
                        **progress,
                        "phase": "completed",
                        "version": current,
                        "candidate": previous["image"],
                        "digest": previous.get("approved_digest"),
                        "revision": plan["revision"],
                        "result": "unchanged",
                    }
                else:
                    # Validate explicit versions before even querying the registry.
                    if version is not None and not eligible_tags(
                        current, [version], {**policy, "level": "major"}
                    ):
                        raise Error(
                            "invalid_version", "Select a higher stable tag in the current semver family"
                        )
                    tags = json.loads(run(["skopeo", "list-tags", *tls, "docker://" + repository]))["Tags"]
                    choices = eligible_tags(
                        current, tags, {**policy, "level": "major"} if version is not None else policy
                    )
                    if version is not None and version not in choices:
                        raise Error(
                            "version_unavailable",
                            "Requested stable tag is absent from the configured repository",
                        )
                    target_tag = version if version is not None else next(iter(choices), None)
                    if target_tag is None:
                        if not deployment.runtime.healthy(previous["image_id"], previous["config_hash"]):
                            raise Error("unhealthy", "Repairing the current version requires explicit apply")
                        return {
                            **progress,
                            "phase": "completed",
                            "revision": plan["revision"],
                            "version": current,
                            "candidate": previous["image"],
                            "digest": previous.get("approved_digest"),
                            "result": "up_to_date",
                        }
                if not eligible_tags(
                    current, [target_tag], {**policy, "level": "major"} if version is not None else policy
                ):
                    raise Error(
                        "invalid_version",
                        "Pending image must be a higher stable tag allowed by the selection policy",
                    )
                target = repository + ":" + target_tag
                desired = copy.deepcopy(rendered)
                desired["services"][deployment.cfg["service"]]["image"] = target
                config_hash = fingerprint(desired)
                approval = read_json(deployment.root / "approved-update.json", {})
                if approval.get("image") == target and approval.get("config_hash") == config_hash:
                    digest = approval["digest"]
                else:
                    digest = json.loads(run(["skopeo", "inspect", *tls, "docker://" + target]))["Digest"]
                receipt = {
                    "version": target_tag,
                    "candidate": target,
                    "digest": digest,
                    "config_hash": config_hash,
                    "authority_hash": fingerprint(deployment.cfg),
                    "base_revision": plan["revision"],
                    "base_config_hash": plan["config_hash"],
                    "base_image": plan["image"],
                    "phase": "selected",
                    "published_revision": None,
                }
                if plan["change"] == "image":
                    receipt.update(
                        revision=plan["revision"], phase="published", published_revision=plan["revision"]
                    )
                write_json(receipt_path, receipt)

            progress.update(receipt)
            if previous["config_hash"] == receipt["config_hash"] and not deployment.update_is_applied(
                receipt
            ):
                raise Error(
                    "digest_mismatch",
                    "Applied target lacks the retained approved digest; inspect status/history and explicitly apply only to accept the reviewed running state",
                )
            # Store approved bytes before any push, including retries after an ambiguous push.
            write_json(
                deployment.root / "approved-update.json",
                {
                    "image": receipt["candidate"],
                    "digest": receipt["digest"],
                    "config_hash": receipt["config_hash"],
                },
            )
            if receipt["phase"] != "published":
                if receipt.get("revision"):
                    deployment.source.publish_prepared(receipt["revision"], receipt["base_revision"])
                else:

                    def prepared(revision):
                        receipt["revision"] = revision
                        write_json(receipt_path, receipt)
                        progress.update(receipt)

                    deployment.source.commit_image(
                        receipt["base_revision"],
                        receipt["base_image"],
                        receipt["candidate"],
                        prepared=prepared,
                    )
                receipt.update(phase="published", published_revision=receipt["revision"])
            progress.update(receipt)
            write_json(receipt_path, receipt)
            result = deployment._apply(expected_revision=receipt["revision"], image_update=True)
            progress.update(phase="completed", result=result["result"], deployment=result)
            receipt_path.unlink()
            return progress
    except Exception as error:
        if not isinstance(error, Error):
            error = Error("internal_error", "Unexpected manual update failure; inspect status and history")
        active = deployment.state().get("active")
        progress["pending_operation"] = active
        if hasattr(error, "deployment"):
            progress["deployment"] = error.deployment
        elif active:
            operation = read_json(deployment.operations / (active + ".json"), {})
            progress["deployment"] = {
                "operation": active,
                "phase": operation.get("phase"),
                "failed_phase": operation.get("failed_phase"),
            }
        progress["recovery"] = (
            "Inspect status/history, stop any previous executor and hooks, then use recover SERVICE --mode accept or rollback"
            if active
            else "Inspect plan/status/history; retry update SERVICE --now with the same target. If source or policy changed, restore the retained intent or explicitly apply the reviewed Git revision"
        )
        if error.code == "digest_mismatch":
            progress["recovery"] = (
                "Inspect plan/status/history and verify the running image bytes. "
                "Only to accept that reviewed state, use apply SERVICE --revision SHA from plan; "
                "this supersedes the retained intent. A healthy unchanged state is not repulled; "
                "explicit apply repairs an unhealthy state"
            )
        error.data = progress
        raise error
