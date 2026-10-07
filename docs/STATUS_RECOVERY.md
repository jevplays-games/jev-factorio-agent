# Status checks, recovery, and the live overlay

For strict confidence holds, see [two-stage JEV decisions](TWO_STAGE_JEV.md).
For repeated stale decisions during unrelated fish movement, see
[the fish freshness diagnosis and recovery note](FISH_FRESHNESS.md).
For an optional input route hiding existing serial service, see
[the input-route service diagnosis](INPUT_ROUTE_SERIAL_SERVICE.md).
For the October 6 planner cycle and guest-agent access failure, see the
[incident checklist](RECOVERY_20261006.md).

For the replacement October 4 Train campaign, start with the concrete
[continuous-runtime operations runbook](CONTINUOUS_RUNTIME_OPERATIONS.md).
For the October 5 archive crash, completed-craft reconciliation and compatible
deployment, use the [short continuous recovery checklist](CONTINUOUS_RECOVERY_20261005.md).
For an empty-boiler stop after utility connections, use the
[sparse inventory and completed-connector recovery note](EMPTY_BOILER_RESUME_20261005.md).

A status question about an expected ongoing campaign includes authorization to
repair an unexpected stop. Complete diagnosis, safe recovery, and verification
instead of returning only a stopped status. This does not authorize restarting
an intentionally stopped/completed run, extending its original cutoff, changing
an immutable treatment, or bypassing an unresolved native-action boundary.

For a coal hold that hides ready furnace output, see the
[output-buffer ordering and rotated craft proof note](OUTPUT_BUFFER_STALL_20261005.md).

## Recover through the existing owner

1. Identify the active campaign, deployed source, supervisor, service owner and
   original cutoff or explicit until-complete authority. Use the latest executed
   signed owner and its linked receipts; an old prepared initializer is not the
   current owner contract. Check live processes, checkpoint status, event history and
   completed-action timestamps. A running PID or advancing game tick alone does
   not prove useful autonomous progress. Distinguish gameplay from development
   agents and completed issue-writing tasks.
2. Put the existing maintenance/recovery presentation on the live output while
   diagnosing. Confirm the dashboard follows the active campaign and retains
   original evidence timestamps. Do not relabel old observations as fresh.
3. Preserve the world, actor, pending action, receipt identity, ownership,
   reservations, failure history and immutable incident baseline. Inspect native
   controls before backend attachment: binding fair controls can replace the
   previous job's diagnostic state. An absent receipt alone is not proof that an
   action had no effect.
4. Follow [reliability recovery](RELIABILITY_RECOVERY.md),
   [supervisor acceptance](AUTONOMOUS_SUPERVISION.md) and
   [provenance ownership](SUPERVISOR_PROVENANCE.md). Coordinate with the current
   owner and its actual locks. Do not instantiate a second live supervisor for
   verification or remove a blocked gate by editing its JSON. Check maintenance
   ownership, storage admission and the remaining authorized window.
5. Use the applicable reviewed reconciliation contract. A code repair must pass
   its tests, review, publication and deployment gates. An operational repair
   needs genuine native evidence and the supervisor's normal acceptance. A
   status-only checkpoint transition, where that contract allows one, retains
   pending for the resumed controller to verify; it is not proof of success.
6. Resume through the established service owner only after acceptance. Verify the
   original attempt's reconciliation and subsequent autonomous useful actions.
   Restore the gameplay presentation and verify both the visible output and
   current dashboard state. Report any remaining root-cause risk separately from
   a successful operational recovery.

If required evidence or authority is missing, keep the unresolved boundary and
truthful overlay intact, complete independent authorized work, and report the
specific missing requirement. Do not use a shared VM restart as a recovery step.

## Fast path: QEMU guest-agent channel lost

Use this path only when the existing Factorio QEMU domain is still running but
the host can no longer inspect the guest through QEMU Guest Agent (QGA). A lost
QGA channel is a management-path failure; it does not prove that the guest or
Factorio stopped. Do not create another domain or controller.

1. On the existing libvirt host, make read-only checks:

   ```sh
   D=existing-domain-name  # substitute the exact recorded libvirt domain
   virsh -c qemu:///system domstate "$D"
   virsh -c qemu:///system domifaddr "$D" --source arp
   virsh -c qemu:///system qemu-agent-command "$D" '{"execute":"guest-ping"}'
   virsh -c qemu:///system qemu-monitor-command "$D" --pretty '{"execute":"query-chardev"}'
   ```

   Confirm the domain state and whether the guest-agent channel is connected.
   Use the existing known guest address and configured SSH identity if available;
   keep normal SSH host-key verification enabled. A refused SSH connection or an
   empty serial console is not evidence that the VM is stopped. If attaching to
   the existing serial console, attach with
   `virsh -c qemu:///system console --safe "$D"`; detach with Ctrl+] when there
   is no login prompt. Do not send guessed credentials or commands.

2. If an authorized in-guest administrative shell is available, inspect the
   guest-agent unit and its recent logs, then restart only that unit if it is
   stopped, failed, or demonstrably wedged in its own health/log evidence:

   ```sh
   sudo systemctl status qemu-guest-agent --no-pager
   sudo journalctl -u qemu-guest-agent --since '-30 min' --no-pager
   # Run these only after the checks above establish the service is unhealthy.
   sudo systemctl restart qemu-guest-agent
   sudo systemctl is-active qemu-guest-agent
   ```

   Do not install or enable a new access service as a shortcut. Back on the
   libvirt host, repeat `guest-ping` and `query-chardev`. Proceed only after the
   channel responds. Record the observed service failure as the cause only when
   guest logs establish it; otherwise leave the cause unknown.

3. Re-run the existing read-only owner census immediately. Record its timestamp
   and preserve the same session ID, checkpoint bytes, pending action, receipts,
   attempt history, source revision, owner lock and cutoff. Reconcile through the
   existing owner. A terminal reconciliation that freezes restart, a missing
   owner handoff, or an unverified original window remains a hard stop after QGA
   is restored. Do not promote a prepared-but-unexecuted manifest or infer
   authority from a null cutoff. Conversely, do not mistake an old initializer's
   null cutoff for missing authority when the latest executed signed owner
   explicitly establishes until-complete for this isolated identity, save and
   owner. Preserve whichever window actually governs the run; never convert a
   timed campaign implicitly. See [NATIVE_ACCEPTANCE_WINDOW.md](NATIVE_ACCEPTANCE_WINDOW.md).

4. Resume only through the accepted existing supervisor and only inside the
   verified original window. Follow the normal reconciliation and acceptance
   gates above. Verify a fresh owner readback, the original attempt's outcome,
   advancing native evidence and a subsequent useful gameplay action. Until new
   evidence arrives, keep the overlay labeled stopped/stale; a host heartbeat or
   repeated old snapshot must not make old game events look fresh.

If QGA is disconnected and neither authorized SSH nor a usable serial login is
available, leave the running VM untouched and report the access blocker. A full
VM restart interrupts the live game server and may lose unsaved state. Obtain
explicit user approval before attempting one. After approval, use only the
existing operator's graceful procedure. On a confirmed libvirt host this is
the following sequence, replacing the placeholder with the exact existing
domain. Request shutdown and verify its completion:

```sh
D=existing-domain-name
virsh -c qemu:///system shutdown "$D"
virsh -c qemu:///system domstate "$D"
```

Run the start command only after `domstate` reports `shut off`:

```sh
virsh -c qemu:///system start "$D"
```

If graceful shutdown does not complete, stop rather than force-destroying or
resetting the domain. Recheck QGA, the existing save, the same campaign identity
and owner handoff before any controller resume. This QEMU procedure does not
permit a WSL restart.

Keep guest-agent responses small. Never return an entire archive through one
`guest-exec-status` response: an oversized response can break subsequent libvirt
agent access. Read selected fields or bounded chunks with explicit byte limits,
and use the established file-transfer channel for bulk evidence. A transport
failure is not permission to repeat a potentially executed mutation.

For recurring blocked exits, maintenance receipt mismatches and reboot recovery,
see the [October 4 reliability investigation](RUNTIME_RELIABILITY_20261004.md).
For a live controller blocked after steam-engine placement despite a confident
global choice, see the [October 5 outpost evidence stall](OUTPOST_FRONTIER_STALL_20261005.md).
For the subsequent strict-choice hold between paid crafting and raw gathering,
see the [craft handling estimate investigation](CRAFT_HANDLING_STALL_20261005.md).
After a host/guest boot, verify the actual world save and its relationship to the
controller checkpoint before starting gameplay. Check the server's configured
write-data directory, autosave settings, retained save timestamps and receipts.
If only the initial world remains, preserve the stopped campaign and seek a
compatible backup; do not attach the latest checkpoint to a reset world.

## OBS Studio Mode: Preview is not Program

In Studio Mode, selecting the Maintenance scene may change only **Preview**.
Viewers continue seeing the scene in **Program** until the transition is made.
Verify the Program title and actual rendered output after each transition.

During recovery, use the existing maintenance scene without restarting OBS,
the stream, the viewer or the game. After fresh gameplay verification, prepare
the intended gameplay scene in Preview and transition it to Program. Read back
streaming state, audio activity/settings, browser-source configuration and the
dashboard's campaign identity, source timestamp and supervisor phase. A local
selection or successful dashboard HTTP request is insufficient visual proof.

## Observed recovery on 2026-09-27 UTC

This is a sanitized incident record, not a reusable command or an automatic
retry policy. The observed deployment was
`0b5242b7a82995e81bed7c8ba02a8d9ec6e35179`.

The controller stopped at approximately 23:55 UTC on September 26 after a native
walking control lease expired. Retained diagnostics showed a failed approach
before the transfer-RPC stage. The original two-copper extraction remained
ambiguous, so the supervisor deliberately blocked further gameplay.

An independently reviewed, single-use operational intervention held the owner
and maintenance locks, retained the original checkpoint and native diagnostics,
and recorded its intent before walking and transferring. Normal walking and
native reach were enforced. Identity, receipt absence, stock, capacity and exact
inventory conservation were checked in the same native invocation as the paid
transfer. Actor copper increased from 8 to 10 and source stock decreased from
585 to 583, with the original receipt recording two items. Durable preparation
prevented blindly rerunning the intervention after interruption.

The supervisor accepted the operational evidence with only the permitted
checkpoint status transition. The resumed controller verified the original
attempt at 01:19:57 UTC; two subsequent autonomous extractions verified at
01:20:56 and 01:21:50 UTC. Source, original cutoff and historical failure counts
were preserved.

The OBS investigation found Maintenance in Preview while Mission Control was
still on Program. Maintenance was transitioned onto Program during recovery,
then Mission Control was restored at approximately 01:22:25 UTC after the
gameplay checks. Screenshots and fresh telemetry verified actual Program output,
active streaming and unchanged audio/browser-source configuration. The dashboard
already followed the correct campaign; no frontend code change was needed.

These measurements establish that recovery episode only. They do not establish
a durable fix for short control-lease expiry under scheduling/transport delays,
nor general automatic reconciliation after a failed approach. The attribution
and capacity work in [#96](https://github.com/CompleteDotTech/jev-factorio-agent/issues/96)
and [#97](https://github.com/CompleteDotTech/jev-factorio-agent/issues/97) remains
separate, as does the broader native acceptance in
[#92](https://github.com/CompleteDotTech/jev-factorio-agent/issues/92).
Private raw evidence and incident-specific operational scripts remain outside
this public repository.

## Subsequent bounded-path stop

At 04:02 UTC on September 27, the same campaign stopped on an ambiguous
five-iron-plate extraction. Native diagnostics showed that every direct
interaction path had been rejected before movement because the route required
destroying neutral obstacles. The transfer receipt was absent. An operational
intervention walked the original actor through ordinary native waypoints and
completed the exact retained transfer once. The supervisor accepted its native
receipt and inventory conservation; the resumed controller verified the
original pending action, then completed new steel extraction and crafting work.
The original campaign source and cutoff were retained.

The adapter now tries a bounded pair of ordinary waypoint corridors when all
direct interaction approaches fail before movement. Each leg still uses the
native path safety check. If any leg has moved the actor and the target remains
unreachable, the result stays uncertain rather than being classified as a
no-movement rejection. This source change is for future deployments; the
ongoing campaign remains pinned to its original source.
