# Retained launch-module reconciliation

An attached native profile can be valid for its existing modules while still
carrying an older `launch_readiness.lua` closure. The attachment reader accepts
the source-pinned legacy launch digest so unrelated retained capabilities can
continue to work. That digest alone does not prove the current receipt check is
loaded. `NativeFactory` therefore requires a runtime capability emitted by the
current launch module, matching the attached session, actor, profile, and
current launch-asset digest, before launch, payload load, launch-pad, or fish
operations. The check runs before movement and again in the same Lua command as
the guarded operation. Other retained operations keep their existing paths.

The constructor does not reinstall a module into a retained campaign. A caller
may ask `prepare_launch_reconciliation_upgrade()` for a one-use Lua command, but
preparing that text does not authorize or send it. The command only proceeds
when the exact supported legacy profile, current session and actor, and original
pinned launch digest still match. It runs the reviewed module source, then
requires the existing launch state, attempt table, receipt table, and runtime
capability to remain bound before updating that installation's launch-asset
digest. It does not launch, load cargo, place a pad, collect fish, reset state,
or create receipts. An operator must separately authorize and apply this
command through the established native reconciliation process, then obtain a
fresh source-bound attachment readback before using launch operations.

The explicit upgrade branch checks the attached asset map and the live event
registrations against the installed fair-actions, launch-readiness, craft-job,
and output-buffer callbacks before changing launch state. It installs only the
new launch guard; it does not rerun the ordinary installers or replace the
retained observer, transfer, crafting, tick, or pathfinding callback chain. The
post-install check repeats the graph qualification and rolls back the launch
guard and launch digest if that check fails.

If the retained profile, asset digest, session, actor, state, or loaded module
does not match, launch-related operations fail closed before movement or a
native action. Unknown or partial installations still require the existing
broader reconciliation path. Offline Lua tests establish the command and guard
contract; they do not authorize an in-game upgrade or establish Factorio
runtime acceptance.
