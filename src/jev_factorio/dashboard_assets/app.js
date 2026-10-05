"use strict";

const $ = (id) => document.getElementById(id);
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const array = (value) => Array.isArray(value) ? value : [];
const text = (value, fallback = "—") => ["string", "number", "boolean"].includes(typeof value) ? String(value) : fallback;
const number = (value, places = 2) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(places) : "—";
const short = (value) => typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat(undefined, {notation: "compact", maximumFractionDigits: 1}).format(value) : "—";
const set = (id, value) => { $(id).textContent = text(value); };
const el = (tag, className, content) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = text(content);
  return node;
};
// [title, viewer subtitle, inspector description]
const STAGES = [
  ["Supervisor", "Keeps the run on schedule","A separately supplied supervisor.json reports campaign state. A running dashboard is not evidence of a supervisor lock."],
  ["Controller", "Reads the game, commits steps", "Existing validated observations and controller state publications. Opening this viewer never creates an observation or game action."],
  ["Goals", "Fuel → mining → rocket", "stockpile_fuel → bootstrap_mining → target. Completion is read from the controller's verified goal predicates, never inferred from a score."],
  ["Planner", "Works out what to build next", "The deterministic compiler prepares bounded executable prerequisites. Candidate detail is available from the actual model request or committed plan; unobserved alternatives stay unknown."],
  ["JEV decides", "Scores the options, picks one", "Observed question batches, provider answers, duration and the controller's final selection. Raw provider answers are not accepted decisions. Utility is not confidence."],
  ["Act", "Sends the action to the game", "An existing backend call is in progress or has returned. Return acknowledgment alone does not prove the expected game effect."],
  ["Verify", "Checks the game really changed", "Prepared and ambiguous actions remain pending until the original controller verifies their effects. This viewer cannot clear, replay, or reconcile them."],
  ["Repair", "Codex fixes a stuck run", "Repair state is read from the matching supervisor. The dashboard has no repair, approval, merge, restart, or game-control endpoints."]
];

let userNotice = "";
let telemetryNotice = "";
let feedNotice = "";
let latest = null;
let displayed = null;
let renderedKey = "";
let connected = false;
let frozen = false;
let receivedAt = 0;
let stream = null;
let mediaGeneration = 0;
let mediaBusy = false;
let sourceName = "NO SOURCE";
let broadcast = new URLSearchParams(location.search).get("overlay") === "1";
const studio = new URLSearchParams(location.search).get("studio") === "1";
document.body.classList.toggle("studio", studio);
if (studio) {
  $("video-status").textContent = "OBS COMPOSITION";
  $("video-resolution").textContent = "Video is supplied by a separate OBS source";
}

function notice(message, persistent = true) {
  if (persistent) userNotice = message || "";
  else telemetryNotice = message || "";
  const combined = [userNotice, telemetryNotice, feedNotice].filter(Boolean).join(" ");
  $("notice").hidden = !combined;
  set("notice", combined);
}
function inspect(title, description, evidence) {
  set("inspector-title", title);
  set("inspector-description", description);
  set("inspector-content", JSON.stringify(evidence ?? {available: false}, null, 2));
  if (!$("inspector").open) $("inspector").showModal();
}
function stageEvidence(index) {
  const data = object(displayed);
  const v = object(data.view);
  return [data.supervisor, {state: v.state, status: v.status, invocation: v.run_id},
    {goal: v.goal, target: v.target, completed_goals: v.completed_goals}, {plan: v.plan, candidates: object(v.request).candidates},
    {request: v.request, response: v.response, accepted_decision: v.decision, latency_ms: v.model_ms},
    {action: v.action, parameters: v.parameters, pending: v.pending},
    {pending: v.pending, verified: v.verified, outcome: v.outcome, state: v.state}, data.supervisor][index];
}

STAGES.forEach(([title, subtitle, description], index) => {
  const node = el("button", "workflow-node");
  node.id = `stage-${index + 1}`;
  node.append(el("span", "node-index", String(index + 1).padStart(2, "0")));
  const body = el("span", "node-text");
  body.append(el("strong", "", title), el("small", "", subtitle));
  node.append(body);
  node.addEventListener("click", () => inspect(title, description, stageEvidence(index)));
  $("workflow").append(node);
});

const MILESTONE_STATES = new Set(["done", "next", "pending"]);

function renderMilestones(rows, v) {
  const tree = $("goals");
  tree.classList.add("milestones");
  // Long ladders (Space Age) fold earlier finished rows into one so the column still fits.
  let list = rows.slice(0, 64).map(object);
  const limit = studio ? 11 : 24;
  let folded = 0;
  if (list.length > limit) {
    const pending = list.findIndex((row) => row.state !== "done");
    folded = Math.min(Math.max(0, (pending < 0 ? list.length : pending) - 2), list.length - limit + 1);
    if (folded > 1) list = list.slice(folded); else folded = 0;
  }
  const summary = [];
  if (folded) {
    const node = el("div", "goal-node done goal-summary");
    node.append(el("strong", "", `✓ ${folded} earlier`), el("small", "", "Completed"));
    summary.push(node);
  }
  tree.replaceChildren(...summary, ...list.slice(0, limit - summary.length).map((row) => {
    const state = MILESTONE_STATES.has(row.state) ? row.state : "pending";
    const node = el("div", `goal-node${state === "done" ? " done" : state === "next" ? " current" : ""}`);
    node.dataset.milestone = text(row.key, "unknown");
    const active = row.kind === "goal" && state !== "done" && row.key === v.goal;
    const detail = row.start === true ? "From start" : state === "done"
      ? row.kind === "research"
        ? typeof row.tick === "number" ? `Seen tick ${row.tick}` : "Researched"
        : `Verified tick ${text(row.tick)}`
      : active ? row.key === v.target ? "Active target" : "Active prerequisite"
      : state === "next" ? "Next milestone" : "Not yet";
    node.append(el("strong", "", text(row.title, text(row.key))), el("small", "", detail));
    return node;
  }));
  const done = rows.filter((row) => object(row).state === "done").length;
  $("goals-count").textContent = `${done} / ${rows.length}`;
}

// Science-pack colours for packs without an icon; unknown (modded) packs stay neutral.
const PACK_COLOURS = {
  "automation-science-pack": "#d8483f", "logistic-science-pack": "#57b847", "military-science-pack": "#8d8f93",
  "chemical-science-pack": "#3f9fdc", "production-science-pack": "#9a5fd0", "utility-science-pack": "#e2c33d",
  "space-science-pack": "#e8e8e8", "metallurgic-science-pack": "#e2862f", "electromagnetic-science-pack": "#d352b8",
  "agricultural-science-pack": "#a3c93a", "cryogenic-science-pack": "#5cc6e6", "promethium-science-pack": "#6d4f86",
};
const TRIGGERS = {"craft-item": "Craft", "craft-fluid": "Make", "mine-entity": "Mine", "build-entity": "Build",
  "send-item-to-orbit": "Launch", "capture-spawner": "Capture", "create-space-platform": "Create a space platform",
  "scripted": "Scripted"};
const words = (value) => String(value).replaceAll("-", " ").replaceAll("_", " ");

function triggerText(trigger) {
  trigger = object(trigger);
  const verb = TRIGGERS[trigger.type] || words(text(trigger.type, "Trigger"));
  const target = trigger.item || trigger.entity || trigger.fluid;
  const count = typeof trigger.count === "number" && trigger.count > 1 ? `${trigger.count} × ` : "";
  return target ? `${verb} ${count}${words(target)}` : verb;
}

function packChip(tier, state) {
  const chip = el("li", state);
  const pack = text(tier.pack, "unknown");
  chip.dataset.pack = pack;
  chip.title = `${text(tier.title, pack)}: ${text(tier.done)} of ${text(tier.total)} researched on the path`;
  const dot = el("span", "pack-dot");
  dot.style.setProperty("--pack", PACK_COLOURS[pack] || "#8b8674");
  if (/^[a-z0-9][a-z0-9-]{0,63}$/.test(pack)) {
    const icon = document.createElement("img");
    icon.alt = "";
    icon.src = `/icons/${pack}.png`;
    icon.addEventListener("error", () => icon.replaceWith(dot), {once: true});
    chip.append(icon);
  } else chip.append(dot);
  chip.append(el("b", "", `${text(tier.done)}/${text(tier.total)}`));
  return chip;
}

function renderResearch(research) {
  research = object(research);
  const strip = $("research-strip");
  strip.hidden = research.status !== "ok";
  if (strip.hidden) return;
  const current = object(research.current);
  const trigger = current.trigger && typeof current.trigger === "object";
  set("research-current", current.name ? text(current.title, current.name) : "Nothing queued");
  const progress = typeof current.progress === "number" && current.progress >= 0 && current.progress <= 1 ? current.progress : null;
  $("research-bar-track").hidden = !current.name || trigger;
  $("research-bar").style.width = `${Math.round((progress || 0) * 100)}%`;
  set("research-progress", !current.name ? "" : trigger ? triggerText(current.trigger) : progress === null ? "—" : `${Math.floor(progress * 100)}%`);
  const tiers = Array.isArray(research.tiers) ? research.tiers.map(object) : [];
  const activePack = current.pack;
  let active = tiers.findIndex((tier) => tier.pack === activePack && tier.done < tier.total);
  if (active < 0) active = tiers.findIndex((tier) => tier.done < tier.total);
  // Long ladders (Space Age) collapse finished tiers and show a window around the active one.
  let first = 0;
  if (tiers.length > 7 && active > 1) first = Math.min(active - 1, tiers.length - 7);
  const chips = [];
  if (first > 0) {
    const done = el("li", "done", `✓ ${first}`);
    done.title = `${first} earlier science tiers`;
    chips.push(done);
  }
  tiers.slice(first, first + 7).forEach((tier, offset) => {
    const index = first + offset;
    chips.push(packChip(tier, tier.done >= tier.total ? "done" : index === active ? "active" : index > active ? "locked" : ""));
  });
  if (first + 7 < tiers.length) chips.push(el("li", "locked", `+${tiers.length - first - 7}`));
  $("research-tiers").replaceChildren(...chips);
  set("research-goal", `TO ${text(research.goal, "GOAL").toUpperCase()}`);
  set("research-path", `${text(research.path_done)} / ${text(research.path_total)}${typeof research.available === "number" ? ` · ${research.available} open` : ""}`);
}

function renderGoals(v) {
  const completed = object(v.completed_goals);
  const target = typeof v.target === "string" ? v.target : "rocket_launch";
  if (target === "rocket_launch" && Array.isArray(v.milestones) && v.milestones.length) {
    renderMilestones(v.milestones, v);
    return;
  }
  $("goals").classList.remove("milestones");
  $("goals-count").textContent = "GOAL TREE";
  const path = target === "bootstrap_mining" ? ["stockpile_fuel", target] : ["stockpile_fuel", "bootstrap_mining", target];
  const nodes = path.map((goal) => {
    const done = Object.hasOwn(completed, goal);
    const active = !done && goal === v.goal;
    const node = el("div", `goal-node${done ? " done" : active ? " current" : ""}`);
    node.append(el("strong", "", goal), el("small", "", done ? `Verified · tick ${text(completed[goal])}` : active ? goal === target ? "Active target" : "Active prerequisite" : "Not yet verified"));
    return node;
  });
  $("goals").replaceChildren(...nodes);
}

function renderCandidates(v) {
  const focusedCandidate = document.activeElement?.dataset.candidateId;
  const tableScroll = $("candidates").closest(".table-scroll");
  const scrollTop = tableScroll.scrollTop;
  const decision = object(v.decision);
  const request = object(v.request);
  const candidates = object(request.candidates);
  const answers = object(decision.answers);
  const rawAnswers = object(object(v.response).answers);
  const accepted = Object.keys(answers).length > 0;
  const utilities = object(decision.utilities);
  const plan = object(v.plan);
  let entries = Object.entries(candidates).filter(([, value]) => value && typeof value === "object");
  if (!entries.length && typeof plan.id === "string") entries = [[plan.id, plan]];
  const legacy = displayed?.source?.mode === "legacy";
  $("candidate-table").hidden = legacy && !entries.length;
  $("recorded-actions").hidden = !legacy || Boolean(entries.length);
  if (legacy && !entries.length) {
    const actions = array(displayed.events).filter((event) => event.action).slice(-5).reverse();
    $("recorded-actions").replaceChildren(el("p", "evidence-caption", "Recorded actions · not a live execution phase"), ...actions.map((event) => {
      const row = el("div", "recorded-action");
      row.append(el("span", "mono", `tick ${text(event.tick)}`), el("strong", "", acting(event.action)),
        el("span", "", event.outcome ? Explain.event(event).text : event.verified === true ? "Postcondition verified" : "Effect not verified in this record"));
      row.title = text(event.outcome, "");
      return row;
    }));
  }
  set("candidate-count", !entries.length && legacy ? "UNAVAILABLE IN LEGACY LOG" : `${entries.length} ${Object.keys(candidates).length ? "MODEL CANDIDATES" : "KNOWN PLANS"}`);
  const rows = entries.slice(0, 16).map(([id, candidate]) => {
    candidate = object(candidate);
    const selected = id === decision.plan_id || id === plan.id;
    const row = el("tr", selected ? "selected" : "");
    const title = el("td");
    const button = el("button");
    button.dataset.candidateId = id;
    button.append(el("span", "candidate-title", text(candidate.description, id)), el("span", "candidate-id", id));
    button.addEventListener("click", () => inspect(id, "Captured candidate; quantities and parameters are evidence, not dashboard controls.", candidate));
    title.append(button);
    const a = accepted ? answers : rawAnswers;
    const status = selected ? "COMMITTED" : Object.hasOwn(utilities, id) ? "ELIGIBLE" : accepted ? "NOT ELIGIBLE" : "NOT ACCEPTED";
    row.append(title, el("td", "", number(object(a[`${id}/benefit`]).score)),
      el("td", "", number(object(a[`${id}/disruption`]).score)),
      el("td", "", number(object(a[`${id}/needs_observation`]).noul)),
      el("td", "", number(object(object(a.candidate).probabilities)[id])),
      el("td", "", `${number(object(a[`${id}/benefit`]).confidence)} / ${number(object(a[`${id}/disruption`]).confidence)}`),
      el("td", "utility", number(utilities[id], 3)));
    const state = el("td"); state.append(el("span", "tag", status)); row.append(state);
    return row;
  });
  if (!rows.length) {
    const row = el("tr");
    const cell = el("td", "empty", legacy ? "Candidate definitions were not captured by this legacy log." : "No candidate batch in this cycle. Deterministic alternatives are not invented.");
    cell.colSpan = 8; row.append(cell); rows.push(row);
  }
  $("candidates").replaceChildren(...rows);
  tableScroll.scrollTop = scrollTop;
  if (focusedCandidate !== undefined) {
    Array.from($("candidates").querySelectorAll("button")).find((button) => button.dataset.candidateId === focusedCandidate)?.focus({preventScroll: true});
  }
  set("selected-plan", text(plan.id, text(decision.plan_id, "No current committed plan")));
  set("selection-source", `${text(decision.source, "No new selection")} · ${Array.isArray(plan.steps) ? plan.steps.length + " bounded steps" : "no active plan"}${decision.reason ? " · " + text(decision.reason) : ""}`);
}

function renderLog(data) {
  const filter = $("event-filter").value;
  const events = array(data.events);
  const selected = events.filter((event) => filter === "all" || String(event.stage) === filter).slice(-120).reverse();
  const legacy = data.source?.mode === "legacy";
  const rows = selected.map((event) => {
    if (legacy) {
      const said = Explain.event(event);
      const row = el("div", `event-row plain ${said.state}`);
      const line = el("span", "event-kind");
      if (said.item) line.append(icon(said.item));
      line.append(document.createTextNode(said.text));
      row.append(el("span", "event-time", `tick ${text(event.tick)}`), el("span", "event-state", {done: "✓", pending: "…", wait: "·"}[said.state]), line);
      row.title = text(event.outcome, "");
      return row;
    }
    const kind = text(event.kind, "unknown");
    const row = el("div", `event-row${kind.endsWith("failed") ? " failed" : ""}`);
    const date = new Date(Number(event.time) * 1000);
    const timestamp = legacy ? `tick ${text(event.tick)}` : event.time == null || Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString([], {hour12: false});
    const outcome = text(event.outcome, event.verified === true ? "postcondition verified" : "effect not verified in this record");
    row.append(el("span", "event-time", timestamp), el("span", "event-stage", legacy ? "RECORDED" : `0${event.stage} / ${["", "SUP", "CONTROLLER", "GOALS", "PLANNER", "JEV", "NATIVE", "VERIFY", "REPAIR"][event.stage] || "UNKNOWN"}`), el("span", "event-kind", legacy ? `${text(event.action, "Decision")} · ${outcome}` : kind.replaceAll("_", " ") + (event.action ? ` · ${text(event.action)}` : "")), el("span", "event-duration", typeof event.duration_ms === "number" ? `${number(event.duration_ms, 1)} ms` : ""));
    return row;
  });
  const scroll = $("event-log").scrollTop;
  $("event-log").replaceChildren(...(rows.length ? rows : [el("p", "empty", "No matching captured events. No simulated activity is displayed.")]));
  $("event-log").scrollTop = scroll;
  set("event-count", `${events.length} recent events`);
}

// Item icons come from the local game install via /icons; without it the name is shown.
function icon(name) {
  const image = el("img", "item-icon");
  image.alt = "";
  image.src = `/icons/${Explain.item(name) || "unknown"}.png`;
  image.addEventListener("error", () => { image.hidden = true; image.parentElement?.classList.add("no-icon"); }, {once: true});
  return image;
}

let knownTechs = null;
const newItems = new Map();
function renderInventory(state) {
  const inventory = object(state.inventory);
  const researched = array(state.researched);
  if (!Object.keys(inventory).length && !researched.length) {
    const empty = el("div", "slot empty-slot"); empty.append(el("span", "slot-name", "Inventory"), el("strong", "", "Awaiting native state"));
    $("inventory").replaceChildren(empty);
    return;
  }
  const {items, byTech} = Explain.unlocked(researched);
  const now = performance.now();
  if (Array.isArray(state.researched)) {
    // Only research that completes while the page is open is announced as newly unlocked.
    if (knownTechs) for (const tech of researched) if (!knownTechs.has(tech)) for (const name of byTech[tech] || []) newItems.set(name, now + 90000);
    knownTechs = new Set(researched);
  }
  const held = Object.keys(inventory).filter((name) => Explain.item(name) && typeof inventory[name] === "number" && inventory[name] > 0);
  let slots = [...items, ...held.filter((name) => !items.includes(name))];
  const capacity = Math.max(6, Math.floor(($("inventory").clientWidth || 1340) / 64));
  while (slots.length > capacity) {
    const spare = slots.findLastIndex((name) => !held.includes(name));
    slots.splice(spare >= 0 ? spare : slots.length - 1, 1);
  }
  $("inventory").replaceChildren(...slots.map((name) => {
    const count = typeof inventory[name] === "number" ? inventory[name] : 0;
    const fresh = (newItems.get(name) || 0) > now;
    const slot = el("div", `slot${count ? "" : " zero"}${fresh ? " new" : ""}`);
    slot.title = `${Explain.words(name)}: ${count}${fresh ? " (newly unlocked)" : ""}`;
    slot.append(icon(name), el("span", "slot-name", Explain.words(name)), el("strong", "", short(count)));
    return slot;
  }));
}

const ACTING = {factory_insert: "loading items", factory_extract: "collecting items", factory_gather: "gathering",
  factory_craft_job: "crafting", factory_place: "building", factory_connect: "connecting", factory_research: "starting research"};
function acting(action) { return ACTING[action] || (typeof action === "string" ? Explain.words(action) : "last action"); }

// The most recent recorded workflow boundary, for legacy logs without live stage events.
function lastStage(v) {
  if (Object.keys(object(v.pending)).length || v.action === "observe" || v.action === "verify") return 7;
  if (typeof v.action === "string" && v.action) return 6;
  return v.model_call === true ? 5 : 0;
}

function snapshotKey(data) {
  return JSON.stringify([data.view, data.events, data.source, data.supervisor]);
}

function render(data) {
  displayed = data;
  renderedKey = snapshotKey(data);
  document.body.classList.toggle("legacy-feed", data.source?.mode === "legacy");
  const v = object(data.view);
  const state = object(v.state);
  const supervision = object(data.supervisor);
  const sup = supervision.session_match ? object(supervision.state) : {};
  set("session", text(state.session_id, text(v.session_id, "Awaiting telemetry")));
  set("world", state.world_kind === "mock" ? "MOCK WORLD · not native gameplay" : text(state.world_kind, "No world observed"));
  set("policy", v.policy);
  set("tick", `tick ${text(state.tick, text(v.tick))}`);
  set("controller-status", text(v.status, "UNKNOWN").toUpperCase());
  set("run-id", `Invocation ${text(v.run_id, "not observed")}`);
  set("overlay-goal", text(v.goal, "Awaiting observation"));
  set("overlay-status", text(v.status, "NO TELEMETRY").toUpperCase() + (state.world_kind === "mock" ? " / MOCK" : ""));
  set("source-mode", frozen ? "DISPLAY FROZEN" : data.source?.mode === "legacy" ? "LEGACY / COMPLETED DECISIONS" : "READ-ONLY / EVENT FEED");
  MissionControl.render(data, inspect);
  renderGoals(v);
  renderResearch(v.research);
  const position = array(state.player_position);
  const chest = state.drill_output_connected;
  const observed = [
    ["Player at", position.length === 2 && position.every((n) => typeof n === "number") ? `x ${Math.round(position[0])}, y ${Math.round(position[1])}` : "—"],
    ["Starter drill", "drill_status" in state ?Explain.drill(state.drill_status) : "—", "The first burner mining drill JEV placed"],
    ["Drill fuel", typeof state.drill_fuel === "number" ? `${state.drill_fuel} coal` : "—"],
    ["Drill output chest", chest === true ? "In place" : chest === false ? "None yet" : "—", "A chest under the drill catches its ore"],
    ["Ore in that chest", chest === false ? "n/a" : text(state.iron_ore_collected)],
    ["Research done", Array.isArray(state.researched) ? `${state.researched.length} techs` : "—"],
  ];
  $("observations").replaceChildren(...observed.map(([key, value, hint]) => { const row = el("div"); const term = el("dt", "", key); if (hint) term.title = hint; row.append(term, el("dd", "", value)); return row; }));
  renderInventory(state);
  const latency = el("span", "", " ms"); $("latency").replaceChildren(document.createTextNode(number(v.model_ms, 0)), latency);
  const currentResponse = v.response && typeof v.response === "object" && !Array.isArray(v.response)
    ? object(v.response) : null;
  const usage = currentResponse ? object(currentResponse.usage) : object(v.usage);
  set("tokens", short(usage.total_tokens ?? (typeof usage.input_tokens === "number" && typeof usage.output_tokens === "number" ? usage.input_tokens + usage.output_tokens : null)));
  renderCandidates(v);
  STAGES.forEach((_, index) => {
    const node = $(`stage-${index + 1}`);
    const active = index === 0 ? Boolean(sup.phase) : index === 7 ? sup.repair_required === true || sup.phase === "repair" : data.source?.mode !== "legacy" && v.stage === index + 1 && v.lifecycle !== "returned" && v.lifecycle !== "error";
    node.classList.toggle("active", active);
    node.classList.toggle("seen", array(v.seen).includes(index + 1));
    node.classList.toggle("last", data.source?.mode === "legacy" && lastStage(v) === index + 1);
  });
  const pending = object(v.pending);
  const hasPending = Object.keys(pending).length > 0;
  set("verification-status", hasPending ? `Pending: ${acting(pending.action)}` : v.verified === true ? "Confirmed by the game" : "Nothing to confirm right now");
  set("verification-detail", hasPending ? (pending.dispatch === "ambiguous" ? "The game didn't acknowledge the action cleanly. JEV watches for its effect before changing anything else." : "Sent to the game. JEV watches for its effect before moving on.") : v.verified === true ? Explain.event(v).text : "Returned ≠ verified. Only an observed change in the game advances a step.");
  const tick = typeof state.tick === "number" ? state.tick : v.tick;
  const waited = hasPending && typeof pending.started_tick === "number" && typeof tick === "number" ? Math.max(0, Math.round((tick - pending.started_tick) / 60)) : null;
  $("pending-check").classList.toggle("active", hasPending);
  set("pending-polls", hasPending ? `Checking${waited === null ? "" : ` · ${waited}s`}${typeof pending.polls === "number" ? ` · ${pending.polls} ${pending.polls === 1 ? "look" : "looks"}` : ""}` : "No check in progress");
  $("verified-icon").textContent = v.verified === true && !hasPending ? "✓" : "◇";
  $("verified-icon").classList.toggle("good", v.verified === true && !hasPending);
  set("repair-state", sup.phase ? text(sup.phase).toUpperCase() : supervision.available ? "SESSION MISMATCH" : "UNCONNECTED");
  set("repair-detail", sup.phase ? `Reported supervisor phase: ${text(sup.phase)}. Repair required: ${text(sup.repair_required, "unknown")}. Attempt: ${text(sup.attempt)}. Lock ownership is not inferred.` : supervision.available ? "Supervisor session does not match this telemetry. Its repair state and cutoff are not applied." : "No matching supervisor state connected. No repair or process-control actions are available.");
  renderLog(data);
  const warnings = [];
  $("evidence-ticker").hidden = data.source?.mode !== "legacy";
  if (data.source?.invalid) warnings.push(`${data.source.invalid} malformed or unsupported rows rejected.`);
  if (data.source?.partial) warnings.push("Waiting for a complete final JSONL line.");
  if (v.gap) warnings.push("Event history has a gap or was bounded; this is not a complete audit.");
  if (data.source?.status === "unavailable") warnings.push("Telemetry file unavailable; showing retained evidence.");
  if (frozen) warnings.push("Display frozen; gameplay and video continue independently.");
  notice(warnings.join(" "), false);
  refreshStatus();
}

function refreshStatus() {
  const data = object(frozen ? displayed : latest);
  const v = object(data.view);
  const clock = object(latest);
  const now = typeof clock.server_time === "number" ? clock.server_time + (performance.now() - receivedAt) / 1000 : Date.now() / 1000;
  const age = typeof v.last_event_time === "number" ? Math.max(0, now - v.last_event_time) : null;
  const stale = age === null || age > 15 || data.source?.status === "unavailable";
  const ended = ["returned", "error"].includes(v.lifecycle);
  const heartbeatAge = (performance.now() - receivedAt) / 1000;
  const transportFresh = connected && heartbeatAge <= 3;
  const active = transportFresh && !stale && !ended;
  const observationAge = typeof v.state_observed_time === "number" ? now - v.state_observed_time : null;
  MissionControl.freshness({active: active && observationAge !== null && observationAge >= 0 && observationAge <= 15,
    frozen, legacy: data.source?.mode === "legacy", gap: v.gap || data.source?.partial});
  for (let stage = 2; stage <= 7; stage++) $(`stage-${stage}`).classList.toggle("active", active && !frozen && data.source?.mode !== "legacy" && v.stage === stage);
  $("connection-led").className = `led ${active ? "live" : "stale"}`;
  set("connection", !connected ? "Reconnecting" : !transportFresh ? "Feed delayed" : ended ? "Invocation ended" : stale ? "No recent telemetry" : "Feed connected");
  $("connection").title = receivedAt ? `Last transport snapshot ${Math.floor(heartbeatAge)}s ago. Game records update independently of this heartbeat.` : "No transport snapshot received.";
  set("freshness", age === null ? "No recorded events" : `${data.source?.mode === "legacy" ? (v.legacy_record_timestamp ? "Recorded at" : "File modified") : "Last event"} ${age < 1 ? "just now" : Math.floor(age) + "s ago"}`);
  const thinking = active && !frozen && data.source?.mode !== "legacy" && v.model_busy === true;
  const recovery = object(v.persistent_recovery);
  const waitingForChangedEvidence = recovery.phase === "waiting_for_changed_game_evidence";
  const evaluatingChangedEvidence = recovery.phase === "evaluating_changed_game_evidence";
  const unresolvedDecision = recovery.phase === "evaluation_outcome_unknown_waiting";
  const legacy = data.source?.mode === "legacy";
  const recorded = legacy && (v.action || v.outcome);
  const checking = legacy && Object.keys(object(v.pending)).length > 0;
  $("signal").classList.toggle("active", thinking || (checking && !stale && !frozen));
  const ago = age === null ? "" : age < 5 ? "just now" : age < 120 ? `${Math.floor(age)}s ago` : `${Math.floor(age / 60)}m ago`;
  set("thinking-status", frozen ? "Display frozen" : unresolvedDecision && active ? "Decision outcome unknown · observing for changed evidence" : waitingForChangedEvidence && active ? "Blocked · waiting for changed game evidence" : evaluatingChangedEvidence && active ? "Re-evaluating changed game evidence" : thinking ? "JEV is evaluating" : recorded ? Explain.event(v).text : ended ? "Controller invocation ended" : stale ? "Awaiting fresh evidence" : text(v.kind, "Waiting for an agent").replaceAll("_", " "));
  set("model-detail", unresolvedDecision && active ? `No repeat model request · next observation in ${number(recovery.next_observation_seconds, 0)}s · game status remains ${text(v.status, "unchanged")}`
    : active && waitingForChangedEvidence ? `${text(recovery.reason, "Recoverable decision block")} · next observation in ${number(recovery.next_observation_seconds, 0)}s · no gameplay action running`
    : active && evaluatingChangedEvidence ? `${text(recovery.reason, "Recoverable decision block")} · checking changed native evidence before another selection`
    : recorded ? `${text(v.goal, "no goal").replaceAll("_", " ")} · tick ${text(object(v.state).tick, text(v.tick))}${ago ? " · " + ago : ""}${stale ? " · no newer step yet" : ""}`
    : legacy ? "Completed decision only · no in-flight telemetry" : thinking ? "Provider call in flight · no tokens invented" : v.response ? "Provider response captured · inspect acceptance" : "No model response in this cycle");
  const supervision = object(data.supervisor);
  // Elapsed time since the supervisor started this run; it stops at the cutoff.
  const run = supervision.session_match ? object(supervision.state) : {};
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  if (finite(run.started_at) && run.started_at <= now) {
    const end = finite(run.cutoff) && run.cutoff >= run.started_at ? Math.min(now, run.cutoff) : now;
    const elapsed = Math.floor(end - run.started_at);
    set("run-time", `${String(Math.floor(elapsed / 3600)).padStart(2, "0")}:${String(Math.floor(elapsed / 60) % 60).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`);
  } else set("run-time", "Not connected");
}

function setBroadcast(value) {
  broadcast = value;
  document.body.classList.toggle("broadcast", value);
  document.documentElement.classList.toggle("obs-scrollbars-hidden", studio || value);
  $("broadcast").setAttribute("aria-pressed", String(value));
}
function stopCapture() {
  mediaGeneration += 1;
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  $("game-video").srcObject = null;
  $("capture-placeholder").hidden = false;
  $("stop-capture").disabled = true;
  $("video-led").className = "led";
  $("camera-devices").hidden = true;
  $("camera-devices").replaceChildren();
  set("video-status", studio ? "OBS COMPOSITION" : "NO SOURCE");
  set("video-resolution", studio ? "Video is supplied by a separate OBS source" : "Capture permission required");
}
function updateVideoStatus() {
  if (!stream) return;
  const video = $("game-video");
  const track = stream.getVideoTracks()[0];
  const live = track?.readyState === "live" && !track.muted;
  $("video-led").className = `led ${live ? "live" : "stale"}`;
  set("video-status", live ? sourceName : "SOURCE PAUSED");
  set("video-resolution", video.videoWidth ? `${video.videoWidth} × ${video.videoHeight} · browser capture (not synchronized to game ticks)` : "Connecting video source…");
}
async function capture(kind) {
  if (mediaBusy) return;
  if (!navigator.mediaDevices || !window.isSecureContext) { notice("Capture requires a supported browser on localhost or HTTPS."); return; }
  if (kind === "window" && !navigator.mediaDevices.getDisplayMedia) { notice("Window capture is unavailable in this browser. Use Camera / OBS or a supported desktop browser."); return; }
  mediaBusy = true;
  const generation = ++mediaGeneration;
  try {
    const device = $("camera-devices").value;
    const incoming = kind === "window" ? await navigator.mediaDevices.getDisplayMedia({video: true, audio: false}) : await navigator.mediaDevices.getUserMedia({video: device ? {deviceId: {exact: device}} : true, audio: false});
    if (generation !== mediaGeneration) { incoming.getTracks().forEach((track) => track.stop()); return; }
    if (!incoming.getVideoTracks().length) {
      incoming.getTracks().forEach((track) => track.stop());
      throw new DOMException("No video track", "NotFoundError");
    }
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = incoming;
    sourceName = kind === "window" ? "WINDOW CAPTURE" : "CAMERA / OBS";
    $("game-video").srcObject = stream;
    $("capture-placeholder").hidden = true;
    $("stop-capture").disabled = false;
    const track = stream.getVideoTracks()[0];
    track.addEventListener("ended", () => { if (stream === incoming) stopCapture(); });
    track.addEventListener("mute", updateVideoStatus);
    track.addEventListener("unmute", updateVideoStatus);
    try { await $("game-video").play(); } catch {
      stopCapture();
      notice("Video playback could not start. Check browser permissions and source availability.");
      return;
    }
    updateVideoStatus();
    notice("");
    if (kind === "camera") {
      const devices = await navigator.mediaDevices.enumerateDevices().catch(() => []);
      if (generation !== mediaGeneration) return;
      const selected = track.getSettings().deviceId;
      $("camera-devices").replaceChildren(...devices.filter((d) => d.kind === "videoinput").map((d, i) => { const option = el("option", "", d.label || `Video device ${i + 1}`); option.value = d.deviceId; option.selected = d.deviceId === selected; return option; }));
      $("camera-devices").hidden = false;
      notice("Choose OBS Virtual Camera in the device list after starting it in OBS. The selected camera is previewed locally.");
    }
  } catch (error) {
    if (generation !== mediaGeneration) return;
    const messages = {NotAllowedError: "Capture was cancelled or denied. No new source was connected.", NotFoundError: "No matching video device was found. Start OBS Virtual Camera, then try again.", NotReadableError: "The selected source could not be read. Check whether another application is using it.", OverconstrainedError: "That video device is unavailable. Select another device."};
    notice(messages[error?.name] || "Video capture could not start. Check browser permissions and source availability.");
  } finally { mediaBusy = false; }
}

$("capture").onclick = $("capture-main").onclick = () => capture("window");
$("camera").onclick = () => capture("camera");
$("camera-devices").onchange = () => capture("camera");
$("stop-capture").onclick = stopCapture;
$("game-video").onloadedmetadata = updateVideoStatus;
$("fullscreen").onclick = async () => { try { if (document.fullscreenElement) await document.exitFullscreen(); else await $("game-stage").requestFullscreen(); } catch { notice("Fullscreen was not available or permitted."); } };
$("broadcast").onclick = () => setBroadcast(!broadcast);
$("freeze").onclick = () => { frozen = !frozen; document.body.classList.toggle("frozen", frozen); $("freeze").setAttribute("aria-pressed", String(frozen)); set("freeze", frozen ? "▶ Resume display" : "Ⅱ Freeze display"); if (!frozen && latest) render(latest); else if (displayed) render(displayed); };
$("event-filter").onchange = () => { if (displayed) renderLog(displayed); };
$("close-inspector").onclick = () => $("inspector").close();
$("inspect-model").onclick = () => inspect("JEV request & response", STAGES[4][2], stageEvidence(4));
$("inspect-plan").onclick = () => inspect("Committed plan", STAGES[3][2], stageEvidence(3));
$("inspect-pending").onclick = () => inspect("Pending-action evidence", STAGES[6][2], stageEvidence(6));
$("export").onclick = () => {
  if (!displayed) { notice("No captured view is available to export."); return; }
  const payload = {display_only: true, complete_audit: false, exported_at: new Date().toISOString(), snapshot: displayed};
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"}));
  const link = el("a"); link.href = url; link.download = "jev-dashboard-view.json"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
window.addEventListener("keydown", (event) => {
  if ($("inspector").open || ["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
  if (event.key === "b" || event.key === "Escape" && broadcast) { setBroadcast(!broadcast); return; }
  if (event.target.tagName === "BUTTON") return;
  if (event.code === "Space") { event.preventDefault(); $("freeze").click(); }
});
setBroadcast(broadcast);
renderGoals({});
let events = null;
function connectEvents() {
  if (events) events.close();
  events = new EventSource("/api/events");
  events.addEventListener("snapshot", (event) => {
    try {
      const parsed = JSON.parse(event.data);
      if (!parsed || typeof parsed !== "object" || !parsed.source || typeof parsed.source !== "object" ||
          Array.isArray(parsed.source) || !parsed.view || typeof parsed.view !== "object" ||
          Array.isArray(parsed.view) || !Array.isArray(parsed.events) || !Number.isFinite(parsed.server_time)) throw new Error("shape");
      latest = parsed; connected = true; receivedAt = performance.now();
      if (feedNotice) { feedNotice = ""; notice(userNotice); }
      if (!frozen && snapshotKey(latest) !== renderedKey) render(latest);
      else refreshStatus();
    } catch {
      connected = false;
      feedNotice = "An invalid dashboard snapshot was rejected.";
      notice(userNotice);
      refreshStatus();
    }
  });
  events.onerror = () => { connected = false; refreshStatus(); };
}
connectEvents();
setInterval(refreshStatus, 500);
window.addEventListener("pagehide", () => { stopCapture(); events.close(); connected = false; refreshStatus(); });
window.addEventListener("pageshow", (event) => { if (event.persisted) connectEvents(); });
