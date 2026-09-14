# Mooring

Update Docker and rootless Podman services from Git, using readable image tags
and a JSON CLI for people, agents and systemd timers.

Early-stage software under the [MIT license](LICENSE). Tested on both runtimes,
with limited production use and no independent security audit. Contributions are
welcome; support and response times are not guaranteed.

## How it works

Git owns desired Compose files; each host owns its schedule, hooks and deployment
records. Mooring discovers version tags, commits image-only updates, then backs up,
checks readiness, recreates the selected service and verifies health. Containers
keep readable tags; recorded image IDs preserve the approved bytes for rollback.

Initial deployments and configuration changes require explicit `apply`. Stateful
services require a backup hook. Use idle/drain hooks or explicitly allow interruption
during a quiet window. Image rollback requires declared data compatibility and
cannot undo database migrations. [Operations](docs/operations.md) covers policies,
hooks, supported Compose features and recovery.

## Get started

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), Git, Skopeo and Docker
Compose v2 or rootless Podman with `podman-compose`. Run as the runtime owner.

```sh
git clone https://github.com/KyleDerZweite/mooring.git
cd mooring
uv tool install .
mkdir -p ~/.config/mooring ~/services/example
cp examples/host.json ~/.config/mooring/host.json
```

Edit the host config to point at your **existing** deployment repository and its
Compose file. The [example](examples/compose.yaml) is a stateless web service;
configure backup and health hooks for your own workload. Keep credentials and
private host state outside Git.

```sh
mooring services
mooring plan example
mooring apply example --revision <revision-from-plan>
mooring status example
mooring update example             # discover a version without committing
mooring update example --commit    # commit the image tag without deploying
mooring run                        # run enabled automatic policies
```

For scheduling, follow [host setup](docs/operations.md): install the example user
units, enable lingering, and verify the timer after a successful explicit deployment.
For agent-driven deployment, read the [agent guide](docs/agents.md).

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

On an authorized test host with Docker and rootless Podman:

```sh
MOORING_INTEGRATION=1 uv run pytest -q tests/test_integration.py
```

Integration tests create disposable containers, volumes and a loopback registry.
See [validation](docs/validation.md) for tested versions and limits, and
[security](SECURITY.md) for private vulnerability reporting.
