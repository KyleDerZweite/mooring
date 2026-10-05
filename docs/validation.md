# Validation

The immediate manual update source `13b17357b9b963eff649ca130e4c4f31feaf86b7`
passed validation on October 5, 2026. `uv sync --group dev`, `uv run pytest`,
`uv run ruff check .` and `uv run ruff format --check .` passed locally on
Python 3.14.7: 83 tests passed and 20 opt-in integration cases were skipped.
Independent review also verified the retained-intent lifecycle and error contract,
with no remaining findings. [PR #2](https://github.com/KyleDerZweite/mooring/pull/2)
tracks GitHub CI on Python 3.11 and 3.14.

The same source snapshot passed all 20 real Docker and rootless Podman integration
cases in 206.14 seconds on the authorized Ubuntu 26.04 test host. The exact command
was `MOORING_INTEGRATION=1 uv run pytest -q tests/test_integration.py`, after
`uv sync --group dev` in an isolated temporary source directory. Runtime versions
were Docker 29.1.3, Docker Compose 2.40.3, rootless Podman 5.7.0, podman-compose
1.6.0, Skopeo 1.21.0-dev and Python 3.14.4. All teardowns passed; subsequent checks
found no test containers, volumes or networks in either engine. The installed
Mooring checkout remained clean. No production installation or update was performed.

Coverage includes immediate latest/exact selection outside the window and age
delay, persistent data, healthy no-op, backup-failure retry, automatic follow-up,
and retained digest approval after a later publication fails. Local regressions
cover Git/tag races, publication interruption, pre-commit authority and locks,
partial-success JSON, recovery and retirement of completed intents. Failed or
unproven targets remain bound to their selected bytes and revision.

An earlier host run passed all test bodies but failed Podman cleanup with
`rootless netns: kill network process: permission denied`. The host owner reloaded
the unchanged existing Pasta AppArmor profile without its cache; no rule was
expanded. The final run above includes successful cleanup after that reload.
Additional local Podman 5.8.4 diagnostics were incomplete: podman-compose 1.5.0
rejected the existing `--pull never` syntax, while temporary 1.6.0 tooling passed
seven cases with two bind-permission failures on the SELinux-enforcing host.
Local test resources and temporary tooling were removed. These diagnostics are
not evidence of full support for that local host configuration.

Version 0.1.1 was tested on September 14, 2026: 33 local tests and 12 Docker/Podman
integration tests passed. Integration runtime: 118.83 seconds. Lint and formatting
passed. GitHub CI also passed on Python 3.11 and 3.14.

The integration host used Ubuntu 26.04, Docker 29.1.3, Docker Compose 2.40.3,
rootless Podman 5.7.0, podman-compose 1.6.0, Skopeo 1.21.0-dev and Python 3.14.4.

Coverage includes initial deployment, automatic version commits and application,
persistent volumes/binds, unchanged-container identity, backup/idle failure gates,
maintenance locking, rollback after tag movement, quarantine, interrupted-operation
recovery, explicit health checks, readable running tags and independent sibling
updates. Unit tests cover major-version delays alongside immediate same-major fixes.
Systemd tests verify Podman's container monitor survives updater completion.

Version 0.1.0 passed 32 local and ten integration tests on September 13. Tests found
host setup issues with Pasta AppArmor signals, Docker group membership and Podman
monitor lifetime. Mooring clears service identity variables for Podman children;
host permissions remain part of enrollment. A later production enrollment also
showed that enabled user timers stop after logout unless lingering is enabled.

Integration hooks are fixtures, not proof of any application's backup or drain
logic. Limited production use includes two Docker services. Database migration
rollback, application traffic draining, coordinated stack upgrades and host-reboot
recovery require workload-specific verification. This is not a security audit.

Run the commands in [README](../README.md) to reproduce. Integration tests remove
their own containers, volumes and networks; base images and build caches may remain.
