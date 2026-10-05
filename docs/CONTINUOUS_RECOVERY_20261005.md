# Continuous campaign recovery: October 5

The latest crash was a decision-history archive boundary defect, not a VM
failure. At 16:30 UTC, the controller had 1,024 active decision records and a
tracked science craft. Archive rotation rejected the background job and exited.
The world continued and completed the craft. The owner held instead of replaying
it. Earlier strict JEV evidence holds and an observer false alarm are separate
incidents; a successful restart does not explain all of them.

## Classify before restarting

| Live evidence | Meaning and next check |
| --- | --- |
| Controller terminal result and archive-boundary traceback | Inspect active-tail count and the exact background job, attempt and saved step. |
| Live controller, `blocked`, rejected usefulness or choice answer | Strict JEV hold. Preserve the model request/response and investigate missing current evidence; do not reroll or lower floors. |
| Advancing receipt-bound craft counters | Useful work is continuing even if no whole action has completed. Check counter and event-tick progression together. |
| Long pending gather and old dashboard observation | Read native work and its deadline before calling it a crash. A blocking gather can make the event feed stale temporarily. |
| Guest or game service absent after a known reboot | Check the existing guest, world save and runtime binding. A controller checkpoint cannot reconstruct the world. |

The controller VM's existence is not proof it owns the run. This campaign's
actual controller is inside `factorio-native-acceptance-20260927`, under the
`jev-continuous-controller` service. Verify process command lines and working
directories instead of starting another controller on the idle dedicated VM.

## Short recovery sequence

1. Read the current owner status, linked prepared/launched/result receipts,
   console tail and checkpoint under `/home/ubuntu/jev-continuous-20261004/run`.
   Confirm the original session, run ID, source revision, writer lock and
   until-complete authority. Compare useful-action timestamps with current
   native state, not just process uptime or game ticks.
2. Preserve the exact checkpoint bytes and digest, pending work, native receipt,
   provider state, source pins and owner signatures. Check a recent rotating
   autosave. Keep the stopped/recovering state visible; never reset progress age
   merely because a process restarted.
3. If the native craft completed while the controller was down, qualify its
   original identity, paid inputs, output inventory, actor, empty queue and
   completion deadline through the reviewed completed-background attachment.
   Run one admitted `--reconcile-only` invocation under the original writer
   lock. Retain resume/composition/provenance; remove continuous-run,
   reevaluation and persistent-idle flags as described in the
   [attachment incident note](OUTPUT_BUFFER_STALL_20261005.md).
   Verify exactly one completion, zero model calls and zero gameplay dispatches.
   An unfinished or ambiguous receipt remains held.
4. Deploy clean, signed, merged source through its existing source admission.
   An infrastructure-only fix with an unchanged decision contract uses
   [compatible-source recovery](COMPATIBLE_SOURCE_RECOVERY.md), preserving
   aggregate paid-attempt limits. If consumed events left rolling history,
   authenticate original journal and launch/result bytes and use the signed v2
   epoch witness. Do not insert old events into current history or change source
   hashes to satisfy a gate. A failed or ambiguous installation requires
   readback, never a blind second application.
5. Resume through the existing owner once. Verify its actual source, single
   child, consumed admission, original world/session, unchanged historical
   reevaluations, authenticated archive coverage and new useful native actions.
   Check the actual OBS **Program** output, streaming and audio; Preview alone
   does not establish what viewers see.
6. Continue the [persistent progress monitor](PERSISTENT_PROGRESS_MONITOR.md)
   and active investigation across later production stages. Distinguish a
   monitor alert, policy hold and process exit. Monitor expiry ends the watch,
   not an until-complete game. Never restart a shared host or WSL to repair an
   individual service.

## Verified deployment and limits

The repairs were reviewed and merged in PRs
[#429](https://github.com/jevplays-games/jev-factorio-agent/pull/429),
[#430](https://github.com/jevplays-games/jev-factorio-agent/pull/430),
[#432](https://github.com/jevplays-games/jev-factorio-agent/pull/432) and
[#434](https://github.com/jevplays-games/jev-factorio-agent/pull/434).
The production controller uses merged commit
`9ba7d55477f76130781cbb0b57e2a5e49f4dd9ac`. Its source fingerprint is
`a2627811f7bb547af902b16616478063b1f1a298ee62c34bb5f7a040a7cb7e14`.
The deployed observer independently includes the tracked-craft progress repair.

At 17:13 UTC, observation-only reconciliation verified the original 20-pack craft
exactly once, without a model request or action. A bounded same-source resume
then archived the full tail and reached a real idle policy boundary. The final
compatible migration retained 1,063 decision records, all 15 historical source
reevaluations, native ownership and provider state. It changed only the recovery
source header and appended the signed compatibility record. No new JEV
reevaluation allowance was granted. Fresh coal gathering was verified after the
17:35 UTC deployment; subsequent iron gathering and extraction also progressed.

The final combined source passed 204 focused tests before deployment. The
individual final PR heads passed their hosted Python, browser and research
checks. Retained native receipt and archive fixtures supplement these tests;
they do not establish uninterrupted multi-day operation. Continue the soak and
record any later stall separately. Strict JEV can still abstain, and this direct
attachment path does not authorize recovery of an unfinished native craft.
