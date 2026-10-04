# Starting a new persistent native campaign

The deployed Train service layout, save checks and recovery sequence are in
[continuous-runtime operations](CONTINUOUS_RUNTIME_OPERATIONS.md).

Use `--initialize-persistent-campaign` only when the operator explicitly
authorizes a new dedicated world. It enables persistent blocked recovery from
the first decision, instead of requiring the first invocation to stop before
that mode can be enabled. It does not resume or repair an existing campaign.

The owner supplies validated `JEV_FACTORIO_PROVENANCE` with the exact deployed
source revision, holds the campaign writer lock, and selects unused checkpoint
and research paths. A typical child command is:

```sh
python -m jev_factorio --backend fle --controller hierarchical --policy jev \
  --model jev-1.13.0 --target rocket_launch --until-complete \
  --persist-recoverable-blocks --initialize-persistent-campaign \
  --persistent-idle-observations 0 --factory-scheduling ready-work \
  --background-work --furnace-output-buffers --furnace-input-belts \
  --mining-outposts --campaign-diagnostics --profile-observations \
  --consolidated-observations --tick-seconds 0.1 \
  --checkpoint /private/new-run/controller.json \
  --run-dir /private/new-run/research-initial \
  --log-file /private/new-run/gameplay.jsonl \
  --dashboard-events /private/new-run/dashboard.jsonl
```

Credentials remain in the existing private environment, never in this command
or provenance. Model availability and any production treatment must be checked
for the deployed run. A marked dedicated Factorio world and connected native
player are still required. Initial fixture items are not earned gameplay.

Admission rejects resume/adoption/migration flags, existing checkpoints, and
previous initialization intent. Immediately before backend attachment it
exclusively writes and fsyncs `controller.json.initialization.json`. If startup
fails, retain that record and reconcile the partial world. Changing the research
directory does not authorize another initialization. Existing resume paths keep
their source, session, checkpoint and native-attachment checks.

For multi-day operation, supervise the game, viewer and controller independently:

* Enable periodic game saves and retained generations before launching. A
  five-minute interval with 12 slots is the current operational starting point;
  verify save completion and restore behavior. Keep experimental nonblocking
  saving disabled until separately qualified.
* Keep game and controller identity separate. A restarted game must load the
  newest valid save, not silently fall back to the initial world. A controller
  checkpoint is not a game save.
* Treat a game restart as a reconciliation boundary. Current FLE executable
  runtime closures live outside saved Factorio storage; a saved world alone
  does not restore the native attachment, receipt ledger or callback chain.
  Do not enable blind controller restart after a game reboot.
* Preserve an exclusive writer lock and durable initialization/launch/exit
  records in the service owner. Unexpected controller exit may resume only when
  the existing native attachment and checkpoint pass normal reconciliation.
  A service manager must never replay fresh initialization automatically.
* Keep useful-progress age separate from PID, heartbeat and game tick. Zero idle
  observations disables the passive-wait exit limit; it does not make an
  unchanged blocked decision healthy. Report prolonged stalls honestly.
* Freeze the accepted source during the run. Plan deployments at reconciled
  boundaries, validate receipt contracts before stopping a child, and preserve
  all failed attempts. Native ambiguity and nonrecoverable blocks still hold.

The new flag is not proof of days-long reliability. Qualification still requires
native useful actions, verified saves, controlled interruption/reconciliation
tests and a sustained 48–72 hour run. See the October 4 reliability investigation
for the separate maintenance and world-durability failures.
