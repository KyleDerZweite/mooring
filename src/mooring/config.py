"""Trusted host configuration. Git may change Compose; it cannot change hook authority."""

import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import Error, private_dir

KEYS = {
    "repository",
    "ref",
    "compose_file",
    "project",
    "project_directory",
    "runtime",
    "service",
    "hooks",
    "stateless",
    "rollback_safe",
    "window",
    "automatic",
    "idle_seconds",
    "idle_timeout",
    "health_timeout",
    "hook_timeout",
    "update",
    "env_file",
    "podman_compose",
    "allow_interruption",
    "insecure_registry",
}
HOOKS = {"backup", "idle", "drain", "resume", "health"}


def load(path):
    try:
        data = json.loads(Path(path).expanduser().read_text())
        if set(data) - {"schema", "state_dir", "maintenance_lock", "services"} or data.get("schema") != 1:
            raise ValueError("Unknown configuration fields or schema")
        data["state_dir"] = private_dir(Path(data["state_dir"]).expanduser().resolve())
        if not isinstance(data["services"], dict):
            raise ValueError("Services must be a mapping")
        for name, cfg in data["services"].items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or set(cfg) - KEYS:
                raise ValueError("Invalid service name or unknown service fields")
            for field in ["repository", "compose_file", "project", "project_directory", "runtime", "service"]:
                if not isinstance(cfg.get(field), str) or not cfg[field]:
                    raise ValueError(f"Missing service field: {field}")
            if cfg["runtime"] not in {"docker", "podman"}:
                raise ValueError("Runtime must be docker or podman")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", cfg["project"]):
                raise ValueError("Explicit stable Compose project name required")
            source = Path(cfg["compose_file"])
            if source.is_absolute() or ".." in source.parts:
                raise ValueError("Compose path must stay inside the Git snapshot")
            cfg["project_directory"] = str(Path(cfg["project_directory"]).expanduser().resolve())
            if not Path(cfg["project_directory"]).is_dir():
                raise ValueError("Stable project_directory must already exist")
            cfg.setdefault("ref", "refs/heads/main")
            if not cfg["ref"].startswith("refs/heads/") or cfg["repository"].startswith("-"):
                raise ValueError("A branch ref and non-option repository URL are required")
            hooks = cfg.setdefault("hooks", {})
            if set(hooks) - HOOKS:
                raise ValueError("Unknown hook")
            for argv in hooks.values():
                if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                    raise ValueError("Hooks must be nonempty executable argument arrays")
            if bool(hooks.get("drain")) != bool(hooks.get("resume")):
                raise ValueError("Drain and resume hooks must be configured together")
            for key in ["stateless", "rollback_safe", "automatic", "allow_interruption", "insecure_registry"]:
                if key in cfg and type(cfg[key]) is not bool:
                    raise ValueError(f"{key} must be a boolean")
            window = cfg.get("window")
            if window:
                if set(window) != {"start", "end", "timezone"}:
                    raise ValueError("Window requires start, end and timezone")
                ZoneInfo(window["timezone"])
                for key in ["start", "end"]:
                    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", window[key]):
                        raise ValueError("Window times must be HH:MM")
                if window["start"] == window["end"]:
                    raise ValueError("Window start and end must differ; omit window for all day")
            for key, default in [
                ("idle_seconds", 120),
                ("idle_timeout", 600),
                ("health_timeout", 120),
                ("hook_timeout", 300),
            ]:
                cfg.setdefault(key, default)
                if type(cfg[key]) not in (int, float) or not math.isfinite(cfg[key]) or cfg[key] < 0:
                    raise ValueError("Timeouts must be non-negative numbers")
            cfg["name"] = name
        return data
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise Error("invalid_config", str(error)) from None
