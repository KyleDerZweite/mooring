# Validation

The immediate manual update implementation was checked on October 5, 2026.
Source `c440c0cfbb1019fba53e6691043f1776546712aa` passed `uv sync --group dev`,
`uv run pytest` with 73 passed and 18 integration cases skipped, `uv run ruff
check .` and `uv run ruff format --check .` on Python 3.14.7. Regressions cover
exact and policy-based selection, authority and locks before publication, retained
commit/digest retries, Git races, partial-success JSON, recovery and completed
receipt retirement across automatic or manual successors.

A disposable source snapshot of `84fbc3a576171a973e587490755ee530b80229aa` ran
`uv sync --group dev` and `MOORING_INTEGRATION=1 uv run pytest -q
tests/test_integration.py` on the authorized Ubuntu test host, using the Docker
and rootless Podman versions listed below. All 18 test bodies passed in 191.51
seconds, including the six new immediate-update cases. All nine Podman teardowns
failed with exit 125 and `rootless netns: kill network process: permission denied`.
Docker cleanup passed. This is not a passing integration run. Final source changes
to completed-receipt retirement still require a complete integration rerun after
the host cleanup failure is resolved. No installed Mooring source, service or
schedule was changed by these tests.

An additional local rootless Podman 5.8.4 check used
`MOORING_TEST_REGISTRY_RUNTIME=podman MOORING_INTEGRATION=1 uv run pytest -q
tests/test_integration.py -k podman`. The existing podman-compose 1.5.0 rejected
the adapter's `--pull never` syntax, so all nine cases failed before initial
deployment. An isolated temporary podman-compose 1.6.0 environment allowed seven
cases to pass. Two bind-persistence assertions failed with permission denied on
the SELinux-enforcing host. The tests and host protection were not weakened.
Local disposable containers, volumes, networks and the temporary Python environment
were removed. These local attempts do not substitute for Docker coverage.

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
