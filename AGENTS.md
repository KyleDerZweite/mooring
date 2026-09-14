# Working on Mooring

Read [README.md](README.md) for the deployment contract and development commands.
Keep manual, agent and scheduled operations on the same JSON CLI.

For host setup, deployment or recovery, read [agent operations](docs/agents.md)
and the user's target-host record. Reuse the configured deployment repository;
Mooring does not require a separate repository per service. Git owns Compose;
trusted host configuration owns hooks, schedules and rollback permission.

For implementation changes, run the README's local tests and lint. Changes to
runtime adapters, deployment or recovery also require the integration suite on
an authorized test host with Docker and rootless Podman. Record actual results
and remaining limits in [validation](docs/validation.md).

A deployment is complete when target-host status shows the intended image,
passing health and no pending operation. Preserve unrelated containers and data.
