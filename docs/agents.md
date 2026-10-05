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

For an authorized immediate version update, use `update NAME --now` or
`update NAME --now --version TAG`. This single invocation publishes an image-only
commit and deploys its observed digest at that exact Git revision. `--revision SHA`
optionally guards the starting branch revision. `--now` and `--commit` are mutually
exclusive, and `--version` requires `--now`. Bare `update`, `update --commit` and
`run` keep their existing behavior.

Without a version, selection follows the configured semver level. An exact higher
stable tag can override that level for this invocation, but must keep the current
repository and semver family. The automatic window and both release-age delays
are bypassed. No host policy or schedule changes. Drift, an active operation,
unapplied host policy and non-image Compose changes stop publication. A pending
image-only Git change is adopted if it matches the requested target and selection
policy, before querying for newer tags. Same-version healthy targets remain
unchanged, even if the registry tag has moved.

The successful JSON `data` includes `version`, `candidate`, `digest`, `revision`,
`published_revision` and `phase`, plus `deployment` when deployment was attempted.
A no-op has no new publication and may have a null digest when its earlier explicit apply had no digest approval.
On failure, the usual `error` and exit code remain, with additional `data` carrying
the retained target and recovery guidance. `phase` is the last completed workflow
stage: `selected`, `published`, or `completed`. `preflight` means no target was
selected. `revision` can identify a prepared commit before push;
`published_revision` is only set after publication is confirmed. When deployment
has a journal entry, `deployment.failed_phase` and `deployment.phase` identify
the failed step and subsequent recovery or rollback outcome.

After a failure, inspect `plan`, `status` and `history`. Repeating `update --now`
resumes the retained target and approved digest, without another discovery or
image commit. A changed branch stops with `source_changed`; it never silently
deploys another revision. If an operation is pending, stop the prior executor and
hooks before `recover`. A successful accept clears the retained update. Rollback
keeps it available for a deliberate retry. To supersede a retained target after
reviewing a changed branch or host policy, explicitly `apply NAME --revision SHA`.
Successful explicit apply clears the old intent. A normal automatic deployment of
the retained revision also clears it. Failed candidates remain quarantined for
automatic apply; `--now` is an explicit retry of its retained target. A retained
receipt remains the digest approval for its matching image/configuration even if
a later failed publication overwrites the ordinary update approval. If an already
applied target has a different or unknown approved digest, retry stops with
`digest_mismatch`. Review status/history before explicitly applying to accept the
running state and supersede the old intent. A healthy unchanged state is not
repulled; explicit apply can recreate an unhealthy state using its existing
repair and recovery contract.

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
