"""Two native runtime adapters behind one deployment interface."""

import copy
import io
import json
import os
import time
from pathlib import Path

from ruamel.yaml import YAML

from .common import Error, atomic_write, fingerprint, run


def yaml_text(value):
    stream = io.StringIO()
    YAML().dump(value, stream)
    return stream.getvalue()


def image_only(before, after, service):
    before, after = copy.deepcopy(before), copy.deepcopy(after)
    try:
        before["services"][service]["image"] = "<image>"
        after["services"][service]["image"] = "<image>"
    except KeyError:
        return False
    return before == after


def has_healthcheck(service):
    health = service.get("healthcheck") or {}
    test = health.get("test")
    return bool(test) and not health.get("disable") and test != ["NONE"] and test != "NONE"


class Runtime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.binary = cfg["runtime"]

    def compose(self, path):
        command = (
            ["docker", "compose"]
            if self.binary == "docker"
            else [self.cfg.get("podman_compose", "podman-compose")]
        )
        command += ["-p", self.cfg["project"]]
        if self.binary == "docker":
            command += ["--project-directory", self.cfg["project_directory"]]
        env_file = self.cfg.get("env_file")
        if env_file:
            command += ["--env-file", str(Path(env_file).expanduser().resolve())]
        return [*command, "-f", str(path)]

    def render(self, source):
        document = YAML(typ="safe").load(source.read_text())
        if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
            raise Error("invalid_compose", "Compose requires a services mapping")
        if any(key in document for key in ["include", "extends", "configs", "secrets"]):
            raise Error(
                "unsupported_compose",
                "Compose includes/configs/secrets require adoption support outside this release",
            )
        if self.cfg["service"] not in document["services"]:
            raise Error("missing_service", "Configured service is absent from Compose")
        for service in document["services"].values():
            if any(key in service for key in ["build", "extends", "develop"]):
                raise Error(
                    "unsupported_compose", "Use published images; build/extends/develop are unsupported"
                )
        # Entries own one independently deployed service. Dependencies are never
        # started here; omit siblings so their image updates cannot invalidate this
        # entry's configuration receipt.
        document["services"] = {self.cfg["service"]: document["services"][self.cfg["service"]]}
        document["services"][self.cfg["service"]].pop("depends_on", None)
        # Podman Compose bases relative mounts on the compose-file directory, not cwd.
        # A temporary file IN the stable project directory makes both runtimes agree.
        import tempfile

        fd, name = tempfile.mkstemp(
            prefix=".mooring-render-", suffix=".yaml", dir=self.cfg["project_directory"]
        )
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(yaml_text(document))
            output = run([*self.compose(name), "config"], cwd=self.cfg["project_directory"])
            rendered = YAML(typ="safe").load(output)
        finally:
            Path(name).unlink(missing_ok=True)
        rendered.pop("name", None)
        for service in rendered["services"].values():
            if service.get("env_file") or service.get("configs") or service.get("secrets"):
                raise Error(
                    "unsupported_compose",
                    "External config references must be resolved into the rendered configuration",
                )
        target = rendered["services"][self.cfg["service"]]
        image = target.get("image", "")
        if not image or "${" in image:
            raise Error("invalid_image", "Service requires a resolved image reference")
        if not has_healthcheck(target) and not self.cfg.get("hooks", {}).get("health"):
            raise Error(
                "health_required",
                "Define an explicit Compose healthcheck or a trusted application health hook",
            )
        # Normalize short bind mounts for Podman Compose; named volumes remain stable through -p.
        for service in rendered["services"].values():
            volumes = service.get("volumes", [])
            for i, mount in enumerate(volumes):
                if isinstance(mount, str) and ":" in mount:
                    src, suffix = mount.split(":", 1)
                    if src.startswith("."):
                        volumes[i] = str((Path(self.cfg["project_directory"]) / src).resolve()) + ":" + suffix
                elif isinstance(mount, dict) and mount.get("type") == "bind":
                    mount["source"] = str((Path(self.cfg["project_directory"]) / mount["source"]).resolve())
        return rendered

    def pull(self, image):
        args = [self.binary, "pull"]
        if self.binary == "podman" and self.cfg.get("insecure_registry"):
            args.append("--tls-verify=false")
        run([*args, image], timeout=600)
        return self.image(image)

    def image(self, reference):
        data = json.loads(run([self.binary, "image", "inspect", reference]))[0]
        identity = data.get("Id") or data.get("ID")
        if not identity:
            raise Error("image_identity", "Runtime did not report an image ID")
        return {
            "id": "sha256:" + identity.removeprefix("sha256:"),
            "digests": data.get("RepoDigests", []),
            "architecture": data.get("Architecture"),
            "os": data.get("Os"),
        }

    def observe(self):
        ids = run(
            [
                self.binary,
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={self.cfg['project']}",
                "--filter",
                f"label=com.docker.compose.service={self.cfg['service']}",
            ]
        ).split()
        if not ids:
            return []
        containers = json.loads(run([self.binary, "inspect", *ids]))
        return [
            {
                "id": c["Id"],
                "image_id": c["Image"],
                "config_hash": c.get("Config", {}).get("Labels", {}).get("io.mooring.config-hash"),
                "requires_health": c.get("Config", {}).get("Labels", {}).get("io.mooring.health-required")
                == "true",
                "running": c["State"]["Running"],
                "health": c["State"].get("Health", {}).get("Status", "none"),
            }
            for c in containers
        ]

    def healthy(self, identity, config_hash=None):
        containers = self.observe()
        return bool(containers) and all(
            c["running"]
            and (config_hash is None or c["config_hash"] == config_hash)
            and c["image_id"].removeprefix("sha256:") == identity.removeprefix("sha256:")
            and c["health"] in ({"healthy"} if c["requires_health"] else {"healthy", "none"})
            for c in containers
        )

    def freeze(self, rendered, identity, destination):
        executable = copy.deepcopy(rendered)
        target = executable["services"][self.cfg["service"]]
        labels = target.setdefault("labels", {})
        if isinstance(labels, list):
            labels = dict(item.split("=", 1) if "=" in item else (item, "") for item in labels)
            target["labels"] = labels
        labels["io.mooring.image-id"] = identity
        labels["io.mooring.config-hash"] = fingerprint(rendered)
        labels["io.mooring.health-required"] = "true" if has_healthcheck(target) else "false"
        atomic_write(destination, yaml_text(executable))

    def deploy(self, executable):
        target = YAML(typ="safe").load(Path(executable).read_text())["services"][self.cfg["service"]]
        identity = target.get("labels", {}).get("io.mooring.image-id")
        if identity:
            # Keep the human-readable reference at container creation, but bind it
            # to the approved local bytes. Rollback retags the saved identity too.
            run([self.binary, "tag", identity, target["image"]])
            if self.image(target["image"])["id"] != identity:
                raise Error("image_identity", "Local version tag differs from the approved image")
        env = os.environ.copy()
        if self.binary == "podman":
            # Podman 5.7 suppresses conmon's independent scope under INVOCATION_ID.
            # The container monitor must outlive this updater's systemd service.
            # libpod/oci_conmon_linux.go: moveConmonToCgroupAndSignal.
            env.pop("INVOCATION_ID", None)
            env.pop("NOTIFY_SOCKET", None)
        run(
            [
                *self.compose(executable),
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "--force-recreate",
                self.cfg["service"],
            ],
            cwd=self.cfg["project_directory"],
            timeout=600,
            env=env,
        )

    def wait(self, identity, config_hash=None):
        deadline = time.monotonic() + self.cfg["health_timeout"]
        while True:
            if self.healthy(identity, config_hash):
                return
            if time.monotonic() >= deadline:
                raise Error("unhealthy", "Selected image did not become running and healthy")
            time.sleep(0.5)

    @staticmethod
    def hash(rendered):
        return fingerprint(rendered)
