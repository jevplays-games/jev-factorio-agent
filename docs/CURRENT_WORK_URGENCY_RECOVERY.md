# Immediate work hidden by speculative urgency

During the October 6 campaign, the controller repeatedly waited for JEV evidence
on an optional cable-assembler refill. Native tick 13364986 showed 20 carried
iron plates and 20 more in the owned iron furnace. The current objective was
20 automation-science packs; collecting those plates was an immediate input
step. Cable production belonged to the optional later workload.

The composed planner generated that pickup correctly. The later capital
frontier assigned urgency to the empty cable assembler and returned only urgent
plans. Its speculative refill therefore removed the immediate pickup from the
model's candidate set. The refill had no qualified dependency chain to red
science, so JEV correctly refused it. Boiler-fuel changes eventually broke the
wait, but that was incidental recovery rather than a durable fix.

When every urgent candidate is explicitly lookahead work, the frontier now keeps
executable immediate non-capital work alongside those candidates. This policy
lives in the capital planner, covered by the existing blob-bound decision
contract, so the source admission can distinguish the actual candidate change. Existing
immediate urgency keeps its priority. This neither accepts the speculative
refill nor chooses an action for JEV: native checks, cost/reservation gates,
failure history, model eligibility and strict choice confidence remain in force.

For efficient diagnosis and recovery:

1. Read owner liveness, checkpoint status, pending work and the last verified
   action. Preserve the campaign and original cutoff. Distinguish an advancing
   craft or lab from a foreground policy wait.
2. Retain the original accepted observation, matching request/response/decision
   and source revision. Compare the raw composed planner frontier with the final
   controller frontier; checking only the planner can miss this defect.
3. Inspect each removed candidate's work scope, urgency and native start
   evidence. An empty machine alone does not make its output relevant to the
   current objective. Do not fabricate that missing relationship.
4. Reproduce against the actual snapshot and compatible native catalog. The
   regression here fails on the old composed controller, preserves the original
   refusal, and checks that the restored pickup has qualified evidence within
   the real 48 KB model-request boundary. Catalog capture occurred later, so the
   test also checks that the researched technology set was unchanged.
5. Merge reviewed code after checks pass. Deploy the reviewed source with the
   established signed-owner and checkpoint-reconciliation procedure in
   [the status recovery runbook](STATUS_RECOVERY.md). Preserve every prior
   decision and failure. A genuine decision-contract change requires its normal
   explicit source-reevaluation admission; never reset or reroll a held input.
6. Verify a new JEV decision and native useful action, then inspect OBS Program
   and continued autosaves. A brief successful recovery does not establish
   multi-day reliability.
