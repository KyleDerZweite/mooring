# Working on Mooring

Read [README.md](README.md) for the deployment contract. For target-host actions,
read [agent operations](docs/agents.md) and that host's owning inventory record.
Preserve unrelated containers, data and repositories.

Keep the JSON CLI the shared interface for manual and automatic operations.
Trusted local host configuration owns hook authority. Git owns desired Compose
configuration. Keep those responsibilities separate.

Run local tests and lint for implementation changes. Changes to deployment,
recovery or runtime adapters also require the opt-in integration suite against
real Docker and rootless Podman on an authorized test host. Record tested versions
and unresolved limits in [validation](docs/validation.md).
