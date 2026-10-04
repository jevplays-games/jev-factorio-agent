# Autonomous campaign supervision

`python -m jev_factorio.supervisor` supervises an already initialized fresh FLE
campaign. The launcher must record the fresh game's Unix start timestamp and
create its initial `rocket_launch` controller checkpoint. The watchdog never
initializes or resets a world.

Example (substitute the exact session, paths, and fresh-launch timestamp):

```sh
python -m jev_factorio.supervisor \
  --state-dir /absolute/campaign/supervision \
  --checkpoint /absolute/campaign/controller.json \
  --session-id SESSION_ID --started-at UNIX_TIMESTAMP \
  --duration-hours 12 --cwd /absolute/repository \
  --python /absolute/venv/bin/python \
  --repair-command-json '["/usr/local/bin/codex2","exec","-m","gpt-5.6-sol","-c","model_reasoning_effort=\"high\"","-s","danger-full-access","-"]'
```

The repair executable receives the saved prompt on stdin. No shell evaluates
the command. The process inherits its launcher's environment and existing Codex
configuration. Protect campaign logs and repair artifacts; they may contain
private game or repository data.

`supervisor.json` stores the immutable session, checkpoint, original cutoff,
process identity, phase, and repair attempt. Updates are atomic.
The last validated checkpoint and an immutable incident baseline survive corrupt
checkpoint writes. A persistent repair-required gate blocks gameplay across
restarts until an accepted repair; retries never trust a rejected repair's source
or checkpoint changes. Pending action, active plan, step index, and reservations
must match the original incident baseline. Failure budgets cannot decrease.
`events.jsonl` is an fsynced audit trail. A user-wide advisory lock at
`~/.jev-factorio-supervisor.lock` prevents concurrent watchdogs across checkouts.
Restart with the identical arguments;
changing the start or duration is rejected. Recovery kills the prior recorded
process group before proceeding, and refuses a reused leader PID.

Every gameplay launch uses FLE `--resume`, `--resume-controller`, hierarchical
hybrid policy, pinned model `jev-1.13.0`, and `rocket_launch`. Children receive
`TMPDIR=<repository>/runs/tmp` and `PYTHONPATH=<repository>/src`.
Remaining duration comes from the original
cutoff. Blocked/uncertain checkpoints, process exits (including exit zero without
completion), invalid checkpoint reads, and a stale checkpoint heartbeat trigger
repair. Completed campaigns stop successfully only when no live work remains in
the checkpoint. A completed status paired with a pending action, attempt, active
plan, reservation, transfer-recovery record, or acknowledged background job
and attempt is treated as unresolved checkpoint reconciliation and cannot return
success or launch gameplay. Retained attempt outcomes and audit history are
receipts; they do not keep an otherwise valid completion open. A changed source
with any such live work is routed to repair before gameplay resumes. The
background job and attempt remain intact for review. An explicitly authorized
compatible-source migration must still satisfy its own checkpoint-scope and
source-lineage contract; ordinary source-change detection does not infer that
authorization from a matching decision contract.

The watchdog kills and reaps the gameplay process before launching repair.
Repair and verification use bounded timeouts; failures back off exponentially up
to fifteen minutes until cutoff.
SIGINT/SIGTERM requests stop; at cutoff the watchdog kills its current process
group and reaps the leader. Descendants must remain in the inherited process
group; deliberately detached external services are outside this process boundary.
The existing Factorio server remains available as the preserved game session.

Repair prompts require preservation of FLE runtime handlers, entity identities,
and pending actions. A JSON result file must say `status: "repaired"`, identify
the exact `session_id` and absolute `checkpoint`, and provide a nonempty `evidence`
array. An exit-zero repair process alone never authorizes a restart.

Every repair prompt carries permanent fairness requirements: actual walking,
normal mining, standard interaction reach, and 1x game speed. Repairs must not
restore teleportation, fast movement/mining bypasses, remote interaction beyond
standard reach, scripted harvest or inventory grants, elapsed-time-only simulated
walking/mining, or game/player speed changes. Failures must be fixed in the fair
execution path, with focused regression tests and independent exact-head source
review of affected fairness behavior. A fairness regression or unresolved fairness
concern requires a blocked result, not permission to resume.

Future bug fixes belong in the repair loop: diagnosis, proposals, and unpublished
patches do not complete a code repair. The repair agent must finish the fix, tests,
independent review, publication/merge, and origin synchronization (plus fork when
configured) before the supervisor resumes within the original cutoff, session, and pending identity.
These fairness instructions are a repair-agent acceptance contract, not an
independent runtime fairness detector. Prompt tests verify that the instructions
remain present; they do not prove fair real-game behavior. Repair evidence must
distinguish source findings and mock tests from native observation evidence.

- `kind: "operational"` requires `operational_verified: true`, receipt/inventory/
  observation evidence, and unchanged tracked source and HEAD. Existing pending
  actions must remain byte-equivalent as JSON values for controller observation
  on resume. Setting status to running after a repair is allowed while retaining
  pending: controller verification precedes further dispatch. Failure budgets
  must not be silently erased. This result is an agent attestation; it is not
  native game proof.

  An ambiguous native placement may leave pending only after a resumed
  observation establishes that the intended role is absent, the player force
  has zero entities of the requested prototype, and all reserved materials
  remain in inventory. The controller records a plan failure and returns a
  non-mutating reconciliation step before it may plan another dispatch. Missing
  or conflicting evidence stays uncertain; acknowledgement alone never permits
  a replay. An ambiguous pipe or pole connection uses the same fail-closed rule:
  both endpoint roles must remain, the player force must have zero entities of
  the connector prototype, and all exactly reserved materials must remain. This
  deliberately cannot reconcile once any same-prototype connector already exists.
- `kind: "code"` requires all of `tests_passed`, `checks_passed`,
  `exact_head_reviewed`, `merged`, and `remotes_synced` true, a full `commit` SHA,
  and `pr_url`. The watchdog independently verifies local HEAD, origin main, and
  a configured fork main match that SHA, the PR is merged at that SHA, an approval refers to the
  exact PR head, no current reviewer requests changes, and checks succeeded.
  Alternatively `independent_review` names a JSON file inside the state directory,
  containing `head`, `verdict: "approved"`, `reviewer`, and nonempty
  `source_evidence`. Its reviewer ID must differ from the result's `repair_agent`.
  This permits an independent Codex review without bypassing branch rules.
  It also reruns `python -m pytest tests/` unless an explicit `prevalidation`
  artifact ID verifies a fresh same-host full-suite run for this exact merged
  commit and runtime. An invalid requested artifact blocks acceptance instead
  of silently running the suite during maintenance. See
  [deployment prevalidation](DEPLOYMENT_PREVALIDATION.md). Missing checks or reviews block restart.
  The worktree must be clean before and after tests, with HEAD still equal to the
  verified merge commit.

An optional `transfer_recovery` checkpoint reference supports one deliberately
narrow historical incident: an ambiguous insertion of 50 iron ore into a stone
furnace, with genuine observations showing five ore becoming four and exactly
one completed smelt. `transfer_recovery.build_recovery(memory, run_dir)` reads a
sealed canonical research run and returns a reference without changing state.
It binds the complete checkpoint (except status, reason, observation tick, and
the reference), complete log hashes, original Lua source, action identity, and
pre/post observations. It requires unchanged actor inventory and receipts, the
same bound actor and furnace, and no other dispatch, crafting, or input route.
The resumed controller repeats these checks against the current native state.
Conservation includes the ore already consumed by the in-progress smelt; an
exhausted, idle furnace may therefore have finished six additional plates.
It records `rejected_transfer_reconciled`, charges a plan failure, and returns
a nonmutating step before replanning. It never retries the old command or writes
a native receipt. A mismatch stays uncertain with the pending identity intact.
This is conservation evidence of no durable effect, not proof of a particular
error message. Unsupported transfers still require separate investigation.
Generate the reference only under stopped, audited supervision: the complete
checkpoint must match the immutable incident, its source revision must match
the canonical action, and no later gameplay launch or live controller may
exist. These are repair-handoff attestations, not facts inferred from a missing
receipt. The normal FLE resume installs the checked `factory.lua` through
`NativeFactory` before observing; historical source provenance is trusted from
the matching supervisor execution record. Arbitrary or unaudited logs do not
establish deployed-source provenance.

Only running/completed checkpoints with the original session can pass repair
validation. Code repair authorization permits focused commits, PRs, pushes,
guarded merges, and synchronization of origin plus any configured fork, never
force pushes, bypassed checks, leaked credentials, world resets, or erasure of ambiguous pending work.
The watchdog does not itself merge or push. It cannot independently prove that
free-text evidence describes a real observation or that a reviewer is independent;
those requirements remain part of the repair agent's acceptance contract.

Run isolated lifecycle tests with:

```sh
PYTHONPATH=src python -m pytest tests/test_supervisor.py
```

These tests use fake clocks/processes and do not qualify real-game play or remote
repair execution.
