# Diagnose a stopped campaign after a host reboot

The October 7 interruption had three separate causes. A strict JEV decision
stopped useful work before shutdown. Train then rebooted, removing the native
Lua runtime. The OBS guest stayed shut off because its libvirt autostart setting
was disabled. Starting a controller cannot repair all three boundaries.

## Verified incident

The V46 campaign used session `a0072f75985343a8abd9e73993d0c5d5` and source
`678f2eac3f6f8ad1b850dd5e6b9c7e1df8723efc`. At 15:48:13 UTC on October 7,
its accepted observation at tick `21090420` offered a transfer of 12 carried
iron ore and a gather of four more ore. JEV selected gathering with confidence
`0.31`, below the unchanged `0.45` floor. The selection was retained and no
action was dispatched. The model's reason for that confidence is unavailable;
the recorded refusal does not prove a new planner or prompt defect.

The last useful action was an iron gather at tick `21090343`. The controller's
shutdown receipt at 16:24:35 UTC retained checkpoint tick `21212922` and digest
`45ef61072890f75a1d66695f03d7478c48e97663a48173353f7d1604a7c2455a`.
Train booted again at approximately 16:26 UTC. The guest's existing game,
viewer, dashboard and controller-owner services started automatically.

Read-only RCON on October 8 confirmed the dedicated-world marker, real-time
speed, connected original player and character unit `2543`, and the surviving
factory. Twelve rolling autosaves were present, and a stable generation passed
ZIP integrity checks. However,
`jev_fle_runtime` was absent. The owner correctly held with
`World restarted or session changed; native runtime reconciliation required`.
No new controller child or V47 admission had been created.

The power-scope fixes in pull requests 534 and 535 had merged and passed their
final-source CI. The final production source was staged as V47 but had not been
admitted or started. That staged rollout must be requalified against postboot
state; its old process assertions cannot authorize recovery on this boot.

The checkpoint includes four completed connector routes, including paid and
external cells, and a paid iron-furnace output buffer. The retained accepted
observation also contains 128 transfer receipts. These are valuable recovery
evidence; they are not a complete serialized native runtime or a qualified
restore protocol. Reinstalling empty ownership tables would lose their meaning.

The original V46 monitoring window remains failed. Its verified sample hash
chain recorded useful work until about 15:48 UTC and a process-identity change
across reboot. Continuing to append samples does not turn that interrupted
window into 24 hours of uninterrupted gameplay.

## Fast recovery checks

1. Confirm the host boot ID and use `virsh -c qemu:///system` to inspect the
   existing domains. The default user libvirt connection can show an empty list
   even while the system domains are running. Verify UUIDs before starting a
   shut-off domain; leave running domains untouched.
2. Inspect the current owner status, its service PID/start identity, checkpoint
   digest/session, pending work and terminal receipts. Old helper PID assertions
   can fail after boot. That failure does not authorize replaying deployment or
   initialization helpers.
3. Probe RCON without initializing FLE. Read the dedicated-world marker, tick,
   speed, connected player, character unit, runtime presence and native session.
   Compare saved-world identity and action/ownership evidence with the
   checkpoint. A current advancing tick and a valid ZIP alone do not establish
   that the loaded save contained every checkpointed effect.
4. If the runtime is missing, retain the hold. Resume requires a tested native
   reconstruction protocol bound to compatible world and ledger evidence, or a
   separately authorized new campaign with its own world, checkpoint and
   identity. Preserve the old campaign. Do not attach its checkpoint to a reset
   world, adopt observed infrastructure as newly paid, clear failures, or lower
   the strict choice threshold.
5. Diagnose OBS separately. A capture sender repeatedly failing to open its
   destination can mean the receiver VM or OBS listener is absent. Redact media
   URLs and credentials before printing or retaining logs. Inspect the actual
   desktop-user service and login keyring; an active launcher can still be
   waiting for the keyring while OBS itself is absent.
6. For an authorized ongoing broadcast, start the verified shut-off OBS domain
   once, unlock its existing keyring through transient secret input, and let the
   existing managed service resume. Preserve its profile, collection, audio and
   encoder settings. Verify streaming bytes increase, capture restart count
   stabilizes, the monitor updates, and the actual OBS Program image is current.

In this incident, starting `obs-production` and unlocking its existing login
keyring restored its managed broadcast and SRT receiver. Libvirt autostart was
enabled and read back as enabled. The progress monitor then reported the
controller stop without a display error. This repairs the broadcast; it does
not establish recovered gameplay or automatic keyring unlock after a later
boot.

## Remaining reliability work

Native restart recovery needs durable game-side session, actor, receipts,
ownership, pending effects and callback-version bindings saved with the world.
The recovery protocol must distinguish older saves from compatible state,
reconstruct executable callbacks without resetting their data, and reconcile
ambiguous actions before dispatch. Qualify it with isolated save/reload trials
and failures at pending, effect-before-reply and checkpoint boundaries.

Strict low-confidence decisions remain a separate acceptance risk. Capture the
original accepted observation and exact decision before proposing evidence or
planner changes. Repeated calls on unchanged evidence and deterministic choice
fallback are not recovery under this campaign's policy. Require fresh useful
native work after a justified repair and a new, explicitly recorded monitoring
window; retain each failed window separately.

See [continuous runtime operations](CONTINUOUS_RUNTIME_OPERATIONS.md),
[managed OBS recovery](OBS_MANAGED_RECOVERY.md), and
[strict decision acceptance](https://github.com/jevplays-games/jev-factorio-agent/issues/495).
