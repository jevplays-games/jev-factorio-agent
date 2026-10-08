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
inconsistent or expired craft evidence is unknown during running work; uncertain
actions, unrelated controller holds, stopped processes and completion retain precedence. Monitoring never
unlocks craft outputs or grants a gameplay retry.

When the foreground is waiting specifically for `low choice confidence` or
`Candidate evidence insufficient`, a qualifying advancing craft is displayed as
`crafting`, with its batch counter and foreground wait reason. The monitor keeps
the original blocked-episode timestamp and completed-action age, and reports
`foreground_status=blocked`; it does not change the controller. Once the craft
disappears, expires, or stops advancing for the stall interval, the ordinary
blocked warning returns. A first craft observation cannot suppress that warning.

A probe can also supply `native_research` from `research_sample()`. Read a bounded
tail of the pinned current execution's research log and pair an `observation`
with its accepted `observation_validated` record by observation ID. The helper
checks campaign/execution identity, native world, matching ticks, unpaused runtime
and timestamps no older than 30 seconds. Never substitute model-request facts or
an observation that has not passed validation. Omit absent or stale observations;
malformed or mismatched pairs must fail the probe. Field projections are allowed
from the trusted local log reader; this helper does not authenticate arbitrary
external event records or independently query the game.

Two samples of the same technology and force must increase both the research
fraction and native observation tick before research counts as advancing work.
A changed technology starts a new baseline; repeated values or a fresh heartbeat
cannot refresh progress. During either known foreground policy wait, the monitor
then displays `researching`, technology, percentage and the unchanged wait reason.
It preserves the blocked episode and completed-action age. A frozen counter,
missing observation or stopped research restores the ordinary warning. Native
uncertainty, unrelated holds, process stops and completion still take priority.
This evidence grants no gameplay retry or research-completion credit.
An observer gap as long as the stall interval resets the craft/research baseline:
a larger counter after that gap cannot prove that the work happened recently.
The original foreground-wait and completed-action timestamps are retained.

By default, sample every 15 seconds, label heartbeats older than 30 seconds
unknown, and flag 120 seconds without verified useful progress. Policy waits
remain visible alongside active crafting; uncertain actions are visible immediately. Pending native work remains
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

The public `atomic_json()` and `run_once()` library APIs work on Windows and
POSIX. Status writes use a temporary file in the state directory, flush and
`fsync` that file, then install it with `os.replace`. On POSIX, the containing
directory is also opened and synchronized; failures from file writes, flushes,
replacement, or directory synchronization are reported to the caller. Python's
portable Windows API does not expose the same parent-directory `fsync`, so a
successful Windows write confirms the flushed file and replacement but does
not claim that the directory entry will survive sudden power loss. The service
CLI's separate instance lock uses `fcntl`, so the service command currently
requires POSIX or WSL; this library persistence support does not add Windows
service-lock support.

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

On October 6, the monitor reported a foreground low-confidence block while the
native automation-science craft `1779cc5ea58c4085a53769a0e0635fdd` advanced
from 16 to 18 of 20 batches. The foreground decision had not stopped the admitted
craft. The regression retains those counters and checks the combined display,
preserved wait duration across observer restarts, return to a blocked warning
when progress expires, and unchanged priority for native faults and stops.

Later that night, Automation 2 research advanced from 21.2111% at tick 13330812
to 38.3139% at tick 13336969 while the foreground intermittently declined the
passive wait choice. The monitor previously treated those policy waits as a
stopped run despite lab progress. The retained fixture contains field projections
of both accepted native observation pairs, explicitly distinguished from complete
hash-verifiable event records. Tests cover increasing/frozen/regressed counters,
pairing, freshness, identity, restart preservation and fault precedence.
