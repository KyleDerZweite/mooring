"""Regression checks for deployment identity and automated Git ordering."""

import json
from unittest.mock import Mock

import pytest

from mooring import cli
from mooring.common import Error, fingerprint, read_json, write_json
from mooring.deployment import Deployment
from mooring.updates import update


@pytest.fixture
def deployment(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = {
        "name": "web",
        "runtime": "docker",
        "project": "review-web",
        "service": "web",
        "project_directory": str(project),
        "repository": str(tmp_path / "source.git"),
        "compose_file": "compose.yaml",
        "ref": "refs/heads/main",
        "hooks": {},
        "automatic": True,
        "update": {"enabled": True},
        "health_timeout": 0,
    }
    config = {"state_dir": tmp_path / "state", "services": {"web": cfg}}
    instance = Deployment(config, "web")
    instance.runtime = Mock()
    return instance


def baseline(deployment):
    applied = {
        "image_id": "sha256:old",
        "config_hash": "old-config",
        "authority_hash": fingerprint(deployment.cfg),
    }
    state = {"applied": applied}
    plan = {
        "revision": "a" * 40,
        "change": "unchanged",
        "observed": [{"image_id": "old", "config_hash": "old-config"}],
    }
    return state, plan


def test_drift_in_configuration_is_detected_even_when_image_matches(deployment):
    state, plan = baseline(deployment)
    plan["observed"][0]["config_hash"] = "different-config"
    with pytest.raises(Error, match="Runtime differs") as caught:
        deployment.check_applied(plan, state)
    assert caught.value.code == "runtime_drift"


def test_automatic_discovery_cannot_commit_with_changed_authority(deployment, monkeypatch):
    state, plan = baseline(deployment)
    deployment.cfg["hooks"] = {"health": ["/changed/health"]}
    deployment.save(state)
    monkeypatch.setattr(deployment, "_plan", lambda: (plan, {}))
    discovery = Mock(side_effect=AssertionError("Registry must not be queried"))
    monkeypatch.setattr("mooring.updates.run", discovery)
    with pytest.raises(Error) as caught:
        update(deployment, commit=True, automatic=True)
    assert caught.value.code == "authority_changed"
    discovery.assert_not_called()


def test_pending_image_deploys_before_discovering_another_version(deployment, monkeypatch):
    state, plan = baseline(deployment)
    plan["change"] = "image"
    deployment.save(state)
    monkeypatch.setattr(deployment, "_plan", lambda: (plan, {}))
    discovery = Mock(side_effect=AssertionError("Registry must not be queried"))
    monkeypatch.setattr("mooring.updates.run", discovery)
    result = update(deployment, commit=True, automatic=True)
    assert result["result"] == "pending_deployment"
    assert result["committed"] is False
    discovery.assert_not_called()


def test_accept_rejects_candidate_that_was_never_deployed(deployment):
    candidate = {
        "image_id": "sha256:same",
        "config_hash": "new-config",
        "authority_hash": fingerprint(deployment.cfg),
    }
    deployment.save({"active": "pending"})
    write_json(
        deployment.operations / "pending.json",
        {"id": "pending", "candidate": candidate, "deployment_started": False},
    )
    with pytest.raises(Error) as caught:
        deployment.recover("accept")
    assert caught.value.code == "deployment_not_started"
    deployment.runtime.wait.assert_not_called()
    assert deployment.state()["active"] == "pending"


def test_accept_requires_candidate_configuration_identity(deployment):
    candidate = {
        "image_id": "sha256:same",
        "config_hash": "new-config",
        "authority_hash": fingerprint(deployment.cfg),
    }
    deployment.save({"active": "pending"})
    write_json(
        deployment.operations / "pending.json",
        {"id": "pending", "candidate": candidate, "deployment_started": True},
    )
    deployment.runtime.wait.side_effect = Error("unhealthy", "Old configuration still running")
    with pytest.raises(Error) as caught:
        deployment.recover("accept")
    assert caught.value.code == "unhealthy"
    deployment.runtime.wait.assert_called_once_with("sha256:same", "new-config")
    assert deployment.state()["active"] == "pending"


def test_discovery_failure_does_not_skip_existing_desired_deployment(monkeypatch, capsys):
    config = {"services": {"web": {"automatic": True, "update": {"enabled": True}}}}
    instance = Mock()
    instance.apply.return_value = {"result": "deployed"}
    monkeypatch.setattr(cli, "load", lambda _: config)
    monkeypatch.setattr(cli, "Deployment", lambda *_: instance)
    monkeypatch.setattr(cli, "update", Mock(side_effect=Error("registry_down", "Registry unavailable")))
    assert cli.main(["run"]) == 1
    instance.apply.assert_called_once_with(automatic=True)
    assert '"phase": "update"' in capsys.readouterr().out


def test_explicit_apply_can_adopt_changed_host_policy(deployment, monkeypatch):
    state, plan = baseline(deployment)
    deployment.save(state)
    deployment.cfg.update(stateless=True, allow_interruption=True)
    plan.update(image="example/web:1.0.0", config_hash="old-config")
    monkeypatch.setattr(deployment, "_plan", lambda: (plan, {"services": {"web": {"image": plan["image"]}}}))
    deployment.runtime.healthy.return_value = True
    deployment.runtime.pull.return_value = {"id": "sha256:old", "digests": []}
    result = deployment.apply()
    assert result["result"] == "deployed"
    deployment.runtime.deploy.assert_called_once()
    assert deployment.state()["applied"]["authority_hash"] == fingerprint(deployment.cfg)


def test_moving_tag_digest_restarts_maturity_delay(deployment, monkeypatch):
    deployment.cfg["update"]["minimum_age_seconds"] = 60
    plan = {"revision": "a" * 40, "image": "example/web:1.0.0"}
    monkeypatch.setattr(deployment, "_plan", lambda: (plan, {}))
    observed = {"now": 100, "digest": "sha256:first"}
    monkeypatch.setattr("mooring.updates.timestamp", lambda: observed["now"])

    def registry(argv):
        return json.dumps({"Tags": ["1.0.1"]} if "list-tags" in argv else {"Digest": observed["digest"]})

    monkeypatch.setattr("mooring.updates.run", registry)
    assert update(deployment)["result"] == "maturing"
    observed["now"] = 160
    assert update(deployment)["result"] == "available"
    observed.update(now=161, digest="sha256:second")
    moved = update(deployment)
    assert moved["result"] == "maturing"
    assert moved["first_observed"] == 161
    observed["now"] = 221
    assert update(deployment)["result"] == "available"
    # Moving back to a previously seen digest also starts a fresh delay.
    observed.update(now=222, digest="sha256:first")
    assert update(deployment)["result"] == "maturing"


@pytest.mark.parametrize("age", [float("nan"), float("inf"), float("-inf"), True, -1])
def test_maturity_rejects_nonfinite_or_invalid_age(deployment, age):
    deployment.cfg["update"]["minimum_age_seconds"] = age
    with pytest.raises(Error) as caught:
        update(deployment)
    assert caught.value.code == "tag_policy"


def test_approved_digest_is_persisted_before_git_push(deployment, monkeypatch):
    deployment.cfg["update"]["minimum_age_seconds"] = 0
    rendered = {"services": {"web": {"image": "example/web:1.0.0"}}}
    plan = {"revision": "a" * 40, "image": "example/web:1.0.0"}
    monkeypatch.setattr(deployment, "_plan", lambda: (plan, rendered))
    monkeypatch.setattr(
        "mooring.updates.run",
        lambda argv: json.dumps(
            {"Tags": ["1.0.1"]} if "list-tags" in argv else {"Digest": "sha256:approved"}
        ),
    )

    def failed_push(*args):
        assert read_json(deployment.root / "approved-update.json") == {
            "image": "example/web:1.0.1",
            "digest": "sha256:approved",
            "config_hash": fingerprint({"services": {"web": {"image": "example/web:1.0.1"}}}),
        }
        raise Error("command_failed", "Push failed")

    monkeypatch.setattr(deployment.source, "commit_image", failed_push)
    with pytest.raises(Error, match="Push failed"):
        update(deployment, commit=True)
    assert rendered["services"]["web"]["image"] == "example/web:1.0.0"


@pytest.mark.parametrize("matching", [True, False])
def test_apply_binds_matching_approval_to_digest(deployment, monkeypatch, matching):
    deployment.cfg.update(stateless=True, allow_interruption=True)
    state, plan = baseline(deployment)
    deployment.save(state)
    rendered = {"services": {"web": {"image": "example/web:1.0.1"}}}
    desired_hash = fingerprint(rendered)
    plan.update(change="image", image="example/web:1.0.1", config_hash=desired_hash)
    monkeypatch.setattr(deployment, "_plan", lambda: (plan, rendered))
    write_json(
        deployment.root / "approved-update.json",
        {
            "image": plan["image"],
            "digest": "sha256:approved",
            "config_hash": desired_hash if matching else "different-configuration",
        },
    )
    deployment.runtime.pull.return_value = {"id": "sha256:new", "digests": ["example/web@sha256:approved"]}
    result = deployment.apply(automatic=True)
    assert result["image"] == "example/web:1.0.1"
    deployment.runtime.pull.assert_called_once_with(
        "example/web@sha256:approved" if matching else "example/web:1.0.1"
    )
    applied = deployment.state()["applied"]
    assert applied["image"] == "example/web:1.0.1"
    assert applied["approved_digest"] == ("sha256:approved" if matching else None)
