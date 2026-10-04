# Recurring native controller stops: October 4 investigation

The existing native campaign has multiple independent failure paths. Restarting
the dashboard or keeping a process alive does not address them. This report
separates observed causes, tested corrections, and work still needed before a
multi-day reliability claim. Raw runtime scripts and credentials are private.

## Evidence and causal chain

The September 30 through October 4 archive contains 34 persistent-recovery child
exit records, excluding separate stop-helper records:

| Exit family | Count | Observed behavior |
| --- | ---: | --- |
| Exit 0 while blocked | 19 | 17 exhausted six unchanged observations at 300-second intervals; two other blocked/model-abstention exits |
| Interrupted, exit -2 | 9 | SIGINT/KeyboardInterrupt; several associated with explicit maintenance/source-forward operations |
| Error exits | 6 | Three preflight exit-2 errors and three exit-1 errors involving recovery ledgers, archive lookup or duplicate candidates |

These are exit records, not 34 independent infrastructure crashes. In
particular, exit 0 did not mean the campaign completed successfully. The owner
waited for a single child, recorded the result, and exited. It did not provide
an ongoing reconciliation/restart service. The guest's inspected system services
contained no matching Factorio/JEV supervisor service.

Latest launch118 began at 2026-10-04 11:46:25 UTC and ended around 16:03 UTC with
KeyboardInterrupt during sleep after a maintenance stop. It explicitly used
`--until-complete --persist-recoverable-blocks --persistent-idle-observations 0`.
The zero observation limit avoided the earlier idle-budget exits, but permitted
hours of repeated blocked observations. The checkpoint remained `Candidate
evidence insufficient`; the last useful action was an iron-plate extraction at
tick 37637264, versus blocked checkpoint tick 38536189. Advancing game time and a
live PID were not evidence of useful decisions.

The source-forward operation125 moved the existing checkout from `4c487ce1` to
`ba83e4a7`, incorporating PR316's intermediate-craft usefulness correction.
That source change needs native verification; it is not itself proof that the
blocked campaign recovered. Pending action and background-attempt fields were
null at the inspected checkpoint. No ledger or failure history was cleared.

### Deterministic maintenance receipt mismatch

Maintenance123's terminal reconciliation evaluates:

```python
result['script_sha256'] == expected_script_sha
```

Actual launch118's terminal result does not contain `script_sha256`. It contains
prepared/launched receipt links and other identity fields, but the direct lookup
still raises `KeyError('script_sha256')`. Replaying the exact predicate offline
against the retained result reproduces the failure. The helper catches
`KeyError` while retrying reconciliation, so waiting cannot supply the missing
field. Staged launch126 repeats the omission in its effective result-writing
function, including the exceptional result branch.

An archived explanation referred to a lock-field mismatch. Inspection found
that the relevant derived field is initialized during preflight. The missing
receipt field is independently reproducible; the helper did not retain each
swallowed exception, so the exact first failing check after the signal remains
unproven. Do not rewrite that uncertainty as a proven lock failure.

A private, unsigned producer correction adds the script digest to both result
branches. Offline tests show the exact predicate accepts the corrected receipt
and still rejects a conflicting digest. Historical receipts and signed deployed
scripts remain unchanged. A new signed, reviewed, fully pinned runtime artifact
and its normal deployment acceptance are required before calling that correction
deployed. Never repair history by adding a field to an already-issued receipt.

### Separate management transport and host failures

The concurrent operator's event record attributes the QGA outage to an archive
response exceeding the 10 MiB transport limit. Recovery repaired the existing
channel/backend and restarted only the guest-agent service. This was separate
from the earlier controller exit. Selected bounded reads subsequently worked;
no VM restart was needed for that repair. Before the later host outage, the
guest had six days of uptime and approximately 13.85 GB free space. The older
storage-pressure incident does not explain these observations.

During recovery, SSH then timed out before canonical carrier dispatch. Train
became reachable after a host boot at about 19:53:53 UTC. The existing native
guest was `shut off`, with autostart disabled and no managed save. This session
did not restart Train; it subsequently started the already-stopped guest for
read-only recovery inspection. The reboot's cause is not established by
the collected journal tail. Pre-reboot process/native-state observations must
not authorize post-reboot action replay. Recover the original save and reconcile
it against durable receipts before continuing the same campaign.

### World persistence was disabled

Post-boot inspection found `autosave_interval: 0` in the game's actual server
settings. The configured write-data directory contains the original
`initial-working.zip`, byte-identical to `immutable-pre-controller.zip`. Both
have SHA-256 `8e468af5e81ecc712c2d687c83a65ced6fbeeb4d3bd57a7b84ad1cd4a111bf2d`.
A search of 46 ZIP archives across guest `/home`, `/opt` and `/var/backups`
identified those two campaign worlds and the shipped menu simulations, with no
newer campaign world. Libvirt reported no snapshots or managed save; the bounded
host backup-path search found no matching recovery artifact.

The controller checkpoint survived unchanged, but it cannot reconstruct the
Factorio world. The recovered guest had no game/controller process. Resuming
the latest controller against the initial world would falsely join incompatible
histories. No such launch was performed. Current recovery is blocked on a
compatible saved world or a separately authorized new campaign; an undiscovered
external backup remains possible. The original run's evidence must be retained.

This is a distinct durability failure in addition to the recurring controller
stops. Future admitted runs must have periodic world saves, retained generations,
verified save completion/age, and a restore protocol tied to checkpoint/receipt
identity. A controller checkpoint alone is not a world backup. A disk snapshot
without saved game state is not a substitute for saving the live world either.

## Fast recovery procedure

1. Check host uptime, exact domain UUID/state, guest-agent responsiveness, current
   owner/process identity, checkpoint and most recent useful receipt. Separate
   host, guest, game server, controller, model and display health.
2. Read the latest executed signed launch contract and linked results. The old
   September 28 prepared initializer was not the latest authority. Launch118 and
   the staged126 recovery explicitly used until-complete for the same isolated
   campaign. Do not require a new cutoff because an older initializer has null.
3. Retain failed dispatch records and inspect prepared/launched/result evidence
   before retrying. A carrier handshake only proves bootstrap verification.
   Recheck ownership and native state after any host/guest boot.
4. Before stopping a healthy child for a source deployment, validate the complete
   stop/result/reconcile/relaunch contract offline. Use the field audit below as
   one preliminary check, then test real receipt round trips and fault paths.
   Publish/sign/pin the replacement before initiating the maintenance boundary.
5. Resume through the existing accepted owner, preserving the same save,
   session, receipts, pending actions and history. Never replay a one-use script
   automatically or delete evidence to make preflight pass.
6. Verify useful subsequent gameplay and the live Program output. A fresh
   heartbeat, advancing tick, bootstrap receipt or repeatedly blocked decision
   is insufficient. Keep the display stopped/recovering until supported by fresh
   evidence. Record time to useful progress and any unresolved blocker.

## Read-only receipt field audit

From an installed checkout, run against local copies of the exact scripts:

```sh
python -m jev_factorio.native_launcher_audit \
  --launcher /private/exact-launcher.py \
  --maintenance /private/exact-maintenance.py \
  --producer launch \
  --consumer Root118MaintenanceAdapter.readonly_reconcile_terminal080_and_interrupted_seal
```

Exit 0 means the recognized literal terminal receipts contain the recognized
strict consumer fields; exit 1 means missing fields; exit 2 means unsupported or
unreadable input. The JSON always reports `launch_authorized: false`. The tool
parses ASTs without importing or executing the scripts, bounds reads to 1 MiB,
and selects the last definition when historical functions are redefined.
Exception-only receipts are reported separately from terminal child exits.

This is a narrow structural lint, not a control-flow, value, signature or
provenance proof. It recognizes direct `result['field']` accesses and literal
`durable(RESULT, {...})` writes in the selected functions; aliases, runtime
rebinding, nested scopes and other receipt mechanisms need separate review.
Hashes describe the audited source text; copying with different line endings
changes them and does not authenticate a deployed script. No audit result
authorizes a native launch or changes an existing reconciliation gate.

Validation: nine focused offline tests pass. The exact retained126 producer
fails with missing `script_sha256`; the privately corrected candidate passes the
field check. Neither outcome constitutes native gameplay acceptance.

## Requirements for days of useful autonomous operation

| Requirement | Why it matters | Current evidence |
| --- | --- | --- |
| Durable owner with reconciled restart | A one-child launcher leaves the run stopped after any exit | Not established by existing launch118/126 |
| Useful-progress watchdog | Unlimited idle observations can conceal a four-hour stall | Need receipt/progress-age health, independent of process liveness |
| Stable accepted deployment | Routine source cutovers create avoidable stop/handoff exposure | Nine interrupted results; maintenance contract defect reproduced |
| Boot and save recovery | Host reboot removes volatile game/control state | Autosave disabled; only initial campaign world found; guest autostart disabled |
| Bounded evidence transport | Diagnostic collection must not disable recovery access | Oversized QGA archive response reported; bounded reads verified |
| Fault recovery and soak acceptance | Unit tests do not demonstrate multi-day native reliability | No 48–72 hour useful-progress soak completed in this investigation |

A service manager may keep a durable supervisor alive, but `Restart=always` on
a one-use launcher is unsafe: it can replay an ambiguous effect or conflict with
ownership. The supervisor must distinguish clean completion, intentional stop,
policy block, recoverable transport failure and unresolved native effects;
reconcile before resuming; and enforce the original window and identity.
The existing timed/hybrid supervisor must not be substituted into this JEV-only
until-complete campaign without verifying treatment and window compatibility.

Before claiming the problem fixed for days, qualify graceful maintenance,
controller interruption, model/transport loss and host boot in isolation with
the receipt protocol, then observe 48–72 hours of fresh useful gameplay. Freeze
the accepted gameplay deployment during that soak. Track unplanned downtime,
useful-action age, blocked duration, restart cause and reconciliation outcome.
