import datetime as dt
import json
import subprocess
import sys
import time

import pytest

from mooring.common import Error, lock, run
from mooring.config import load
from mooring.deployment import hook, in_window
from mooring.runtime import image_only
from mooring.source import Source
from mooring.updates import eligible_tags


def test_versions_keep_family_and_policy():
    tags = ["1.2.4", "v1.2.5", "1.3.0", "2.0.0", "1.2.6-rc1", "latest"]
    assert eligible_tags("1.2.3", tags, {}) == ["1.2.4"]
    assert eligible_tags("1.2.3", tags, {"level": "minor"}) == ["1.3.0", "1.2.4"]
    assert eligible_tags("1.2.3", tags, {"level": "major"}) == ["2.0.0", "1.3.0", "1.2.4"]
    assert eligible_tags(
        "1.2.3-alpine", ["1.2.4-alpine", "1.2.5"], {"tag_pattern": r"^(?P<version>\d+\.\d+\.\d+)-alpine$"}
    ) == ["1.2.4-alpine"]


def test_effective_image_only_diff():
    before = {"services": {"app": {"image": "a:1", "environment": {"X": "1"}}}}
    after = json.loads(json.dumps(before))
    after["services"]["app"]["image"] = "a:2"
    assert image_only(before, after, "app")
    after["services"]["app"]["environment"]["X"] = "2"
    assert not image_only(before, after, "app")


def test_midnight_window():
    cfg = {"window": {"start": "23:00", "end": "01:00", "timezone": "Europe/Berlin"}}
    assert in_window(cfg, dt.datetime(2026, 9, 13, 22, tzinfo=dt.UTC))
    assert not in_window(cfg, dt.datetime(2026, 9, 13, 12, tzinfo=dt.UTC))


def test_lock_excludes_competing_process(tmp_path):
    path = tmp_path / "lock"
    with lock(path), pytest.raises(Error, match="holds the lock"), lock(path):
        pass


def test_timeout_stops_descendant(tmp_path):
    marker = tmp_path / "survived"
    child = f"import time; from pathlib import Path; time.sleep(0.5); Path({str(marker)!r}).touch()"
    parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(5)'
    with pytest.raises(Error) as failure:
        run([sys.executable, "-c", parent], timeout=0.15)
    assert failure.value.code == "command_timeout"
    time.sleep(0.6)
    assert not marker.exists()


@pytest.mark.parametrize(
    "evidence",
    [
        "{'idle':True,'observed_at':0}",
        "{'idle':'yes','observed_at':time.time()}",
        "{'idle':True,'observed_at':float('nan')}",
    ],
)
def test_idle_requires_fresh_boolean_evidence(tmp_path, evidence):
    cfg = {
        "name": "app",
        "project_directory": str(tmp_path),
        "hook_timeout": 2,
        "hooks": {"idle": [sys.executable, "-c", f"import json,time; print(json.dumps({evidence}))"]},
    }
    with pytest.raises(Error) as failure:
        hook(cfg, "idle")
    assert failure.value.code == "idle_evidence"


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args],
        cwd=root,
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


def test_git_commit_preserves_other_content_and_rejects_advanced_branch(tmp_path):
    author = tmp_path / "author"
    author.mkdir()
    git(author, "init", "-b", "main")
    path = author / "compose.yaml"
    path.write_text(
        "services:\n  app:\n    image: example:1.0.0 # keep this\n    environment:\n      KEEP: yes\n"
    )
    git(author, "add", ".")
    git(author, "commit", "-m", "Initial")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(author), str(remote))
    source = Source(
        tmp_path / "state",
        {
            "repository": str(remote),
            "ref": "refs/heads/main",
            "compose_file": "compose.yaml",
            "service": "app",
            "name": "app",
        },
    )
    revision, _ = source.fetch()
    changed = source.commit_image(revision, "example:1.0.0", "example:1.0.1")
    fetched, compose = source.fetch()
    assert changed == fetched and fetched != revision
    assert "example:1.0.1 # keep this" in compose.read_text()
    assert "KEEP: yes" in compose.read_text()
    with pytest.raises(Error) as failure:
        source.commit_image(revision, "example:1.0.0", "example:1.0.2")
    assert failure.value.code == "source_changed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("automatic", "false"),
        ("idle_timeout", float("nan")),
        ("window", {"start": "04:00", "end": "04:00", "timezone": "UTC"}),
    ],
)
def test_config_rejects_ambiguous_policy(tmp_path, field, value):
    service = {
        "repository": "repo",
        "compose_file": "compose.yaml",
        "project": "test",
        "project_directory": str(tmp_path),
        "runtime": "docker",
        "service": "app",
        field: value,
    }
    path = tmp_path / "host.json"
    path.write_text(
        json.dumps({"schema": 1, "state_dir": str(tmp_path / "state"), "services": {"app": service}})
    )
    with pytest.raises(Error):
        load(path)


def test_empty_host_is_ready_for_enrollment(tmp_path):
    path = tmp_path / "host.json"
    path.write_text(json.dumps({"schema": 1, "state_dir": str(tmp_path / "state"), "services": {}}))
    assert load(path)["services"] == {}


def test_portable_health_required_before_deployment(tmp_path, monkeypatch):
    from mooring.runtime import Runtime

    document = {"services": {"app": {"image": "example:1.0.0"}}}
    source = tmp_path / "compose.yaml"
    source.write_text(json.dumps(document))
    runtime = Runtime(
        {
            "runtime": "docker",
            "service": "app",
            "project": "test",
            "project_directory": str(tmp_path),
            "hooks": {},
        }
    )
    monkeypatch.setattr("mooring.runtime.run", lambda *args, **kwargs: json.dumps(document))
    with pytest.raises(Error) as failure:
        runtime.render(source)
    assert failure.value.code == "health_required"


def test_missing_runtime_health_is_not_healthy_when_required(monkeypatch):
    from mooring.runtime import Runtime

    runtime = Runtime({"runtime": "podman"})
    container = {
        "running": True,
        "image_id": "image",
        "config_hash": "config",
        "requires_health": True,
        "health": "none",
    }
    monkeypatch.setattr(runtime, "observe", lambda: [container])
    assert not runtime.healthy("image", "config")
    container["health"] = "healthy"
    assert runtime.healthy("image", "config")


def test_podman_monitor_outlives_updater_without_leaking_service_identity(tmp_path, monkeypatch):
    from mooring.runtime import Runtime

    monkeypatch.setenv("INVOCATION_ID", "parent-service")
    monkeypatch.setenv("NOTIFY_SOCKET", "/parent/socket")
    monkeypatch.setenv("KEEP_FOR_REGISTRY_AUTH", "present")
    (tmp_path / "compose.yaml").write_text('{"services":{"app":{"image":"example:1.0.0"}}}')
    calls = []
    monkeypatch.setattr("mooring.runtime.run", lambda argv, **kwargs: calls.append(kwargs))
    Runtime(
        {"runtime": "podman", "project": "test", "project_directory": str(tmp_path), "service": "app"}
    ).deploy(tmp_path / "compose.yaml")
    assert "INVOCATION_ID" not in calls[0]["env"]
    assert "NOTIFY_SOCKET" not in calls[0]["env"]
    assert calls[0]["env"]["KEEP_FOR_REGISTRY_AUTH"] == "present"
