# Continuous Factorio operations on Train

This runbook covers the replacement game authorized on October 4, 2026. Preserve
the original campaign and its receipts under
`/home/ubuntu/jev-native-acceptance-20260928`; its latest checkpoint cannot be
attached to the replacement world.

## Identify the current owner first

| Component | Identity |
|---|---|
| Host SSH account | `completetrain` on `train.home.complete.tech` |
| Native guest | `factorio-native-acceptance-20260927`, UUID `a6ddc89e-10ba-4cf0-b549-ca89956bb711` |
| Native guest account | `ubuntu` |
| Campaign root | `/home/ubuntu/jev-continuous-20261004` |
| Campaign session | `a0072f75985343a8abd9e73993d0c5d5` |
| Checkpoint | `run/controller.json`, relative to the campaign root |
| Owner status | `run/owner-status.json` |
| Controller service | `jev-continuous-controller.service` |
| OBS guest | `obs-production`, UUID `fd83a53b-7bd7-4364-b06c-2bd6abd428bc`, account `ubuntu` |

Use the existing bounded QEMU guest-agent transport and verify the guest UUID
before remote operations. Do not start a second controller or reuse old campaign
launchers. The live owner holds `run/single-writer.lock` and passes its descriptor
to the child. Preserve that ownership chain and all prepared/launched/result
records. Private signed owner scripts and credentials stay outside this public
repository.

## Fast diagnosis

Run these **inside the native guest**, using the established transport:

```sh
systemctl show jev-continuous-controller.service \
  -p ActiveState -p SubState -p MainPID -p ExecMainStatus
cat /home/ubuntu/jev-continuous-20261004/run/owner-status.json
systemctl is-active jev-continuous-game.service jev-continuous-display.service \
  jev-continuous-viewer.service jev-continuous-dashboard.service
journalctl -u jev-continuous-controller.service -n 30 --no-pager
```

Read selected checkpoint fields, not an unbounded history dump: `session_id`,
`status`, `reason`, `last_tick`, `pending`, `attempt`, `background_job`, the latest
verified action and the last rejected decision. Compare two samples. Advancing
ticks, fresh observations or a live PID alone do not establish useful gameplay.
An unchanged rejected frontier should back off; it should not call the model
again until evidence or an explicitly admitted decision contract changes.

| Observation | Next action |
|---|---|
| Guest shut off after an authorized host reboot | Start the existing guest; inspect save/runtime compatibility before any controller launch. |
| Game active, controller missing | Inspect the owner's exit receipt and journal; reconcile any pending mutation before resumption. |
| Owner blocked, same decision repeated | Read the last candidate, facts and independent model judgments; fix the concrete evidence or planning defect. |
| Old overlay, native events current | Compare dashboard endpoints directly; repair the read-only display path. |
| OBS absent after reboot | Check the guest desktop login keyring and startup service. Host sudo credentials do not establish the guest keyring password. |
| QGA timeouts after a large response | Inspect the guest-agent/service transport; use bounded scalar output. Never stream a large archive through QGA. |

Do not restart the host, game server or shared WSL VM as a diagnostic shortcut.
Never clear a rejected-attempt ledger, lower a judgment threshold, invent a
receipt, change campaign identity or enable a different gameplay policy merely
to make a stalled status disappear.

## Safe source recovery

1. Retain the rejected request/response and identify the missing or contradictory
   fact. Reproduce the contract defect offline and add negative cases. A model
   diagnostic is useful evidence but is not a gameplay receipt.
2. Test and publish signed source, wait for checks on the final reviewed commit,
   and stage a separate clean checkout. Keep the active source frozen.
3. Verify native attachment read-only. Current fresh installations and retained
   legacy installations have different observer contracts; do not install a
   legacy bridge merely because a generic error mentions it.
4. At an idle blocked boundary, have the existing owner stop its child. Confirm
   the owner and child exited and the single-writer lock can be acquired. Capture
   the exact final checkpoint, source revision, session and terminal receipt.
5. Use the existing resumed JEV CLI with `--reevaluate-blocked-once`,
   `--exact-checkpoint-sha256` and `--blocked-source-revision`, under the signed
   service owner. The CLI checks clean descendant source, a changed decision
   contract, exact checkpoint and unused authorization. Do not manually rewrite
   the checkpoint to make admission pass.
6. Verify a fresh native action and its inventory/entity postcondition, retained
   failure history, one live controller and a truthful dashboard. Continue
   observing useful work; a successful process launch is insufficient.

The owner records a one-use launch intent before dispatch. If dispatch or its
response is ambiguous, inspect process state, checkpoint and receipts before
deciding what happened. Do not replay initialization or maintenance blindly.

## Services and saves

The replacement deployment enables `jev-continuous-game`, `display`, `viewer`,
`controller`, `capture`, `dashboard` and `dashboard-gateway` systemd services.
The native VM has libvirt autostart enabled. The game server uses UDP 34198 and
private RCON on 27015; the viewer connects locally and uses Xvfb display `:99`.

Server settings use five-minute autosaves with twelve generations, server-side
saving and `auto_pause=false`. Inspect the ZIPs under `server/saves`, their ages
and CRC validity. The server owner selects the newest valid autosave on restart
and fails closed if a previously started world has no valid save. It does not
silently revert to the initial seed.

**A save is not a complete controller recovery proof.** FLE's executable runtime
is deliberately outside Factorio's serialized storage. A server restart can
lose native callback/receipt state, and an autosave may predate the checkpoint.
The controller owner holds on missing runtime, session mismatch or world
rollback. Automatic reconstruction across those boundaries has not been
qualified by this deployment. Never overwrite a newer checkpoint with an older
one simply to remove that hold.
The [October 7 reboot investigation](REBOOT_RECOVERY_20261008.md) confirms this
hold with surviving factory saves and retained paid ownership. It also records
the independent OBS domain autostart and keyring recovery checks.

## Dashboard and broadcast

The native dashboard reads the current `run/dashboard.jsonl` on loopback 8766.
A fixed TCP gateway on native `192.168.122.91:18766` accepts only the OBS guest
at `192.168.122.92`. The OBS `jev-acceptance-dashboard.service` forwards its
loopback 8766 to that gateway. Its proxy program lives under `/opt` because the
service's `ProtectHome=true` sandbox correctly denies programs under `/home`.
The older Train log-copy relay is disabled; its data remains retained.

The dashboard validates the HTTP Host header. A direct gateway diagnostic must
use `Host: 127.0.0.1:8766`; the normal OBS browser-source request already does.
Compare native and OBS endpoint session/event values before attributing an old
event timestamp to transport lag: a genuinely blocked game also emits less
frequently.

The existing `JEV Acceptance Mission Control UI` browser source uses
`http://127.0.0.1:8766/?studio=1`. The capture service sends the game view to the
existing Acceptance media source without changing its private SRT credentials.
OBS startup waits for its guest login keyring to unlock. Once it starts, verify
**Program**, including the Acceptance HUD, stream and audio. Preview alone is
not broadcast verification. Never publish source URLs containing passphrases.

The October 4 unlock failure was a session-binding problem, not proof that the
supplied password was wrong. `completetrain` is the Train host account;
`ubuntu` owns the OBS desktop and its login keyring. A new
`gnome-keyring-daemon --unlock` process did not unlock the existing desktop
Secret Service. Recovery targeted that existing service on the Ubuntu user's
session bus and verified the login collection's `Locked` property became false.
Prefer the desktop keyring UI; any scripted unlock must use that same session
and transient secret input, never an argument, saved script or log containing
the password. Do not replace or delete the keyring.

After unlock, the existing `sts2-obs.service` started OBS. Dismiss its crash
recovery dialog in normal mode if present, then transition the Acceptance HUD
to Program. Query the live OBS WebSocket for Program and stream state and take
a Program-source screenshot; a retained `status.json` or a black desktop preview
is not authoritative. Remove obsolete static recovery text only after checking
the live HUD, and read back stream state and audio settings afterward.

The dashboard's generic supervisor panel is not connected to this private
service owner. Its `Not connected` runtime label does not establish that the
controller process is absent. Use `run/owner-status.json` for this owner's
actual phase, child PID, save age and last useful action. Do not manufacture a
generic supervisor record to make the panel appear connected.

## Incident evidence and remaining qualification

The original stoppages had several distinct causes: clean exits after bounded
idle observations, a one-child launcher with no restart supervision, a missing
`script_sha256` terminal receipt, and later the user-confirmed Train reboot.
Autosaving had been disabled, so the old campaign could not be resumed from a
compatible world. See [the incident analysis](RUNTIME_RELIABILITY_20261004.md),
[new-campaign admission](CONTINUOUS_CAMPAIGN.md),
[missing-machine evidence](MISSING_MACHINE_EVIDENCE_20261004.md), and
[fresh native attachment](FRESH_NATIVE_ATTACHMENT_20261004.md).

The new campaign verified gathering, furnace construction, ten copper plates
and thirty-six iron ore before a separate stone-prerequisite model rejection.
The signed recovery source `4e29e7d7634369e0a13be8fdb71921d0a270b390`
then passed current native attachment and resumed the same session under owner
v3. It verified gathering five stone at tick 6840816. The next decision, at
tick 6840858, rejected crafting the iron-production furnace: actor, carried
inputs and native recipe were ready, but the independent usefulness judgment
selected `unsupported`. At that stage the controller remained alive and backed
off at the blocked frontier, with no pending action.

Four distinct offline construction-evidence revisions also failed that
judgment. The last diagnostic selected `unsupported` with probability 0.52
versus 0.48 for `useful`; neither a high candidate-choice probability nor a
positive benefit score overrides the separate usefulness gate. These standalone
experiments did not qualify a recovery. Preserve the
requests, answers, checkpoint and rejection ledger; do not repeatedly sample an
unchanged request until it happens to pass.

### Combined recovery at 23:20 UTC

[PR341](https://github.com/jevplays-games/jev-factorio-agent/pull/341) and
[PR344](https://github.com/jevplays-games/jev-factorio-agent/pull/344) merged after
all checks passed on their final heads. Together they disclose the construction
dependency and offer immediate raw-material deficits beside a ready handcraft.
A distinct combined diagnostic passed the unchanged selector; 295 focused tests
also passed. Neither repair lowers confidence or usefulness thresholds.

The existing owner stopped its child at an idle boundary, retained the exact
checkpoint and terminal receipt, then consumed one source-change admission.
Signed source `885bec67b4936575e7c17ecb43e988d7ca2958ff` now runs from
`source-recovery-v4-20261004` under `continuous_owner_v4.py`. Session identity,
failure history and the single-writer lock were preserved. The v4 authorization
is consumed; do not replay its admission or launch scripts. The owner's three
prepared launches have been used, so an exit requires reconciliation rather
than an automatic fourth launch.

Native evidence establishes actual progress beyond the original stall:

| Native postcondition | Tick |
|---|---:|
| Furnace craft receipt completed, five stone paid, one furnace produced | 7269437 |
| Iron-production furnace placement verified, unit 2547 | 7269886 |
| Two iron gears produced by completed craft job | 7274770 |
| Latest verified iron-plate extraction | 7276283 |
| Iron furnace finished 31 plates; 11 remained in its output | 7278308 |

This was a recovery of the furnace construction and production sequence, **not
sustained autonomous progress**. At tick 7278308, the next choice offered eight
gear batches and gathering five copper ore for the same one-lab target. Both
passed individual usefulness (0.66 and 0.65), but overall choice confidence was
0.24, below the unchanged 0.45 floor. The controller held with no pending action,
attempt or background job. It did not have permission to force either choice.

During smelting, fresh native output and crafting progress legitimately changed
the decision state. The final two requests show furnace output rising from ten
to eleven and finished products from thirty to thirty-one. Once those facts
stabilized, the recovery ledger stayed at 74 attempts and the loop backed off
without further model calls. Do not confuse those observations with repeated
process restarts or claim the confidence blocker was resolved.

The next investigation must retain that request and its answer, identify a
concrete selection/evidence defect, and reproduce it without rerolling the same
question or weakening a gate. The two merged repairs do not by themselves solve
confidence ambiguity between individually useful alternatives. Keep the live
dashboard in the actual blocked state, with OBS Program showing the current
feed. A live service and advancing game clock are insufficient recovery evidence.

Keep this planning failure distinct from process supervision and save
durability. An unattended 48–72-hour soak with continuing useful actions,
current valid saves and truthful status is still required before claiming
multi-day reliability. Do not reboot the live game to manufacture a recovery
test; qualify disruptive scenarios in an isolated test world.
