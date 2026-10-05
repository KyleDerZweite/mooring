"""Manual updates retain authority, a single candidate and exact publication identity."""

import copy
import json
from unittest.mock import Mock

import pytest

from mooring import cli
from mooring.common import Error, fingerprint, lock, read_json, write_json
from mooring.deployment import Deployment
from mooring.immediate import immediate_update


@pytest.fixture
def manual(tmp_path, monkeypatch):
    cfg = {
        "name": "app",
        "runtime": "docker",
        "project": "manual-test",
        "service": "app",
        "repository": "test.git",
        "compose_file": "compose.yaml",
        "ref": "refs/heads/main",
        "project_directory": str(tmp_path),
        "hooks": {},
        "stateless": True,
        "allow_interruption": True,
        "automatic": True,
        "update": {
            "enabled": True,
            "level": "patch",
            "minimum_age_seconds": 86400,
            "minimum_major_age_seconds": 864000,
        },
        "window": {"start": "00:00", "end": "00:01", "timezone": "UTC"},
    }
    d = Deployment(
        {
            "state_dir": tmp_path / "state",
            "maintenance_lock": str(tmp_path / "maint.lock"),
            "services": {"app": cfg},
        },
        "app",
    )
    d.runtime = Mock()
    rendered = {"services": {"app": {"image": "example/app:1.0.0", "environment": {"KEEP": "yes"}}}}
    path = tmp_path / "applied.json"
    write_json(path, rendered)
    previous = {
        "image": "example/app:1.0.0",
        "image_id": "sha256:old",
        "config_hash": fingerprint(rendered),
        "authority_hash": fingerprint(cfg),
        "revision": "a" * 40,
        "rendered": str(path),
    }
    d.save({"applied": previous})
    desired = {
        "revision": "a" * 40,
        "rendered": rendered,
        "tags": ["1.0.1", "1.0.2", "2.0.0"],
        "digest": "sha256:chosen",
    }
    d.runtime.observe.side_effect = lambda: [
        {"image_id": d.state()["applied"]["image_id"], "config_hash": d.state()["applied"]["config_hash"]}
    ]
    d.runtime.healthy.return_value = True
    d.runtime.pull.return_value = {"id": "sha256:new", "digests": ["sha256:chosen"]}
    d.source.fetch = Mock(side_effect=lambda: (desired["revision"], None))
    d.runtime.render.side_effect = lambda _: copy.deepcopy(desired["rendered"])

    def commit(revision, image, target, *, prepared):
        assert revision == desired["revision"]
        assert image == desired["rendered"]["services"]["app"]["image"]
        prepared("b" * 40)
        desired["revision"] = "b" * 40
        desired["rendered"]["services"]["app"]["image"] = target
        return desired["revision"]

    d.source.commit_image = Mock(side_effect=commit)
    registry = Mock(
        side_effect=lambda argv: json.dumps(
            {"Tags": desired["tags"]} if "list-tags" in argv else {"Digest": desired["digest"]}
        )
    )
    monkeypatch.setattr("mooring.immediate.run", registry)
    return d, desired, registry


def test_now_selects_patch_and_pins_digest_without_changing_policy(manual):
    d, desired, _ = manual
    cfg = copy.deepcopy(d.cfg)
    result = immediate_update(d)
    assert result["version"] == "1.0.2"
    assert result["published_revision"] == "b" * 40
    assert result["phase"] == "completed"
    assert result["deployment"]["result"] == "deployed"
    d.runtime.pull.assert_called_once_with("example/app@sha256:chosen")
    assert d.cfg == cfg
    assert d.state()["applied"]["authority_hash"] == fingerprint(cfg)
    assert not d.state().get("active")
    assert not (d.root / "manual-update.json").exists()
    assert desired["rendered"]["services"]["app"]["environment"] == {"KEEP": "yes"}


def test_explicit_higher_major_overrides_only_selection_level(manual):
    d, _, _ = manual
    assert immediate_update(d, version="2.0.0")["version"] == "2.0.0"
    assert d.cfg["update"]["level"] == "patch"


@pytest.mark.parametrize(
    "version", ["", "0.9.0", "2.0.0-rc1", "v2.0.0", "other/app:2.0.0", "https://example/2.0.0"]
)
def test_invalid_selection_fails_before_registry_or_publication(manual, version):
    d, _, registry = manual
    with pytest.raises(Error) as caught:
        immediate_update(d, version=version)
    assert caught.value.code == "invalid_version"
    d.source.commit_image.assert_not_called()
    registry.assert_not_called()


def test_unavailable_exact_version_does_not_publish(manual):
    d, _, _ = manual
    with pytest.raises(Error) as caught:
        immediate_update(d, version="1.0.9")
    assert caught.value.code == "version_unavailable"
    d.source.commit_image.assert_not_called()


@pytest.mark.parametrize("guard", ["drift", "active", "policy", "configuration", "initial"])
def test_authority_guards_precede_publication(manual, guard):
    d, desired, registry = manual
    if guard == "drift":
        d.runtime.observe.side_effect = lambda: [{"image_id": "stranger", "config_hash": "other"}]
    elif guard == "active":
        state = d.state()
        state["active"] = "old-operation"
        d.save(state)
    elif guard == "policy":
        d.cfg["hooks"]["health"] = ["changed"]
    elif guard == "initial":
        d.save({})
        d.runtime.observe.side_effect = lambda: []
    else:
        desired["rendered"]["services"]["app"]["environment"]["KEEP"] = "changed"
    with pytest.raises(Error) as caught:
        immediate_update(d)
    assert (
        caught.value.code
        == {
            "drift": "runtime_drift",
            "active": "recovery_required",
            "policy": "authority_changed",
            "configuration": "manual_change",
            "initial": "manual_change",
        }[guard]
    )
    d.source.commit_image.assert_not_called()
    registry.assert_not_called()


@pytest.mark.parametrize("which", ["host.lock", "maintenance"])
def test_locks_are_acquired_before_publication(manual, which):
    d, _, registry = manual
    path = d.config["state_dir"] / which if which == "host.lock" else d.config["maintenance_lock"]
    with lock(path), pytest.raises(Error) as caught:
        immediate_update(d)
    assert caught.value.code == "locked"
    d.source.commit_image.assert_not_called()
    registry.assert_not_called()


def test_pending_image_is_selected_before_newer_registry_tag(manual):
    d, desired, registry = manual
    desired["rendered"]["services"]["app"]["image"] = "example/app:1.0.1"
    result = immediate_update(d)
    assert result["version"] == "1.0.1"
    assert result["published_revision"] == "a" * 40
    assert len(registry.call_args_list) == 1 and "inspect" in registry.call_args.args[0]
    d.source.commit_image.assert_not_called()


@pytest.mark.parametrize(
    "target,version",
    [
        ("other/app:1.0.1", None),
        ("example/app:0.9.0", None),
        ("example/app:2.0.0", None),
        ("example/app:1.0.1", "1.0.2"),
    ],
)
def test_pending_image_must_match_allowed_requested_target(manual, target, version):
    d, desired, _ = manual
    desired["rendered"]["services"]["app"]["image"] = target
    with pytest.raises(Error):
        immediate_update(d, version=version)
    d.source.commit_image.assert_not_called()
    d.runtime.pull.assert_not_called()


def test_pull_failure_retries_same_published_candidate_and_bytes(manual):
    d, desired, registry = manual
    d.runtime.pull.side_effect = Error("command_failed", "Pull failed")
    with pytest.raises(Error) as caught:
        immediate_update(d, version="1.0.1")
    assert caught.value.data["phase"] == "published"
    assert caught.value.data["published_revision"] == "b" * 40
    assert "retry" in caught.value.data["recovery"]
    desired.update(tags=["1.0.9"], digest="sha256:moved")
    registry.reset_mock()
    with pytest.raises(Error) as conflict:
        immediate_update(d, version="1.0.9")
    assert conflict.value.code == "pending_update"
    d.runtime.pull.side_effect = None
    result = immediate_update(d)
    assert result["version"] == "1.0.1"
    d.source.commit_image.assert_called_once()
    registry.assert_not_called()
    assert all(c.args == ("example/app@sha256:chosen",) for c in d.runtime.pull.call_args_list)


def test_crash_after_push_recognizes_prepared_revision(manual):
    d, _, registry = manual
    commit = d.source.commit_image.side_effect

    def interrupted(*args, **kwargs):
        commit(*args, **kwargs)
        raise KeyboardInterrupt()

    d.source.commit_image.side_effect = interrupted
    with pytest.raises(KeyboardInterrupt):
        immediate_update(d)
    assert read_json(d.root / "manual-update.json")["revision"] == "b" * 40
    registry.reset_mock()
    result = immediate_update(d)
    assert result["published_revision"] == "b" * 40
    d.source.commit_image.assert_called_once()
    registry.assert_not_called()


def test_branch_advance_after_publication_never_applies_other_revision(manual):
    d, desired, _ = manual
    commit = d.source.commit_image.side_effect

    def concurrent_push(*args, **kwargs):
        result = commit(*args, **kwargs)
        desired["revision"] = "c" * 40
        desired["rendered"]["services"]["app"]["image"] = "example/app:1.0.9"
        return result

    d.source.commit_image.side_effect = concurrent_push
    for _ in range(2):
        with pytest.raises(Error) as caught:
            immediate_update(d)
        assert caught.value.code == "source_changed"
        assert caught.value.data["published_revision"] == "b" * 40
    d.runtime.pull.assert_not_called()
    d.source.commit_image.assert_called_once()


def test_current_healthy_version_is_noop_even_if_registry_tag_moved(manual):
    d, _, registry = manual
    result = immediate_update(d, version="1.0.0")
    assert result["result"] == "unchanged"
    registry.assert_not_called()
    d.source.commit_image.assert_not_called()
    d.runtime.deploy.assert_not_called()


def test_already_deployed_receipt_finishes_without_recreation(manual):
    d, _, _ = manual
    original_apply = d._apply

    def interrupted(**kwargs):
        original_apply(**kwargs)
        raise KeyboardInterrupt()

    d._apply = interrupted
    with pytest.raises(KeyboardInterrupt):
        immediate_update(d)
    d._apply = original_apply
    assert immediate_update(d)["result"] == "unchanged"
    d.runtime.deploy.assert_called_once()


def test_cli_emits_partial_success_and_retryable_exit(manual, monkeypatch, capsys):
    d, _, _ = manual
    d.runtime.pull.side_effect = Error("temporary", "Retry pull", retryable=True)
    monkeypatch.setattr(cli, "load", lambda _: d.config)
    monkeypatch.setattr(cli, "Deployment", lambda *_: d)
    assert cli.main(["update", "app", "--now", "--version", "1.0.1"]) == 75
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == 1 and not result["ok"]
    assert result["error"]["code"] == "temporary"
    assert result["data"]["published_revision"] == "b" * 40
    assert result["data"]["phase"] == "published"


@pytest.mark.parametrize(
    "argv", [["update", "app", "--now", "--commit"], ["update", "app", "--version", "1.0.1"]]
)
def test_cli_rejects_ambiguous_modes(argv):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 2


def test_backup_failure_retains_revision_and_completed_phase(manual, monkeypatch):
    d, _, _ = manual
    monkeypatch.setattr("mooring.deployment.hook", Mock(side_effect=Error("hook_failed", "Backup failed")))
    with pytest.raises(Error) as caught:
        immediate_update(d)
    assert caught.value.data["phase"] == "published"
    assert caught.value.data["deployment"]["failed_phase"] == "backup"
    assert caught.value.data["deployment"]["phase"] == "failed"
    assert not d.state().get("active")
    d.runtime.deploy.assert_not_called()


def test_health_failure_requires_recovery_before_retry(manual):
    d, _, registry = manual
    d.runtime.wait.side_effect = Error("unhealthy", "Target failed health")
    with pytest.raises(Error) as caught:
        immediate_update(d)
    assert caught.value.data["pending_operation"]
    assert "recover SERVICE" in caught.value.data["recovery"]
    registry.reset_mock()
    with pytest.raises(Error) as retry:
        immediate_update(d)
    assert retry.value.code == "recovery_required"
    registry.assert_not_called()
    d.runtime.wait.side_effect = None
    assert d.recover("accept")["result"] == "recovered"
    assert not (d.root / "manual-update.json").exists()


def test_explicit_apply_can_supersede_failed_manual_intent(manual):
    d, desired, _ = manual
    d.runtime.pull.side_effect = Error("pull_failed", "Pull failed")
    with pytest.raises(Error):
        immediate_update(d)
    d.runtime.pull.side_effect = None
    desired["revision"] = "c" * 40
    desired["rendered"]["services"]["app"]["image"] = "example/app:1.0.9"
    assert d.apply(expected_revision="c" * 40)["result"] == "deployed"
    assert not (d.root / "manual-update.json").exists()


def test_timer_can_finish_retained_target_and_clear_receipt(manual, monkeypatch):
    d, _, _ = manual
    d.runtime.pull.side_effect = Error("pull_failed", "Pull failed")
    with pytest.raises(Error):
        immediate_update(d)
    d.runtime.pull.side_effect = None
    monkeypatch.setattr("mooring.deployment.in_window", lambda _: True)
    assert d.apply(automatic=True)["result"] == "deployed"
    assert not (d.root / "manual-update.json").exists()


def test_automatic_newer_deployment_retires_already_fulfilled_receipt(manual, monkeypatch):
    d, desired, _ = manual
    original_apply = d._apply

    def interrupted(**kwargs):
        original_apply(**kwargs)
        raise KeyboardInterrupt()

    d._apply = interrupted
    with pytest.raises(KeyboardInterrupt):
        immediate_update(d, version="1.0.1")
    d._apply = original_apply
    # Timer discovery has already published the next eligible image.
    desired["revision"] = "c" * 40
    desired["rendered"]["services"]["app"]["image"] = "example/app:1.0.2"
    monkeypatch.setattr("mooring.deployment.in_window", lambda _: True)
    assert d.apply(automatic=True)["result"] == "deployed"
    assert not (d.root / "manual-update.json").exists()
    assert immediate_update(d, version="1.0.2")["result"] == "unchanged"


def test_rollback_failure_preserves_operation_and_failed_phase(manual):
    d, _, _ = manual
    d.cfg["rollback_safe"] = True
    state = d.state()
    state["applied"]["authority_hash"] = fingerprint(d.cfg)
    d.save(state)
    d.runtime.wait.side_effect = Error("unhealthy", "Target failed health")
    d.runtime.image.side_effect = Error("image_missing", "Rollback image missing")
    with pytest.raises(Error) as caught:
        immediate_update(d)
    assert caught.value.code == "recovery_required"
    assert caught.value.data["deployment"] == {
        "operation": d.state()["active"],
        "phase": "recovery_required",
        "failed_phase": "verifying",
    }


def test_resume_failure_preserves_original_idle_phase(manual, monkeypatch):
    d, _, _ = manual
    d.cfg["hooks"] = {"drain": ["true"], "resume": ["false"]}
    state = d.state()
    state["applied"]["authority_hash"] = fingerprint(d.cfg)
    d.save(state)
    d.wait_idle = Mock(side_effect=[None, Error("busy", "Still busy", retryable=True)])

    def hook(cfg, phase):
        if phase == "resume":
            raise Error("hook_failed", "Resume failed")

    monkeypatch.setattr("mooring.deployment.hook", hook)
    with pytest.raises(Error) as caught:
        immediate_update(d)
    assert caught.value.code == "recovery_required"
    assert caught.value.data["deployment"]["failed_phase"] == "draining"
    assert caught.value.data["deployment"]["phase"] == "recovery_required"


def test_failed_automatic_successor_does_not_resurrect_fulfilled_intent(manual, monkeypatch, capsys):
    d, desired, _ = manual
    original_apply = d._apply

    def interrupted(**kwargs):
        original_apply(**kwargs)
        raise KeyboardInterrupt()

    d._apply = interrupted
    with pytest.raises(KeyboardInterrupt):
        immediate_update(d, version="1.0.1")
    d._apply = original_apply

    def timer_discovery(*args, **kwargs):
        desired["revision"] = "c" * 40
        desired["rendered"]["services"]["app"]["image"] = "example/app:1.0.2"
        return {"result": "committed", "revision": "c" * 40}

    monkeypatch.setattr(cli, "load", lambda _: d.config)
    monkeypatch.setattr(cli, "Deployment", lambda *_: d)
    monkeypatch.setattr(cli, "update", timer_discovery)
    monkeypatch.setattr(cli, "in_window", lambda _: True)
    monkeypatch.setattr("mooring.deployment.in_window", lambda _: True)
    d.runtime.pull.side_effect = Error("pull_failed", "Temporary pull failure")
    assert cli.main(["run"]) == 1
    assert json.loads(capsys.readouterr().out)["data"][-1]["error"]["code"] == "pull_failed"
    assert not (d.root / "manual-update.json").exists()
    d.runtime.pull.side_effect = None
    assert immediate_update(d)["version"] == "1.0.2"


def test_direct_manual_successor_retires_only_fulfilled_intent(manual):
    d, desired, _ = manual
    original_apply = d._apply

    def interrupted(**kwargs):
        original_apply(**kwargs)
        raise KeyboardInterrupt()

    d._apply = interrupted
    with pytest.raises(KeyboardInterrupt):
        immediate_update(d, version="1.0.1")
    d._apply = original_apply
    desired["revision"] = "c" * 40
    desired["rendered"]["services"]["app"]["image"] = "example/app:1.0.2"
    assert immediate_update(d)["published_revision"] == "c" * 40
    d.source.commit_image.assert_called_once()
    assert not (d.root / "manual-update.json").exists()


def test_unrelated_git_advance_completes_fulfilled_target_without_discovery(manual):
    d, desired, registry = manual
    original_apply = d._apply

    def interrupted(**kwargs):
        original_apply(**kwargs)
        raise KeyboardInterrupt()

    d._apply = interrupted
    with pytest.raises(KeyboardInterrupt):
        immediate_update(d, version="1.0.1")
    d._apply = original_apply
    desired["revision"] = "c" * 40
    # A docs/sibling-only Git commit leaves the selected service configuration alone.
    desired["tags"] = ["1.0.9"]
    registry.reset_mock()
    result = immediate_update(d)
    assert result["version"] == "1.0.1" and result["result"] == "unchanged"
    assert result["published_revision"] == "b" * 40
    registry.assert_not_called()
    d.runtime.deploy.assert_called_once()
    assert not (d.root / "manual-update.json").exists()
