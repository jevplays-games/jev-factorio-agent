# jev-factorio: Architecture

A Jev-powered Factorio agent. As far as we can tell (searches Sept 2026),
nobody has built a Factorio agent driven by TypeSafe's Jev - existing work
is all LLM code-synthesis agents. This would be a first.

## 1. Game state extraction - what exists

### Option A: Factorio Learning Environment (FLE) - recommended foundation
- Repo: https://github.com/JackHopkins/factorio-learning-environment
- Paper: https://arxiv.org/abs/2503.09617
- Docs: https://jackhopkins.github.io/factorio-learning-environment/
- **License: MIT** (LICENSE file, copyright 2025 FLE Contributors; GitHub's
  auto-detector labels it "Other" but the text is verbatim MIT)
- **Maturity: high.** ~1,026 stars, 20 contributors, active through 2026,
  latest release v0.4.3 (Apr 2026). Runs headless Factorio servers in
  Docker (`fle cluster start`), exposes a typed Python tool API
  (`place_entity`, `get_entities`, `nearest`, `inspect_inventory`, ...),
  has a public leaderboard, MCP support, and hosted eval configs. This is
  the framework used by the "Claude plays Factorio" research.
- Fit for us: FLE is built for LLM code-synthesis agents (agent writes a
  Python program per turn). We don't need the LLM part - we reuse its
  server lifecycle and tool functions as the observe/act layer, and put
  Jev where the LLM used to be. This is the fastest path to a real game.

### Option B: Factorio Play API mod
- Mod: https://mods.factorio.com/mod/factorio-agent-api
- Source: https://github.com/kovan/factorio-play-api
- **License: MIT**
- **Maturity: low.** v1.0.0, ~24 downloads, one author, Factorio 2.0.
  In-game `/agent` console commands (walk/mine/build/craft/inventory/put/
  take/rotate/deconstruct/recipe/wire), state written to
  `script-output/agent-gamestate.json` every second.
- Fit: useful as a thin reference for command design, and a plan-B for a
  local non-Docker install. Single-player only, immature schema.

### Option C: Raw modding API + RCON (build our own shim)
- Runtime API: https://lua-api.factorio.com/stable/index-runtime.html
- LuaRCON: https://lua-api.factorio.com/latest/classes/LuaRCON.html
- A custom Lua mod can serialize any game state to RCON/`script-output`
  and receive commands back. Maximum control, most work. Only worth it if
  FLE's abstractions fight us.

**Decision: build on FLE (Option A), keep the Play API mod command set as
the vocabulary for our macro actions.**

## 2. Decision loop design

For a single native `factory_connect` step with coherent current connector
ownership facts, the model receives the executor's additive construction
contract: dispatch surveys the corridor, reuses matching connectors and places
only missing pipes or poles from carried inventory. It does not remove or rebuild
existing factory entities. The disruption rubric includes connector placement;
approach, clearance and successful flow remain separate uncertainties. A planner
material allowance is not a surveyed placement count. Native preparation checks
and the model confidence floors still apply.

Factorio runs at 60 UPS, but it is a planning game: meaningful decisions
are macro-level (what to automate next, where to expand, what is blocked).
That matches Jev perfectly - 70-500 ms latency, typed answers, fractions of
a cent per call.

```
        ┌─────────────────────── every ~2 s ───────────────────────┐
        ▼                                                          │
  OBSERVE ──► DESCRIBE ──► JEV DECIDES ──► GATE ──► ACT ──► VERIFY
  snapshot    candidates    one batched     conf    macro   next observe
  (state)     from rules    /systemone call floor  action   closes loop
```

1. **Observe**: backend returns a `GameSnapshot` (position, inventory,
   nearby resources, placed entities, power, alerts). Raw entity dumps are
   filtered here - Jev's context is 32k tokens, so the snapshot is compact.
2. **Describe**: `questions.py` builds typed questions. Crucially, hard
   game rules filter the candidate-action list *before* Jev sees it -
   Jev always picks a winner even when no option fits, so impossible
   actions must never be in the option set.
3. **Jev decides** (one HTTP call, speculative fan-out):
   - `goal` - **Choice** over macro goals (bootstrap mining / stockpile
     fuel / automate smelting / hold)
   - `next_action` - **Choice** over the filtered candidate actions
   - `is_stuck` - **Noul** (probability the current plan can't progress;
     drives plan invalidation)
   - `urgency` - **Score** (fine / adjust soon / change immediately;
     drives whether to cut a running action short)
4. **Gate**: Choice answers carry `confidence` (0-1 from the probability
   distribution). Below `JEV_CONFIDENCE_FLOOR` (default 0.45) a scripted
   fallback policy acts instead - confidence-gated routing per TypeSafe's
   own pattern docs.
5. **Act**: the backend maps the chosen macro action to game commands.
   Micro-control (walking step by step, placing exactly) stays in code;
   Jev only picks *what*, code owns *how*.
6. **Verify**: next observation confirms the action landed (drill placed,
   inventory changed). Failures go into `alerts`, which the next Jev call
   sees as state.

### Native item transfers

For ordinary factory roles, `NativeFactory` approaches the entity and calls the
bundled Lua `campaign.transfer` endpoint inside a `transfer_rpc` phase. The
endpoint uses explicit inventories: inserts draw from the bound character's
main inventory; extracts draw from the machine output inventory (or its chest
inventory fallback) and deliver to character main. Coal sent to a burner goes
to its fuel inventory before the furnace-source rule; other furnace ingredients
go to furnace source, lab items to lab input, assembler and rocket-silo items to
their input inventory, and other burner items to fuel. The qualified bootstrap
output extractor is a separate endpoint.

Lua checks reach, source quantity, and full requested destination capacity
before removing or inserting items. A short destination therefore causes no
inventory mutation and creates no receipt at that endpoint. A later partial
insert attempts to refund the unaccepted amount; when that refund succeeds, Lua
records the inserted quantity in the receipt and then raises. A failed refund
can happen before receipt creation, so an absent receipt after dispatch is not
proof of no effect. The Python controller does not yet expose capacity
rejection as a typed native result: generic transfer errors remain ambiguous
until exact evidence permits reconciliation. See [prepared transfer
reconciliation](prepared-transfer-reconciliation.md) for the current recovery
rules. Typed capacity classification and transfer-budget followups are tracked
separately in [#478](https://github.com/jevplays-games/jev-factorio-agent/issues/478)
and [#479](https://github.com/jevplays-games/jev-factorio-agent/issues/479);
this description makes no native-acceptance claim. The endpoint is in
[`factory.lua`](../src/jev_factorio/lua/factory.lua#L253-L303), its ordinary
adapter call is in [`NativeFactory`](../src/jev_factorio/backends/native_factory.py#L387-L408),
and the controller's receipt checks are in [`controller.py`](../src/jev_factorio/controller.py#L1228-L1310).

### Frequency and cost
- Cadence: one decision every ~2 s game time; slow to 5-10 s when
  `urgency` is "fine as-is" and nothing changed, wake immediately on
  alerts. Never per-tick.
- Cost: state + questions ~1-2k input tokens/call; output tokens are not
  charged. At 1 Hz that's ~3.6M tokens/hr worst case - about **$0.15/hr**
  at Vercel AI Gateway pricing ($0.042/1M input tokens). Real cadence is
  much lower. Prepaid credits cap spend by construction.

## 3. Jev API facts (verified against docs, Sept 2026)

- Endpoint: `POST https://api.typesafe.ai/v1/systemone`,
  `Authorization: Bearer $TYPESAFE_API_KEY`, model alias `jev-latest`.
- Request: `{state, model, questions: {id: {type, instructions, criteria}}}`.
  `state` may be structured JSON. `instructions` may itself be an object
  with data fields referenced in backticks.
- Question types: **Noul** (yes/no, returns probability 0-1), **Choice**
  (one option from a criteria map, max 255 options, returns choice + full
  probability distribution + confidence), **Score** (ordered levels, 2-10,
  returns probability-weighted score + legend + confidence).
- Response: `{model, answers: {id: {...}}, usage}` - answers keyed by the
  caller's question ids.
- Errors: 401 (key), 422 (bad question), 429 / 529 (back off; SDK retries
  automatically).
- SDKs: Python `pip install typesafe-sdk`
  (`TypeSafeClient` / `AsyncTypeSafeClient`, `Choice`/`Score`/`Noul`
  types, reads `TYPESAFE_API_KEY`), and a JavaScript/TypeScript SDK.
- Access: API keys come from the TypeSafe console/dashboard; access is
  waitlist-gated (per our research Sept 19). **Alternative with no
  waitlist: Vercel AI Gateway**, model id `typesafe-ai/jev`,
  $0.042/1M input tokens, 32k context, no output-token charge, callable
  from the Vercel AI SDK (`experimental_evaluate`) or HTTPS. Jev is also
  on Cloudflare Workers AI as `typesafe/jev`.
- Docs index: https://docs.typesafe.ai/llms.txt

## 4. MVP scope

Smallest thing that proves the loop end-to-end: **Jev bootstraps the
first burner mining drill.**

1. Fresh map, player with 1 burner drill in inventory.
2. Loop runs: Jev chooses walk-to-coal, mine, walk-to-iron, place drill,
   fuel drill - from typed questions over live state.
3. Done when `burner-mining-drill` is `WORKING` and ore is accumulating.
   (This mirrors the first rung of FLE's own eval ladder, so difficulty is
   calibrated.)

Even smaller offline proof: the repo's `--backend mock` runs the full
observe->decide->act loop with a simulated world and a mock Jev client -
no game, no key, no spend. That runs today.

## 5. Risks

- **Jev always picks a winner.** Mitigated by pre-filtered candidate sets
  and the confidence gate; still the #1 correctness risk. Log every
  decision for review.
- **Jev can't explain itself.** Debuggability comes from logged state +
  distributions, not rationales.
- **Waitlist.** If no TypeSafe key is available, route through Vercel AI
  Gateway (`typesafe-ai/jev`) - same model, no waitlist.
- **FLE coupling.** FLE is research code; pin the version (v0.4.3) and
  keep our backend interface narrow so we can drop to Option B/C.
- **Long-horizon planning.** Jev is System One: no memory, no reasoning
  chains. Long-horizon coherence must come from code (goal stack, plan
  bookkeeping in `state.py`), with Jev re-deciding from current state.
- **Latency variance** (70-500 ms): fine at 0.1-0.5 Hz, unusable for
  twitch control - combat/defense needs a scripted reflex layer.

Science-consuming research prepares and inserts its native science ingredients
before selecting a technology on an idle force. Selecting an empty lab only queues
research. The bounded selection proof instead binds a coherent same-session
observation and matching native catalog version to the powered lab, its unit and
network identity, and inputs covering one research unit. This is current start
readiness, not proof of later science consumption, research progress, or an unlock;
those outcomes still require native verification and all independent model gates.

The preceding lab-input transfer retains its selected native technology and
ingredient demand. Its bounded proof binds the current owned lab, missing science
input, paid actor inventory, remaining native bill, and unused same-tick receipt
query. Compact observations disclose the receipt-map count rather than copying
receipt history. Supplying packs is an immediate preparation step; it does not
establish research selection, consumption, progress, or completion, and neither
the proof nor its factual judgment guidance replaces the independent model gates.

## Sources

- FLE repo: https://github.com/JackHopkins/factorio-learning-environment
- FLE paper: https://arxiv.org/abs/2503.09617
- FLE docs: https://jackhopkins.github.io/factorio-learning-environment/
- FLE LICENSE (MIT): https://github.com/JackHopkins/factorio-learning-environment/blob/main/LICENSE
- Factorio Play API mod: https://mods.factorio.com/mod/factorio-agent-api
- Factorio runtime API: https://lua-api.factorio.com/stable/index-runtime.html
- LuaRCON: https://lua-api.factorio.com/latest/classes/LuaRCON.html
- TypeSafe API reference: https://docs.typesafe.ai/api.md
- TypeSafe primitives: https://docs.typesafe.ai/primitives.md
- TypeSafe Python SDK: https://docs.typesafe.ai/sdk/python.md
- TypeSafe patterns (fan-out, confidence routing): https://docs.typesafe.ai/patterns.md
- TypeSafe quickstart: https://docs.typesafe.ai/introduction/quickstart.md
- Vercel AI Gateway, Jev model: https://vercel.com/ai-gateway/models/jev
- Vercel guide, Jev + AI SDK: https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk
- Cloudflare Workers AI, Jev: https://developers.cloudflare.com/ai/models/typesafe/jev/


Paid same-cell service visits retain individually verified native transfers. A
current owned-furnace ingredient insert followed by carried coal can expose a
separate first-step start witness after exact service-policy recompilation. Its
private snapshot-local admission records the planner's actual carried ledger;
a serialized marker cannot create that admission. Current supply limits, native
recipe edges, ownership and unused receipts still qualify the witness. This
states a bounded recipe-input contribution, not fuel-starvation removal, later
output or science completion. Receiver capacity, current reservations and each
step's native preconditions and postconditions remain execution checks.
