# Strict JEV assessment followed by choice

Enable `--two-stage-decisions` with the existing persistent, hierarchical,
native JEV campaign options. It is opt-in and currently synchronous; it cannot
be combined with asynchronous decisions. Existing invocations and checkpoints
without a two-stage record retain their previous behavior.

The original single request asks JEV to assess each candidate and choose among
them independently. Passing individual eligibility checks does not establish
a sufficiently confident global choice. In the October 6 campaign, individual
gathering/refueling candidates passed while choice confidence remained below
the unchanged 0.45 threshold. Prior prompt and evidence repairs sometimes
restored progress but did not establish sustained operation.

The new protocol makes two bounded requests:

1. JEV assesses useful progress, benefit, disruption and missing start evidence
   for each offered candidate. The existing numerical gates validate these
   assessments, which are saved in the controller checkpoint.
2. JEV receives the validated assessments and chooses among the qualified
   candidates, or chooses `observe`. The controller executes **that exact JEV
   choice** only if its confidence meets the existing threshold. Legacy
   selection instead uses the utility maximum after its choice gate.

The second request labels assessments as model judgments, not native success.
Rejected candidates remain in the assessment summary but are not selectable.
Request sizing cannot silently remove another qualified candidate to make the
choice easier. No scheduler fallback or confidence reduction is introduced.

## Durability and recovery

The existing persistent attempt ledger owns source identity, deduplication and
the maximum three unseen candidate batches per unchanged state. Each batch can
make at most one assessment and one choice call. Its prepared request, plans,
session, target, source, confidence floor and request budget are bound to the
phase record and cross-checked against the outer ledger.

| Saved phase | Restart behavior |
| --- | --- |
| Assessment ready | Revalidate native state, then send the assessment once. |
| Assessment pending | Hold: delivery is uncertain; do not resend. |
| Assessment received | Reuse the saved answer and prepare the choice. |
| Choice ready | Revalidate native state, then send the choice once. |
| Choice pending | Hold: delivery is uncertain; do not resend. |
| Choice received | Revalidate native state and apply the strict choice gate. |
| Settled | Resume only a still-pending outer disposition; a completed ledger entry prevents reuse. |

Every provider boundary is saved before entry, and every validated answer is
saved before proceeding. A checkpoint failure prevents later calls/actions.
Timeouts and uncertain transport failures preserve the pending phase. Restarting
does not grant another attempt. Completed records are archived by content hash
before a new batch replaces them; normal storage admission limits remain.

Fresh validated observations are required before assessment, between phases,
and after choice. Changes in semantic native facts invalidate the decision;
known observation clocks use the established normalization. This is conservative:
production changing during evaluation can invalidate a request. Ordinary native
pre-dispatch, ownership, inventory, receipt and verification checks still run
after selection. Execution/reconciliation barriers never become ordinary model
rejections.

The optional protocol modules participate in the decision-contract hash whenever
present. Historical revisions without these modules keep their previous digest.
Changing source or enabling the protocol requires reviewed deployment; it does
not authorize clearing prior attempts or retrying uncertain work.

## Validation and acceptance

Offline tests cover actual controller/checkpoint integration and the pure
protocol: phase crashes, no replay after timeouts, changed evidence, exact JEV
choice, unchanged thresholds, excluded candidates, request/ledger binding,
historical configuration and source-contract compatibility.

A bounded diagnostic on two retained October 6 blocked batches made four JEV
requests in total. One choice remained blocked at 0.27 confidence; the other
selected an eligible action at 0.46, with the floor unchanged at 0.45. Neither
diagnostic executed gameplay. This is mixed evidence, not a demonstrated cure
for every confidence hold.

These tests do not prove that JEV will make confident choices or that the native
game will run for a day. Production acceptance requires 24 hours of useful
verified gameplay without manual recovery, plus intact ownership, checkpoints,
autosaves and monitoring. A live process, advancing game tick or green CI alone
does not satisfy that requirement. Record confidence holds, stale-evidence
rejections and provider failures separately from controller exceptions.
