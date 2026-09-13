"""Journalled deployments shared by people, agents and scheduled jobs."""

import contextlib
import datetime as dt
import json
import os
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import Error, fingerprint, lock, private_dir, read_json, timestamp, write_json
from .runtime import Runtime, image_only
from .source import Source


def in_window(cfg, now=None):
    window = cfg.get("window")
    if not window:
        return True
    now = now or dt.datetime.now(ZoneInfo(window["timezone"]))
    minute = now.astimezone(ZoneInfo(window["timezone"])).strftime("%H:%M")
    start, end = window["start"], window["end"]
    return start <= minute < end if start < end else minute >= start or minute < end


def hook(cfg, kind):
    argv = cfg["hooks"].get(kind)
    if not argv:
        return None
    env = os.environ.copy()
    env.update(MOORING_SERVICE=cfg["name"], MOORING_PHASE=kind)
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cfg["project_directory"],
                env=env,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            raise Error("hook_failed", f"Cannot start {kind} hook") from None
        try:
            status = process.wait(timeout=cfg["hook_timeout"])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise Error("hook_timeout", f"{kind} hook exceeded its deadline") from None
        if kind == "idle" and status == 75:
            return False
        if status:
            raise Error("hook_failed", f"{kind} hook failed with exit status {status}")
        if kind != "idle":
            return True
        if output.tell() > 4096:
            raise Error("idle_evidence", "Idle evidence exceeds 4 KiB")
        output.seek(0)
        try:
            evidence = json.load(output)
            age = timestamp() - float(evidence["observed_at"])
            if type(evidence["idle"]) is not bool or not -2 <= age <= 10:
                raise ValueError()
            return evidence["idle"]
        except (ValueError, KeyError, TypeError):
            raise Error(
                "idle_evidence", "Idle hook must provide fresh JSON idle/observed_at evidence"
            ) from None


class Deployment:
    def __init__(self, config, name):
        if name not in config["services"]:
            raise Error("unknown_service", "Service is not configured on this host")
        self.config, self.cfg = config, config["services"][name]
        self.root = private_dir(config["state_dir"] / "services" / name)
        self.operations = private_dir(self.root / "operations")
        self.state_path = self.root / "state.json"
        self.runtime = Runtime(self.cfg)
        self.source = Source(config["state_dir"], self.cfg)

    @contextlib.contextmanager
    def locks(self, maintenance=True):
        with contextlib.ExitStack() as stack:
            stack.enter_context(lock(self.config["state_dir"] / "host.lock"))
            if maintenance and self.config.get("maintenance_lock"):
                stack.enter_context(lock(Path(self.config["maintenance_lock"]).expanduser()))
            yield

    def state(self):
        return read_json(self.state_path, {})

    def save(self, state):
        write_json(self.state_path, state)

    def _plan(self):
        revision, source = self.source.fetch()
        rendered = self.runtime.render(source)
        state = self.state()
        applied = state.get("applied")
        digest = fingerprint(rendered)
        observed = self.runtime.observe()
        change = "initial"
        if applied:
            before = read_json(applied["rendered"])
            change = (
                "unchanged"
                if digest == applied["config_hash"]
                else ("image" if image_only(before, rendered, self.cfg["service"]) else "configuration")
            )
        plan = {
            "service": self.cfg["name"],
            "revision": revision,
            "change": change,
            "image": rendered["services"][self.cfg["service"]]["image"],
            "config_hash": digest,
            "applied_revision": applied.get("revision") if applied else None,
            "observed": observed,
            "pending_operation": state.get("active"),
            "in_window": in_window(self.cfg),
            "automatic": self.cfg.get("automatic", False),
        }
        return plan, rendered

    def check_applied(self, plan, state):
        previous = state.get("applied")
        if previous:
            current = plan["observed"]
            if not current or any(
                c["image_id"].removeprefix("sha256:") != previous["image_id"].removeprefix("sha256:")
                or c.get("config_hash") != previous["config_hash"]
                for c in current
            ):
                raise Error(
                    "runtime_drift",
                    "Runtime differs from the recorded applied image/configuration; inspect/recover first",
                )
        return previous

    def check_automatic_authority(self, plan, state):
        previous = self.check_applied(plan, state)
        if not previous:
            raise Error("manual_change", "Initial deployment requires explicit apply")
        if previous["authority_hash"] != fingerprint(self.cfg):
            raise Error("authority_changed", "Host policy/hooks changed; explicit apply is required")
        if plan["change"] not in {"unchanged", "image"}:
            raise Error("manual_change", "Non-image changes require explicit apply")
        return previous

    def plan(self):
        with self.locks(maintenance=False):
            return self._plan()[0]

    def status(self):
        state = self.state()
        applied = state.get("applied")
        return {
            "service": self.cfg["name"],
            "applied": applied,
            "pending_operation": state.get("active"),
            "failed_candidate": state.get("failed_candidate"),
            "observed": self.runtime.observe(),
        }

    def history(self):
        return sorted(
            [read_json(path) for path in self.operations.glob("*.json")],
            key=lambda item: item["started"],
            reverse=True,
        )

    def phase(self, op, phase):
        op["phase"], op["updated"] = phase, timestamp()
        write_json(self.operations / (op["id"] + ".json"), op)

    def wait_idle(self, automatic):
        if "idle" not in self.cfg["hooks"]:
            return
        deadline = time.monotonic() + self.cfg["idle_timeout"]
        quiet_since = None
        while True:
            if automatic and not in_window(self.cfg):
                raise Error("outside_window", "Maintenance window ended while waiting", retryable=True)
            quiet = hook(self.cfg, "idle")
            if quiet:
                quiet_since = quiet_since if quiet_since is not None else time.monotonic()
                if time.monotonic() - quiet_since >= self.cfg["idle_seconds"]:
                    return
            else:
                quiet_since = None
            if time.monotonic() >= deadline:
                raise Error("busy", "Service did not become idle before the deadline", retryable=True)
            time.sleep(min(1, max(0.05, self.cfg["idle_seconds"])))

    def finish(self, op, state, phase, applied=None):
        if applied is not None:
            state["applied"] = applied
        state.pop("active", None)
        self.phase(op, phase)
        self.save(state)

    def apply(self, *, automatic=False, expected_revision=None):
        with self.locks():
            state = self.state()
            if state.get("active"):
                raise Error("recovery_required", "An interrupted operation requires explicit recovery")
            if automatic and not self.cfg.get("automatic"):
                return {"service": self.cfg["name"], "result": "disabled"}
            if automatic and not in_window(self.cfg):
                return {"service": self.cfg["name"], "result": "outside_window"}
            plan, rendered = self._plan()
            if expected_revision and plan["revision"] != expected_revision:
                raise Error(
                    "source_changed", "Desired revision differs from the requested revision", retryable=True
                )
            previous = self.check_applied(plan, state)
            if automatic:
                self.check_automatic_authority(plan, state)
            if (
                plan["change"] == "unchanged"
                and previous
                and previous["authority_hash"] == fingerprint(self.cfg)
                and self.runtime.healthy(previous["image_id"], previous["config_hash"])
            ):
                return {**plan, "result": "unchanged"}
            if automatic:
                if plan["change"] != "image":
                    raise Error(
                        "manual_change", "Repairing an unchanged unhealthy deployment requires explicit apply"
                    )
                if state.get("failed_candidate") == plan["config_hash"]:
                    raise Error("quarantined", "This candidate failed previously; explicit apply is required")
            if not self.cfg.get("stateless") and "backup" not in self.cfg["hooks"]:
                raise Error("backup_required", "Stateful services require a successful backup hook")
            if (
                not self.cfg.get("allow_interruption")
                and not {"idle", "drain", "resume"} <= self.cfg["hooks"].keys()
            ):
                raise Error(
                    "drain_required", "Configure idle/drain/resume hooks or explicitly allow interruption"
                )
            approval = read_json(self.root / "approved-update.json", {})
            approved_digest = None
            pull_reference = plan["image"]
            if approval.get("image") == plan["image"] and approval.get("config_hash") == plan["config_hash"]:
                approved_digest = approval["digest"]
                repository, _ = plan["image"].rsplit(":", 1)
                pull_reference = repository + "@" + approved_digest
            identity = self.runtime.pull(pull_reference)
            opid = uuid.uuid4().hex
            release = private_dir(self.root / "releases" / opid)
            rendered_path, executable = release / "rendered.json", release / "runtime.yaml"
            write_json(rendered_path, rendered)
            self.runtime.freeze(rendered, identity["id"], executable)
            candidate = {
                "revision": plan["revision"],
                "image": plan["image"],
                "image_id": identity["id"],
                "approved_digest": approved_digest,
                "digests": identity["digests"],
                "config_hash": plan["config_hash"],
                "authority_hash": fingerprint(self.cfg),
                "rendered": str(rendered_path),
                "executable": str(executable),
                "operation": opid,
            }
            op = {
                "id": opid,
                "service": self.cfg["name"],
                "started": timestamp(),
                "automatic": automatic,
                "candidate": candidate,
                "previous": previous,
                "drain_attempted": False,
                "deployment_started": False,
            }
            self.phase(op, "prepared")
            state["active"] = opid
            self.save(state)
            try:
                self.phase(op, "backup")
                hook(self.cfg, "backup")
                self.phase(op, "waiting_idle")
                self.wait_idle(automatic)
                if automatic and not in_window(self.cfg):
                    raise Error(
                        "outside_window", "Maintenance window ended before deployment", retryable=True
                    )
                if "drain" in self.cfg["hooks"]:
                    op["drain_attempted"] = True
                    self.phase(op, "draining")
                    hook(self.cfg, "drain")
                    self.wait_idle(automatic)
                if automatic and not in_window(self.cfg):
                    raise Error(
                        "outside_window", "Maintenance window ended before deployment", retryable=True
                    )
                op["deployment_started"] = True
                self.phase(op, "deploying")
                self.runtime.deploy(executable)
                self.phase(op, "verifying")
                self.runtime.wait(identity["id"], plan["config_hash"])
                hook(self.cfg, "health")
                self.phase(op, "resuming")
                hook(self.cfg, "resume")
                state.pop("failed_candidate", None)
                self.finish(op, state, "succeeded", candidate)
                return {
                    "service": self.cfg["name"],
                    "result": "deployed",
                    "operation": opid,
                    "revision": plan["revision"],
                    "image": plan["image"],
                    "image_id": identity["id"],
                }
            except Exception as error:
                op["error"] = {
                    "code": getattr(error, "code", "internal_error"),
                    "message": str(error) if isinstance(error, Error) else "Unexpected deployment failure",
                }
                if not op["deployment_started"]:
                    if op["drain_attempted"]:
                        try:
                            hook(self.cfg, "resume")
                        except Error:
                            self.phase(op, "recovery_required")
                            raise Error(
                                "recovery_required", "Resume failed; operator recovery is required"
                            ) from error
                    self.finish(op, state, "deferred" if getattr(error, "retryable", False) else "failed")
                else:
                    state["failed_candidate"] = plan["config_hash"]
                    self.save(state)
                    if previous and self.cfg.get("rollback_safe") and plan["change"] == "image":
                        try:
                            self._restore(op, state, previous)
                        except Error:
                            self.phase(op, "recovery_required")
                            raise Error(
                                "recovery_required", "Deployment and automatic rollback failed"
                            ) from error
                    else:
                        self.phase(op, "recovery_required")
                raise

    def _restore(self, op, state, previous):
        self.phase(op, "rolling_back")
        self.runtime.image(previous["image_id"])
        self.runtime.deploy(previous["executable"])
        self.runtime.wait(previous["image_id"], previous["config_hash"])
        hook(self.cfg, "health")
        hook(self.cfg, "resume")
        self.finish(op, state, "rolled_back", previous)

    def recover(self, mode):
        with self.locks():
            state = self.state()
            if not state.get("active"):
                raise Error("no_pending_operation", "There is no interrupted operation to recover")
            op = read_json(self.operations / (state["active"] + ".json"))
            if op["candidate"]["authority_hash"] != fingerprint(self.cfg):
                raise Error("authority_changed", "Restore the operation's host configuration before recovery")
            if mode == "accept":
                if not op["deployment_started"]:
                    raise Error(
                        "deployment_not_started", "The candidate was not deployed; cancel with rollback mode"
                    )
                self.runtime.wait(op["candidate"]["image_id"], op["candidate"]["config_hash"])
                hook(self.cfg, "health")
                hook(self.cfg, "resume")
                state.pop("failed_candidate", None)
                self.finish(op, state, "recovered", op["candidate"])
            elif not op["deployment_started"]:
                if op["drain_attempted"]:
                    hook(self.cfg, "resume")
                self.finish(op, state, "cancelled")
            else:
                if not self.cfg.get("rollback_safe") or not op.get("previous"):
                    raise Error(
                        "unsafe_rollback", "A known previous deployment and rollback_safe policy are required"
                    )
                self._restore(op, state, op["previous"])
            return {"service": self.cfg["name"], "result": op["phase"], "operation": op["id"]}
