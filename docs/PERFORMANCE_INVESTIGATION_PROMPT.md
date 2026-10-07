# Persistent Factorio Agent Optimization Investigation

Act as a persistent performance, gameplay-strategy, and reliability investigator for this repository:

- Canonical repository: https://github.com/jevplays-games/jev-factorio-agent
- Local workspace, when available: `/home/agent/factorio/jev-factorio`
- Related fork: https://github.com/CompleteDotTech/jev-factorio

Use the actual repository, not a generic agent architecture. Inspect its current branch, source, documentation, tests, and available run evidence. Refresh all historical claims before relying on them.

## Mission

Think deeply, investigate iteratively, and propose how this agent can achieve substantially better Factorio results with higher useful throughput, lower time-to-milestone, lower overhead, and fewer failures.

Optimize verified gameplay progress—not action count, model agreement, superficial code cleanliness, or a benchmark disconnected from the game.

This is an investigation and proposal task. Do not implement changes or create a PR yet. Your final step is to ask whether I want you to implement your recommended changes and create a PR.

## Start with the repository

Read applicable `AGENTS.md` instructions and inspect:

1. `README.md`, `docs/ARCHITECTURE.md`, `docs/BUILD_PLAN.md`, and `docs/HIERARCHICAL_CONTROLLER.md`.
2. `src/jev_factorio/main.py`, `loop.py`, and `controller.py` for execution, deadlines, selection, verification, and recovery.
3. `src/jev_factorio/planning/factory.py`, `catalog.py`, `materials.py`, and `goals.py` for production scheduling, native facts, resource accounting, and goal completion.
4. `src/jev_factorio/skills.py`, `factory_contract.py`, and `memory.py` for preconditions, reservations, receipts, pending actions, and checkpoints.
5. `src/jev_factorio/backends/fle.py`, `backends/native_factory.py`, and `lua/` for native execution, observation, transfers, fluid routing, and runtime lifecycle.
6. `src/jev_factorio/judgments.py`, `jev_client.py`, `state.py`, and `evaluation.py` for model decisions, payloads, telemetry, and evaluation.
7. `tests/test_factory.py`, the broader test suite, and `.github/workflows/tests.yml`.

When the local workspace contains them, inspect `runs/active-12-hour.json` and follow its actual run-directory reference to the checkpoint, decision stream, and agent log. Also inspect the native smelting and steam-power validation artifacts referenced by the documentation.

Run artifacts are intentionally untracked. If you only have a clone, explicitly identify missing evidence; do not invent results or assume a historical process is still running.

## Establish the baseline

Record the current source revision, dirty worktree, relevant dependency versions, configuration, and evidence provenance without exposing secrets.

If a campaign exists, determine its actual target, policy, process state, current milestone, pending action, recent verified progress, and deadline. A PID alone does not prove healthy progress.

Previous documentation reports native smelting and steam-power validation and an expanded test suite. Recheck those claims. Do not equate them with a successful full-game or rocket-launch run.

At prompt preparation, the local rocket campaign checkpoint had reached `uncertain` on a pending `factory_insert` of 20 automation science packs into `utility:lab`. Its observed player inventory still held the packs, the lab input was empty, and no matching transfer receipt was recorded. This is evidence of an unresolved dispatch outcome, not permission to replay it or proof of the underlying cause. Inspect the current checkpoint and dispatch diagnostics first; restoring reliable progress takes priority over optimizing an already-stalled loop.

Build a baseline that separates:

- Native gameplay outcomes.
- FLE tool-assisted behavior.
- Synthetic test results.
- Source-derived conclusions.
- Estimates and unresolved assumptions.

Measure what the evidence genuinely supports. Useful metrics include milestone latency, item/science throughput, machine utilization, starvation, blocked outputs, travel distance, transfer size, model-call frequency, fallback rate, observation overhead, stalls, and recovery time.

Do not infer wall-clock latency solely from simulation ticks unless the required assumptions are established. Identify missing instrumentation and propose how to obtain it.

## Investigate the highest-impact constraints

Inspect these repository-specific hypotheses rather than assuming they are the answer:

- `FactoryPlanner._need()` collects available outputs immediately. Does this cause excessive tiny transfers and repeated journeys?
- One role per recipe and sequential demand expansion may underuse parallel production. Where does this become the critical-path constraint?
- Raw-material gathering and solid transport are largely agent-driven. Would additional drills, belts, inserters, buffers, or better layouts outperform manual movement enough to justify construction cost?
- Current gathering, crafting, transfer, stack, and connection budgets may favor safety over throughput. Which bounds are essential, and which should become workload-aware?
- `_power()`, `_fuel()`, `_production()`, `_fluid()`, and `_research()` may benefit from capacity planning, maintenance scheduling, or coordinated upstream replenishment.
- Native observations and resource searches may repeat stable work. Quantify their cost before recommending caching or fewer observations.
- Hybrid policy can call Jev for a compiled candidate and then fall back after abstention. Determine where model judgment adds value and where routine deterministic execution is more efficient.
- Fluid branches, constrained inventories, partial transfers, and pending-action recovery can turn ordinary operational problems into terminal stalls.
- Runtime state, saved world state, controller checkpoints, and connected-player requirements have different persistence boundaries. Identify the exact reliability limitations.

Do not treat faster Python execution as the primary goal if travel, production capacity, or scheduling dominates elapsed time.

## Compare solutions

Consider at least:

1. A minimal, low-risk improvement to the current design.
2. A coordinated scheduling and production-capacity improvement.
3. A larger architectural alternative, if evidence warrants one.

Evaluate batching, transport consolidation, automated supply chains, parallel machines, layout, proactive maintenance, in-flight material accounting, adaptive polling, strategic versus routine model use, and durable recovery.

For each serious proposal, provide:

- The problem and supporting file/log evidence.
- The proposed mechanism.
- Expected benefit and confidence level.
- Dependencies and implementation scope.
- Correctness and operational risks.
- Tests and measurable acceptance criteria.
- Rollback approach.
- Why it is preferable to the alternatives.

Do not invent percentage improvements. Mark unmeasured gains as hypotheses and specify the experiment needed to validate them.

## Preserve correctness and operational safety

Protect native inventory conservation, entity-bound receipts, physical fluid/electricity checks, research prerequisites, connected-player guards, session identity, and native rocket-launch evidence.

Do not improve apparent results by creating resources, unlocking technologies, changing game speed, lowering verification standards, suppressing uncertainty, or presenting fallback decisions as successful model choices.

Default to source inspection, trace analysis, read-only telemetry, and non-mutating offline tests. The repository's normal test command is:

`PYTHONPATH=src .venv/bin/python -m pytest -q`

Verify that the environment exists before using it.

Do not initialize a live FLE backend merely to inspect it: initialization can reset a dedicated world. Do not reset the world, clear pending actions, replay ambiguous mutations, start a second controller, interrupt an active campaign, extend its deadline, or increase spending commitments.

Request approval before any live experiment requiring those actions. Continue safe investigation while waiting.

Do not modify production source, commit, push, create branches, open a PR, or merge during this investigation. You may write isolated analysis artifacts and checkpoints under `runs/performance-investigation/`, subject to applicable repository instructions. Preserve unrelated work and never expose `.env` contents or credentials.

## Work persistently

Maintain a durable checkpoint containing the baseline, evidence locations, findings, tested and rejected hypotheses, ranked proposals, unresolved questions, and next actions.

Resume from that checkpoint after interruptions. Do not restart the investigation from scratch or repeat the same shallow scan.

Proceed through repeated cycles of evidence collection, hypothesis formation, safe testing, and proposal refinement. Give concise progress updates when findings materially change.

Long-running does not mean unbounded. Respect existing deadlines and resource limits. If additional budget or authority is necessary, ask specifically rather than assuming it.

Continue until the recommended first change is implementation-ready and supported by a defensible validation plan. If a necessary conclusion is blocked, identify the missing evidence, explain what remains uncertain, and provide the strongest bounded proposal available.

## Final deliverable and approval gate

Deliver:

1. Executive recommendation.
2. Baseline and evidence limitations.
3. Ranked bottlenecks with source and run references.
4. Proposed changes, alternatives, benefits, and trade-offs.
5. A staged implementation plan.
6. Tests, benchmarks, native acceptance criteria, and rollback conditions.
7. The smallest coherent first PR: exact scope, exclusions, affected components, and dependencies.

Separate “implemented in source,” “tested synthetically,” and “verified natively.” Passing CI alone is not proof of improved gameplay performance or rocket completion.

Finish with this question:

“Would you like me to implement the recommended first-stage optimizations and create a PR for your review?”

Wait for my explicit approval before implementing or opening the PR. Do not merge a future optimization PR without separate authorization.
