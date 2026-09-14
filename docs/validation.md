# Validation, 2026-09-13

Tested on the authorized Ubuntu 26.04 dedicated host with Docker 29.1.3,
Docker Compose 2.40.3, rootless Podman 5.7.0, podman-compose 1.6.0,
Skopeo 1.21.0-dev and Python 3.14.4. The local Python 3.11 check also passed.
All 42 tests passed together on dedicated in 115.22 seconds: 32 local tests
and ten runtime tests, five scenarios on each engine. Lint and formatting passed.
All 32 local tests also passed under Python 3.11; wheel and source builds passed.

Runtime scenarios cover explicit initial deployment, Git version discovery/commit
and automatic application, persistent named-volume and bind contents, no-op
container identity, rejection of automatic configuration changes, backup/idle
failure gates, maintenance-lock exclusion, exact-image rollback, quarantine,
killed-updater recovery, failing Compose healthchecks, and the complete automatic
CLI path under the user systemd manager. Hook failure tests use synthetic local
hooks; they do not validate any production application's backup or drain logic.

Testing found and resolved two host setup problems: Ubuntu's Pasta AppArmor
profile denied Podman's stop signal, and the already-running user systemd manager
had not inherited Kyle's newly added Docker group. Fleet owns the narrow signal
rule and host evidence. The user manager was restarted after confirming that it
had no running application services. Root-held maintenance locking was separately
verified to exclude the user updater.

Systemd testing also caught Podman retaining conmon in the updater service
cgroup when `INVOCATION_ID` was inherited. The adapter now removes the service
identity/notification variables only from its Compose child, allowing Podman to
create its independent conmon scope. The systemd test confirms that scope exists
and the container remains healthy after updater completion. Normal updater
process cleanup stays active.

The original unhealthy fixture also exposed a transport limitation: its published
OCI configuration omitted Docker's image-healthcheck extension. Mooring now
requires an explicit Compose healthcheck or local functional health hook, and
verifies that required runtime health is actually reported. Portable Compose
checks exercise the unhealthy-image rollback on both engines.

The scheduler is installed on dedicated with an empty service inventory. Its
scheduled run returned status 0, and user lingering is enabled. Production VPS
services have not been enrolled or modified. No production database migration,
CPA traffic drain, coordinated gateway deployment or host-reboot recovery is
claimed by these tests. Those require application-specific adoption checks.

Run the commands in README to reproduce. Integration fixtures use loopback-only
registries and unique test object names. They remove their own containers,
volumes and networks; base image/build caches may remain.

On 2026-09-14, regression coverage was added for readable running image references,
rollback after a prior tag moves, independent sibling image changes, and a major
release delay that does not block immediate same-major fixes. VPS enrollment must
verify user lingering; an enabled user timer alone stops after logout.
The full Docker/Podman integration suite passed all 12 tests on the dedicated host
(118.83 seconds). Local unit checks passed 33 tests; lint and formatting passed.
