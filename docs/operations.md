# Host operations

Install the example user units under `~/.config/systemd/user/`, then run
`systemctl --user daemon-reload` and `systemctl --user enable --now mooring.timer`.
Enable `loginctl enable-linger USER` so scheduling survives logout; verify
`loginctl show-user USER -p Linger` and `systemctl --user list-timers mooring.timer`.
The timer checks every five minutes; each service's IANA time zone/window decides
eligibility. There is no catch-up run outside the window. A service without a
window is eligible all day. Observe results with `journalctl --user -u mooring`.
Container startup after a host reboot remains the runtime/service owner's
responsibility. For rootless Podman, validate the installed user
`podman-restart.service` and a compatible `restart: always` policy, or own the
application lifecycle with dedicated systemd units. Mooring stops on unexpected
runtime drift; its updater is not a boot supervisor.

For Podman deployments, Mooring removes `INVOCATION_ID` and `NOTIFY_SOCKET`
from the Compose child environment so Podman puts its persistent container
monitor in a separate scope. The updater retains normal systemd process cleanup.

Systemd marks failed runs; outbound notifications are not configured by Mooring.

Each state directory has a nonblocking host lock. Configure `maintenance_lock`
with the existing OS-maintenance lock path when the runtime user can open it.
Both jobs must lock the same existing inode. Prefer a host-owned shared lock with
appropriate access over unrelated locks. A service's backup hook must not acquire
that same lock again: the caller already holds it. Full-host maintenance stays
separate from container updates.

Hook values are argv arrays, executed without a shell unless a shell is explicitly
configured. Their cwd is the stable project directory; `MOORING_SERVICE` and
`MOORING_PHASE` identify the call. Exit 0 means success. The idle hook may return
75 for busy, or emit `{"idle":true,"observed_at":UNIX_SECONDS}`. Timestamps must
be no more than ten seconds old (two seconds of forward clock tolerance). Idle
must remain true for `idle_seconds`; drain is followed by the same quiet check.
Backup/drain/resume/health output is discarded. Make drain/resume idempotent and
bound long application work inside hooks.

`allow_interruption: true` permits running without traffic hooks. It does not
remove configured hooks or the backup requirement. Set `stateless: true` only
when replacing a container cannot lose authoritative data. Health verifies every
selected container is running, has the intended image/config receipt, and passes
its explicitly declared Compose healthcheck; an application health hook can
provide the functional check instead. Mooring rejects services lacking either.
Image-inherited checks are insufficient: publishing/converting OCI images can
drop Docker-specific health metadata. A required runtime healthcheck must actually
report healthy, not merely be absent.

`status` reports applied and observed state; `plan` additionally fetches desired
Git state. Mooring records a configuration-hash label as the deployment receipt.
This is not a forensic comparison of every live runtime property: external tools
can change a container while preserving labels. Keep Mooring as the deployment
owner. Bind-file contents and external secrets are the service owner's concern.
Automatic Git commits require a literal versioned `image:` field. Tags matching
a semver family are candidates; prereleases are excluded. Delay is measured from
first observation of the newest eligible tag and its digest, not publication
time. A changed digest restarts the delay. `minimum_major_age_seconds` overrides
`minimum_age_seconds` for major-version changes. While the newest major matures,
the newest eligible same-major fix can proceed. Updates published by this host
retain a private digest approval, so deployment pulls the observed bytes even
if the tag later moves. Git changes authored elsewhere are resolved at pull time.
Registry discovery needs Skopeo's own
authentication; use `skopeo login` for private registries as well as runtime login.

Recovery after interruption is explicit. First ensure the old executor and its
children/hooks are stopped, inspect `history`, runtime containers, and traffic
state. Then use `recover NAME --mode accept` to verify the candidate and resume,
or `--mode rollback` to restore a known previous image when rollback is declared
safe. Before deployment started, rollback cancels the operation and resumes if
needed. Accept requires deployment to have started and the candidate receipt to
be running and healthy. Initial unsafe failures need application-specific repair
to the candidate, followed by accept. Mooring cannot invent a safe data restore.

Rollback preserves named volumes and uses a local image ID, so retain prior
images while recovery is needed. Mooring never runs Compose down, deletes volumes,
or prunes images. Operation history and snapshots currently have no automatic
retention; monitor disk use and back up the private state directory. Interrupted
operations and their release files must be retained. Git snapshots are limited
to 128 MiB and reject symlinks/submodules. Deployment branches should contain
configuration and references, not application assets or credentials.

Each entry deploys one selected service from one Compose file. Sibling services
and `depends_on` are excluded from deployment; shared Compose resources remain
part of the configuration check. Builds, includes, configs/secrets references
and unresolved external env files are unsupported. Backup hooks own persistent
data; Mooring stores rendered environment values privately in deployment state.
