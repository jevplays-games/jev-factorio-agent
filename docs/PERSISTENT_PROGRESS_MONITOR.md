# Persistent useful-progress monitoring

`python -m jev_factorio.progress_watch` observes a pinned campaign through a
read-only probe adapter. It does not attach a game backend, call JEV, restart a
controller, change a checkpoint or grant recovery authority. Run it in a separate
service so a healthy game process cannot conceal a stalled decision loop.

The probe returns a small JSON object with `session_id`, owner heartbeat `at`,
`checkpoint_status`, `owner_phase`, `owner_alive`, `child_alive`, `reason`,
`pending`, `progress_tick` and `last_progress_at`. The latter two describe verified
useful work, not the world tick or a new process start. Include verified
background-job completion when deriving the latest progress tick. Use a pinned
adapter that verifies the expected VM/session and reads only bounded status,
checkpoint and process metadata. Never copy credentials into probe output.

For a running background craft, a probe may also supply `background_craft`.
Use `background_sample(checkpoint)` to bind its receipt, requested batch count,
finished count and progress/deadline ticks to the checkpoint's session, attempt
and exact saved-step fingerprint. This projection trusts the pinned controller's
retained native observation; it does not independently inspect the game. Omit
this optional evidence when no tracked craft exists. Existing probes retain
their completed-action-only behavior.

The monitor requires two observations of the same craft with both its finished
count and native event tick increasing. Only then can it report `progressing`
with reason `tracked craft is advancing`. It preserves the separate completed
useful-action timestamp and age. A first observation, replacement receipt,
repeated count or rewritten heartbeat cannot refresh the craft's progress time.
If counts stop advancing for the stall interval, the warning returns. Regressed,
inconsistent or expired craft evidence is unknown; controller blocks, uncertain
actions, stopped processes and completion retain precedence. Monitoring never
unlocks craft outputs or grants a gameplay retry.

By default, sample every 15 seconds, label heartbeats older than 30 seconds
unknown, and flag 120 seconds without verified useful progress. Policy blocks
and uncertain actions are visible immediately. Pending native work remains
explicit; a no-progress label never licenses replay. Known completion and a
service-owner stop are reported without an automatic recovery request.

```sh
python -m jev_factorio.progress_watch \
  --probe-command-file /private/monitor/probe-command.json \
  --display-command-file /private/monitor/display-command.json \
  --state-dir /private/monitor/state --session-id EXACT_CAMPAIGN
```

Each command file is a JSON argument list starting with an absolute executable.
Adapters are trusted deployment configuration: review and pin their contents.
The probe's stdout is JSON; the optional display adapter receives classified
status on stdin. Use it only to update a dedicated status display, and verify
OBS Program rather than Preview. Adapter errors are recorded by type without
copying raw stderr or secrets into the status. A display failure does not
misrepresent the game as healthy or overwrite the game's classification.

The monitor writes atomic `status.json` and a transition journal capped at
approximately four MiB with three rotated backups. It preserves progress age
and the observed blocked episode across monitor restarts. Repeated historical
ticks cannot become fresh because an owner rewrites their timestamp. Missing
identity, stale heartbeat, regressed progress or a regressed clock is unknown.
A separate monitor lock prevents two instances sharing the same state directory.

For a Train user service, enable lingering and `Restart=on-failure`; the service
may restart this observer, never a gameplay launcher. Keep monitor state outside
the campaign's checkpoint directory. Verify `status.json`, the transition log,
service enablement and the actual rendered display after installation. Probe
and display errors, monitoring-service downtime, and gameplay blocks are separate
conditions; this observer does not provide an external paging service or prove
multi-day gameplay reliability.

On October 5, a native 20-pack logistic-science craft advanced from 8 packs at
tick 10927618 to 18 at tick 10931102, then completed at 10931702. The earlier
completed-action-only monitor briefly raised a no-progress warning during this
valid long craft. The regression case retains those observed counters and
checks that advancing work avoids that warning while a frozen counter still
expires after 120 seconds. This corrects a display false alarm; it does not
resolve JEV decision holds or establish multi-day reliability.
