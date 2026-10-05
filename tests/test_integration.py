"""Opt-in destructive tests limited to uniquely named disposable projects/registry."""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from mooring.common import Error, lock
from mooring.config import load
from mooring.deployment import Deployment
from mooring.immediate import immediate_update
from mooring.updates import update

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MOORING_INTEGRATION") != "1",
        reason="set MOORING_INTEGRATION=1 on a disposable Docker + Podman test host",
    ),
]


def command(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"{args[0]} failed: {result.stderr[-3000:]} {result.stdout[-3000:]}"
    return result.stdout.strip()


@pytest.fixture(scope="session")
def registry(tmp_path_factory):
    root = tmp_path_factory.mktemp("registry")
    engine = os.environ.get("MOORING_TEST_REGISTRY_RUNTIME", "docker")
    assert engine in {"docker", "podman"}
    versions = ["1.0.0", "1.0.1", "1.0.2", "1.0.3-broken", "2.0.0"]
    name = "mooring-test-registry-" + uuid.uuid4().hex[:8]
    command(engine, "run", "-d", "--name", name, "-p", "127.0.0.1::5000", "docker.io/library/registry:2.8.3")
    try:
        port = command(engine, "port", name, "5000/tcp").rsplit(":", 1)[1]
        repo = f"localhost:{port}/mooring-test"
        command(engine, "pull", "docker.io/library/busybox:1.37.0")
        for version in versions:
            (root / "Dockerfile").write_text(
                f'FROM docker.io/library/busybox:1.37.0\nLABEL test.version="{version}"\nCMD ["sleep", "86400"]\n'
            )
            if not version.endswith("-broken"):
                with (root / "Dockerfile").open("a") as stream:
                    stream.write("RUN touch /healthy\n")
            command(engine, "build", "-q", "-t", f"{repo}:{version}", str(root))
            command(
                engine, "push", *(["--tls-verify=false"] if engine == "podman" else []), f"{repo}:{version}"
            )
        yield repo
    finally:
        command(engine, "rm", "-f", name)
        if "repo" in locals():
            for version in versions:
                subprocess.run([engine, "image", "rm", f"{repo}:{version}"], capture_output=True, check=False)


@pytest.fixture(params=["docker", "podman"])
def stack(tmp_path, registry, request):
    runtime = request.param
    author = tmp_path / "author"
    author.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    bind = project_dir / "bind"
    bind.mkdir()
    (bind / "sentinel").write_text("persistent-bind")

    def git(*args):
        return command("git", "-c", "user.name=Test", "-c", "user.email=test@localhost", *args, cwd=author)

    git("init", "-b", "main")
    project = "mooring-test-" + uuid.uuid4().hex[:10]
    compose = {
        "services": {
            "app": {
                "image": registry + ":1.0.0",
                "stop_grace_period": "1s",
                "healthcheck": {
                    "test": ["CMD", "test", "-f", "/healthy"],
                    "interval": "1s",
                    "timeout": "1s",
                    "retries": 1,
                },
                "volumes": ["data:/data", "./bind:/bind"],
            }
        },
        "volumes": {"data": {}},
    }
    path = author / "compose.yaml"
    path.write_text(json.dumps(compose))
    git("add", ".")
    git("commit", "-m", "Initial")
    remote = tmp_path / "origin.git"
    git("clone", "--bare", str(author), str(remote))
    git("remote", "add", "origin", str(remote))
    cfg = {
        "repository": str(remote),
        "compose_file": "compose.yaml",
        "project": project,
        "project_directory": str(project_dir),
        "runtime": runtime,
        "service": "app",
        "stateless": False,
        "rollback_safe": True,
        "automatic": True,
        "insecure_registry": True,
        "idle_seconds": 0,
        "idle_timeout": 0,
        "health_timeout": 5,
        "hook_timeout": 5,
        "hooks": {
            "backup": ["true"],
            "idle": [
                sys.executable,
                "-c",
                'import json,time; print(json.dumps({"idle":True,"observed_at":time.time()}))',
            ],
            "drain": ["true"],
            "resume": ["true"],
        },
        "update": {"enabled": True, "level": "patch", "minimum_age_seconds": 0},
    }
    config_path = tmp_path / "host.json"
    config_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "state_dir": str(tmp_path / "state"),
                "maintenance_lock": str(tmp_path / "maintenance.lock"),
                "services": {"app": cfg},
            }
        )
    )
    deployment = Deployment(load(config_path), "app")

    def publish(version=None, environment=None):
        if version:
            compose["services"]["app"]["image"] = registry + ":" + version
        if environment is not None:
            compose["services"]["app"]["environment"] = environment
        path.write_text(json.dumps(compose))
        git("add", ".")
        git("commit", "-m", "Change")
        git("push", "origin", "main")

    try:
        yield deployment, publish, config_path, registry
    finally:
        ids = command(runtime, "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}").split()
        if ids:
            command(runtime, "rm", "-f", *(["--time", "1"] if runtime == "podman" else []), *ids)
        # Exactly this disposable project's volume, never a prune.
        subprocess.run([runtime, "volume", "rm", project + "_data"], capture_output=True, check=False)
        subprocess.run([runtime, "network", "rm", project + "_default"], capture_output=True, check=False)
        if runtime == "podman":
            subprocess.run([runtime, "pod", "rm", "pod_" + project], capture_output=True, check=False)
            for version in ["1.0.0", "1.0.1", "1.0.2", "1.0.3-broken", "2.0.0"]:
                subprocess.run(
                    [runtime, "image", "rm", registry + ":" + version], capture_output=True, check=False
                )


def expect_error(code, call):
    with pytest.raises(Error) as failure:
        call()
    assert failure.value.code == code


def test_deploy_update_persistence_and_manual_config(stack):
    d, publish, _, registry = stack
    assert d.plan()["change"] == "initial"
    expect_error("manual_change", lambda: d.apply(automatic=True))
    assert d.apply()["result"] == "deployed"
    first = d.runtime.observe()[0]["id"]
    actual = json.loads(command(d.cfg["runtime"], "inspect", first))[0]
    assert actual["Config"]["Image"].endswith(":1.0.0")
    command(d.cfg["runtime"], "exec", first, "sh", "-c", "echo volume-survives > /data/sentinel")
    assert d.apply()["result"] == "unchanged"
    assert d.runtime.observe()[0]["id"] == first
    result = update(d, commit=True)
    assert result["committed"] and result["candidate"] == registry + ":1.0.2"
    assert d.plan()["change"] == "image"
    assert d.apply(automatic=True)["result"] == "deployed"
    current = d.runtime.observe()[0]["id"]
    assert current != first
    assert command(d.cfg["runtime"], "exec", current, "cat", "/data/sentinel") == "volume-survives"
    assert command(d.cfg["runtime"], "exec", current, "cat", "/bind/sentinel") == "persistent-bind"
    # Rebase author's checkout onto the updater's published commit.
    command(
        "git", "-C", str(Path(d.cfg["repository"]).parent / "author"), "pull", "--rebase", "origin", "main"
    )
    publish("1.0.2", {"TEST": "changed"})
    expect_error("manual_change", lambda: d.apply(automatic=True))
    assert d.runtime.observe()[0]["id"] == current
    assert d.apply()["result"] == "deployed"
    assert d.status()["pending_operation"] is None


def test_preflight_failures_and_exact_rollback(stack):
    d, publish, _, _ = stack
    d.apply()
    original = d.runtime.observe()[0]["id"]
    old_image = d.state()["applied"]["image_id"]
    # A later pull moving the prior tag must not change the rollback bytes.
    d.runtime.pull(d.state()["applied"]["image"].rsplit(":", 1)[0] + ":1.0.2")
    command(
        d.cfg["runtime"],
        "tag",
        d.state()["applied"]["image"].rsplit(":", 1)[0] + ":1.0.2",
        d.state()["applied"]["image"],
    )
    publish("1.0.1")
    d.cfg["hooks"]["backup"] = ["false"]
    expect_error("hook_failed", lambda: d.apply())
    assert d.runtime.observe()[0]["id"] == original and not d.state().get("active")
    d.cfg["hooks"]["backup"] = ["true"]
    idle = d.cfg["hooks"]["idle"]
    d.cfg["hooks"]["idle"] = ["sh", "-c", "exit 75"]
    expect_error("busy", lambda: d.apply())
    assert d.runtime.observe()[0]["id"] == original
    d.cfg["hooks"]["idle"] = idle
    with lock(Path(d.config["maintenance_lock"])):
        expect_error("locked", lambda: d.apply())
    # Candidate health fails once, rollback then passes the same health hook.
    marker = Path(d.cfg["project_directory"]) / "health-once"
    d.cfg["hooks"]["health"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; p=Path({str(marker)!r}); existed=p.exists(); p.touch(); raise SystemExit(0 if existed else 1)",
    ]
    expect_error("hook_failed", lambda: d.apply())
    assert d.runtime.healthy(old_image)
    assert d.history()[0]["phase"] == "rolled_back"
    assert d.state()["failed_candidate"] and not d.state().get("active")
    del d.cfg["hooks"]["health"]
    expect_error("quarantined", lambda: d.apply(automatic=True))
    assert d.apply()["result"] == "deployed"


def test_killed_updater_requires_explicit_recovery(stack):
    d, publish, config_path, _ = stack
    d.apply()
    publish("1.0.1")
    raw = json.loads(config_path.read_text())
    marker = Path(d.cfg["project_directory"]) / "health-entered"
    # First health invocation blocks; explicit recovery sees marker and returns.
    raw["services"]["app"]["hook_timeout"] = 120
    raw["services"]["app"]["hooks"]["health"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; import time,os; p=Path({str(marker)!r}); existed=p.exists(); p.write_text(str(os.getpid())); time.sleep(0 if existed else 90)",
    ]
    config_path.write_text(json.dumps(raw))
    process = subprocess.Popen(
        [sys.executable, "-m", "mooring.cli", "--config", str(config_path), "apply", "app"],
        stdout=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 40
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.exists(), "Updater did not reach post-deployment health hook"
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        os.killpg(int(marker.read_text()), signal.SIGKILL)
        recovered = Deployment(load(config_path), "app")
        assert recovered.status()["pending_operation"]
        expect_error("recovery_required", lambda: recovered.apply())
        assert recovered.recover("accept")["result"] == "recovered"
        assert recovered.runtime.healthy(
            recovered.state()["applied"]["image_id"], recovered.state()["applied"]["config_hash"]
        )
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def test_runtime_unhealthy_image_rolls_back(stack):
    d, publish, _, _ = stack
    d.apply()
    previous = d.state()["applied"]
    publish("1.0.3-broken")
    expect_error("unhealthy", lambda: d.apply(automatic=True))
    assert d.history()[0]["phase"] == "rolled_back"
    assert d.runtime.healthy(previous["image_id"], previous["config_hash"])


def test_systemd_user_executes_same_cli(stack):
    d, _, config_path, _ = stack
    d.apply()
    output = command(
        "systemd-run",
        "--user",
        "--wait",
        "--pipe",
        "--collect",
        "--unit=" + d.cfg["project"],
        "--property=TimeoutStopSec=5",
        "--property=Environment=PATH=" + os.environ["PATH"],
        sys.executable,
        "-m",
        "mooring.cli",
        "--config",
        str(config_path),
        "run",
    )
    result = json.loads(output)
    assert result["ok"]
    assert any(item.get("result") == "deployed" for item in result["data"])
    assert any(item.get("update", {}).get("committed") for item in result["data"])
    assert d.runtime.healthy(d.state()["applied"]["image_id"], d.state()["applied"]["config_hash"])

    if d.cfg["runtime"] == "podman":
        info = json.loads(command("podman", "inspect", d.runtime.observe()[0]["id"]))[0]
        cgroup = Path(f"/proc/{info['State']['ConmonPid']}/cgroup").read_text()
        assert "libpod-conmon-" in cgroup
        assert d.cfg["project"] + ".service" not in cgroup


def test_sibling_image_change_does_not_recreate_selected_service(stack):
    d, _, _, registry = stack
    author = Path(d.cfg["repository"]).parent / "author"
    path = author / "compose.yaml"
    document = json.loads(path.read_text())
    document["services"]["sibling"] = {"image": registry + ":1.0.0"}
    document["services"]["app"]["depends_on"] = ["sibling"]

    def publish():
        path.write_text(json.dumps(document))
        command("git", "-C", str(author), "add", ".")
        command(
            "git",
            "-C",
            str(author),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "Sibling update",
        )
        command("git", "-C", str(author), "push", "origin", "main")

    publish()
    d.apply()
    original = d.runtime.observe()[0]["id"]
    document["services"]["sibling"]["image"] = registry + ":1.0.1"
    publish()
    assert d.apply(automatic=True)["result"] == "unchanged"
    assert d.runtime.observe()[0]["id"] == original


def test_immediate_cli_bypasses_window_age_and_preserves_data(stack):
    d, _, config_path, _ = stack
    raw = json.loads(config_path.read_text())
    # Keep the one-minute window twelve hours away from this invocation.
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    start = (now + dt.timedelta(hours=12)).strftime("%H:%M")
    end = (now + dt.timedelta(hours=12, minutes=1)).strftime("%H:%M")
    raw["services"]["app"]["window"] = {"start": start, "end": end, "timezone": "UTC"}
    raw["services"]["app"]["update"].update(minimum_age_seconds=86400, minimum_major_age_seconds=864000)
    config_path.write_text(json.dumps(raw))
    d = Deployment(load(config_path), "app")
    d.apply()
    original = d.runtime.observe()[0]["id"]
    command(d.cfg["runtime"], "exec", original, "sh", "-c", "echo retained > /data/sentinel")
    before_config = config_path.read_bytes()
    assert update(d)["result"] == "maturing"
    assert d.apply(automatic=True)["result"] == "outside_window"
    argv = [sys.executable, "-m", "mooring.cli", "--config", str(config_path), "update", "app", "--now"]
    result = json.loads(command(*argv))
    assert result["ok"] and result["data"]["version"] == "1.0.2"
    assert result["data"]["published_revision"] == d.state()["applied"]["revision"]
    assert result["data"]["digest"] == d.state()["applied"]["approved_digest"]
    current = d.runtime.observe()[0]["id"]
    assert current != original
    assert command(d.cfg["runtime"], "exec", current, "cat", "/data/sentinel") == "retained"
    assert command(d.cfg["runtime"], "exec", current, "cat", "/bind/sentinel") == "persistent-bind"
    assert json.loads(command(*argv, "--version", "1.0.2"))["data"]["result"] == "unchanged"
    assert d.runtime.observe()[0]["id"] == current
    assert json.loads(command(*argv, "--version", "2.0.0"))["data"]["version"] == "2.0.0"
    assert config_path.read_bytes() == before_config
    assert d.status()["pending_operation"] is None
    assert json.loads(command(sys.executable, "-m", "mooring.cli", "--config", str(config_path), "run"))["ok"]


def test_immediate_backup_failure_retries_target_then_automatic_continues(stack):
    d, _, _, registry = stack
    marker = Path(d.cfg["project_directory"]) / "allow-backup"
    d.cfg["hooks"]["backup"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; raise SystemExit(0 if Path({str(marker)!r}).exists() else 1)",
    ]
    marker.touch()
    d.apply()
    marker.unlink()
    original = d.runtime.observe()[0]["id"]
    with pytest.raises(Error) as failure:
        immediate_update(d, version="1.0.1")
    assert failure.value.code == "hook_failed"
    revision = failure.value.data["published_revision"]
    assert revision and failure.value.data["phase"] == "published"
    assert d.runtime.observe()[0]["id"] == original
    assert not d.state().get("active")
    marker.touch()
    result = immediate_update(d)
    assert result["version"] == "1.0.1" and result["published_revision"] == revision
    assert d.state()["applied"]["image"] == registry + ":1.0.1"
    assert update(d, commit=True, automatic=True)["candidate"] == registry + ":1.0.2"
    assert d.apply(automatic=True)["result"] == "deployed"
    assert d.runtime.healthy(d.state()["applied"]["image_id"], d.state()["applied"]["config_hash"])


def test_immediate_adopts_pending_image_and_rejects_policy_before_commit(stack):
    d, publish, _, _ = stack
    d.apply()
    publish("1.0.1")
    revision = d.plan()["revision"]
    assert immediate_update(d)["published_revision"] == revision
    assert d.plan()["revision"] == revision
    d.cfg["hooks"]["health"] = ["true"]
    expect_error("authority_changed", lambda: immediate_update(d))
    assert d.plan()["revision"] == revision
