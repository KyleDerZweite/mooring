# Agent operations

Use `mooring --config PATH services` to discover locally enrolled services. All
command results are JSON with `schema: 1`, `ok` and either `data` or `error`.
Errors expose stable `code`, a sanitized `message`, and `retryable`. Exit codes:
0 success, 75 retryable contention/deferral, 1 failure. A `run` containing any
per-service error exits 1; inspect each result. CLI syntax errors exit 2.

For an authorized change, edit and push the configured repository's authoritative Compose
file, run `plan NAME`, inspect `change`, `observed` and `pending_operation`, then
`apply NAME --revision SHA` with the planned revision. Report status/history
from the target host. A local repository edit is not deployment.

`update NAME --commit --revision SHA` publishes only a version change; `apply`
deploys it. `run` combines discovery and automatic deployment according to trusted
host policy. Explicit apply bypasses the schedule and quarantine, but retains
backup, idle and recovery safeguards. Host-policy changes require explicit apply
to adopt them, even when Compose is unchanged.

A pending operation blocks further updates. Inspect `history` and the runtime,
confirm the prior executor and hooks have stopped, and follow
[recovery](operations.md). Retry a fresh plan after `source_changed` or `locked`.
Do not retry a deployment blindly after an interrupted command.

For enrollment, inspect the application's data, health checks, traffic control,
Compose ownership and Git authentication on the target. Keep host configuration
and executable hooks local, store credentials in existing protected stores, and
establish a successful explicit deployment before enabling automation. Verify
lingering and the next timer run using [host setup](operations.md). Use the same
CLI as the timer. Stateful data requires a real backup hook.
