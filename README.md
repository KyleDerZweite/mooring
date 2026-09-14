# Mooring

Mooring deploys versioned Compose services from Git on Linux hosts running Docker
or rootless Podman. A local systemd timer checks each service's policy. The JSON
CLI is the same interface for people, AI agents and scheduled runs.

Git holds readable image tags and desired configuration. Each host records the
applied Git revision, rendered configuration hash, exact image ID and operation
history. Git or registry outages leave running containers alone; one host going
down does not stop another host's scheduler.

## Install and configure

Requires Python 3.11+, Git, Skopeo, and either Docker with Compose v2 or Podman
with `podman-compose`. Run under the account that owns the runtime. Docker socket
access grants host-level privileges. Use your existing Git/registry credential
stores; keep secrets and host hook scripts outside the deployment repository.

```sh
uv tool install .
mkdir -p ~/.config/mooring ~/.local/state/mooring ~/services/example
cp examples/host.json ~/.config/mooring/host.json
```

Edit the example's absolute paths, repository and service policy. The host
configuration is trusted local authority: automatic Git changes cannot install
hooks, change schedules or grant rollback permission. An empty `services` object
is valid for a host awaiting enrollment.

```sh
mooring services
mooring plan example
mooring apply example --revision <revision-from-plan>
mooring status example
mooring update example                 # discover; records first-observed time
mooring update example --commit        # push one image-tag commit; no deployment
mooring run                           # execute configured automatic policies
```

Install [the user systemd units](examples/systemd/) after the first successful
explicit deployment. `loginctl enable-linger USER` keeps user timers running
without an SSH session. Enroll services and enable automation deliberately; the
example starts disabled. [Operations](docs/operations.md) covers recovery and
host-maintenance locking. [The agent guide](docs/agents.md) covers machine callers.

## Deployment contract

A run fetches the configured branch, compares the selected service and shared
Compose resources, pulls the chosen image, journals the operation, backs up state,
waits for fresh idle evidence, drains traffic, checks idle again, recreates the
selected service, verifies image/configuration receipts and health, then resumes
traffic. Each service requires an explicit Compose healthcheck or application
health hook; image metadata alone is insufficient across runtimes. Stateless
services may omit backup. Schedule-only interruption requires
`allow_interruption: true`; a quiet clock time is not evidence of zero requests.

Automatic deployment permits only changes to the selected service's image.
Initial deployment, configuration changes and changed host policy require explicit
`apply`. Registry discovery selects semver tags within the configured patch,
minor or major range, preserves the tag family, and waits after first observation.
`minimum_major_age_seconds` overrides the delay when crossing a major version;
a same-major fix can proceed while a newer major matures.
It publishes an ordinary Git commit before deployment. A pending desired image
is attempted before discovering another update. Conflicting pushes fail safely.

Containers are created with readable image tags. Before creation, Mooring binds the
tag to the approved local image ID and checks the running identity afterwards.
Rollback rebinds the previous tag to the saved image ID and uses the saved configuration.
It runs automatically only for image-only changes with `rollback_safe: true`.
This declaration means the service owner has checked data compatibility. Image
rollback cannot undo a database migration. Failure without a safe rollback leaves
an operation requiring explicit recovery; a failed candidate is quarantined from
repeated automatic attempts.

Supported adoption is intentionally narrow: one Compose file and one selected
service per entry, published images, stable project names/directories, named
volumes and bind mounts. Compose dependencies are not restarted. Coordinated
multi-service stacks, builds, includes, configs/secrets references and unresolved
external env files require further implementation. Compose-rendered environment
values are stored privately; persistent volume/bind contents are not snapshotted
by Mooring itself. Application backup hooks own consistency.

CPA-specific idle/drain hooks have not been proven. This release does not infer
model-request activity from CPU usage or promise safe unattended upgrades of
CPA, databases or the gateway stack. Adopt each application with its own hooks
and recovery test. It does not change production applications merely by installing.

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
MOORING_INTEGRATION=1 uv run pytest -q tests/test_integration.py
```

Integration tests need both runtimes and Docker Hub access. They create uniquely
named test containers, a loopback registry and disposable volumes. They remove
only their own containers/volumes; base images/build cache can remain. See
[validation evidence](docs/validation.md) for actual versions and results.
