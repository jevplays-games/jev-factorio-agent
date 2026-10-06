# Supervisor handling of strict two-stage decisions

The supervisor treats the controller's strict assessment-then-choice record as
durable work until both the inner decision and its existing persistent
selection attempt have a matching terminal disposition. A saved `settled` phase
alone does not finish the obligation: the controller saves that phase before
it finalizes the outer attempt, so a process can stop in between those writes.

For a two-stage record, the supervisor validates the record against the
checkpoint session and target, rebuilds the selection-batch metadata and
decision-input digest from the prepared request, and locates exactly one outer
attempt with that same source and input identity. A nonterminal phase, invalid
record, absent or duplicate outer row, changed request metadata, or incompatible
outer outcome remains unresolved. A terminal outcome only counts when it is
consistent with the saved two-stage outcome. A pending outer attempt also stays
unresolved when an older checkpoint has no `two_stage_decision` field, or when
that field is null; a missing legacy outcome is treated as pending. Archived
attempts are considered only after the checkpoint-bound archive loader verifies
their content-addressed history. Existing pending actions, active plans,
reservations, transfer recovery, and background ownership remain independent
unresolved-work checks.

The same predicate guards source changes, manual intervention, completion,
and operational repair. Repair must preserve the strict decision record and
outer attempt ledger, including when the strict record is absent. A changed or
unknown source cannot adopt a checkpoint while its two-stage provider request
or outer selection remains unfinished. Supervision and repair do not resume the
model request, dispatch native actions, or settle the attempt on the
controller's behalf. Once the real controller has a validated terminal
decision and the exact matching outer row is terminal, that retained decision
is historical evidence and does not by itself block later reviewed source
handling.

The added checks exercise the actual local controller and supervisor entry
points with offline mock provider/backend implementations. They do not qualify
the deployed Factorio campaign, its native state, or the separate runtime and
operational acceptance criteria tracked by #495, #92, and #103.
