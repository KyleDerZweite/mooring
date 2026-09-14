# Validation

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
