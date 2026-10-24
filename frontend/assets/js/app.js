/* ═══════════════════════════════════════════════════════════════
   Agetnic Orchestration Sandbox — test-harness frontend logic
   ───────────────────────────────────────────────────────────────
   Purpose: exercise the orchestrator and visualise *everything* it
   does — input, thinking, the task plan, per-agent process and the
   final output — plus the raw JSON trace persisted on the backend.

   No frameworks, no build step: plain fetch + DOM.
   ═══════════════════════════════════════════════════════════════ */

"use strict";

/* ─── Session state ──────────────────────────────────────────── */
const state = {
  sessionId: `sess_${Math.random().toString(16).slice(2, 10)}`,
  mode: "auto",
  sending: false,
  lastTrace: null, // full RunTrace JSON of the most recent/inspected run
};

/* ─── Tiny DOM helpers ───────────────────────────────────────── */
const $ = (id) => document.getElementById(id);

/** Escape user/LLM text before inserting into HTML (LLM output is untrusted). */
function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** JSON pretty-print for the inspector. */
function pretty(obj) {
  return JSON.stringify(obj, null, 2);
}

/* ═══════════════════════════════════════════════════════════════
   1. Bootstrap: health, modes, documents, run history
   ═══════════════════════════════════════════════════════════════ */

async function init() {
  bindStaticEvents();
  await Promise.all([loadHealth(), loadModes(), loadDocuments(), loadTraces()]);
}

async function loadHealth() {
  try {
    const health = await fetchJSON("/api/health");
    const llm = $("badge-llm");
    llm.textContent = `LLM: ${health.llm_provider}`;
    llm.classList.add("ok");
    const search = $("badge-search");
    const fetcher = health.page_fetcher && health.page_fetcher !== "none"
      ? ` · ${health.page_fetcher}`
      : "";
    search.textContent = `Search: ${health.web_search_provider}${fetcher}`;
    search.classList.add("ok");
  } catch {
    $("badge-llm").textContent = "LLM: offline";
  }
}

async function loadModes() {
  const data = await fetchJSON("/api/modes");
  const container = $("mode-list");
  container.innerHTML = "";
  for (const mode of data.modes) {
    const btn = document.createElement("button");
    btn.className = `mode-btn${mode.id === state.mode ? " active" : ""}`;
    btn.dataset.mode = mode.id;
    btn.innerHTML = `
      <span class="mode-title">${escapeHtml(mode.label)}</span>
      <span class="mode-desc">${escapeHtml(mode.description)}</span>`;
    btn.addEventListener("click", () => selectMode(mode.id, mode.label));
    container.appendChild(btn);
  }
  updateComposerLabel(data.modes);
}

function selectMode(modeId, label) {
  state.mode = modeId;
  document.querySelectorAll(".mode-btn").forEach((el) => {
    el.classList.toggle("active", el.dataset.mode === modeId);
  });
  $("composer-mode-label").textContent = `Mode: ${label ?? modeId}`;
}

function updateComposerLabel(modes) {
  const current = modes.find((m) => m.id === state.mode);
  $("composer-mode-label").textContent = `Mode: ${current ? current.label : state.mode}`;
}

async function loadDocuments() {
  try {
    const data = await fetchJSON("/api/documents");
    const list = $("doc-list");
    $("badge-docs").textContent = `Docs: ${data.documents.length}`;
    $("badge-docs").classList.add("ok");
    if (!data.documents.length) {
      list.innerHTML = `<li class="muted">No documents indexed yet.</li>`;
      return;
    }
    list.innerHTML = data.documents
      .map((d) => `<li>📄 ${escapeHtml(d.name)}</li>`)
      .join("");
  } catch {
    /* backend not ready yet — keep placeholder */
  }
}

async function loadTraces() {
  try {
    const data = await fetchJSON("/api/traces?limit=30");
    const list = $("trace-list");
    if (!data.traces.length) {
      list.innerHTML = `<li class="muted">No runs yet.</li>`;
      return;
    }
    list.innerHTML = "";
    for (const t of data.traces) {
      const li = document.createElement("li");
      const ok = t.success ? "✅" : "❌";
      li.innerHTML = `
        <div>${ok} <strong>${escapeHtml(t.mode_used)}</strong> — ${escapeHtml(t.input_preview || "(empty)")}</div>
        <span class="trace-meta">${escapeHtml(t.run_id)} · ${t.steps} steps · ${Math.round(t.duration_ms || 0)} ms</span>`;
      li.addEventListener("click", () => openTrace(t.run_id));
      list.appendChild(li);
    }
  } catch {
    /* ignore — history panel stays as-is */
  }
}

/* ═══════════════════════════════════════════════════════════════
   2. Sending prompts → orchestrated responses
   ═══════════════════════════════════════════════════════════════ */

async function sendPrompt() {
  const textarea = $("prompt");
  const message = textarea.value.trim();
  if (!message || state.sending) return;

  state.sending = true;
  $("btn-send").disabled = true;
  textarea.value = "";
  hideEmptyState();
  appendUserMessage(message);
  const loadingEl = appendLoadingMessage();

  try {
    const response = await fetchJSON("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        mode: state.mode,
        session_id: state.sessionId,
      }),
    });
    loadingEl.remove();
    appendAssistantMessage(response);
    showTraceJSON(responseToTraceView(response), `${response.run_id}.json (run response)`);
    loadTraces(); // refresh the history sidebar
  } catch (err) {
    loadingEl.remove();
    appendError(err);
  } finally {
    state.sending = false;
    $("btn-send").disabled = false;
    scrollChatToBottom();
  }
}

/* ═══════════════════════════════════════════════════════════════
   3. Chat rendering
   ═══════════════════════════════════════════════════════════════ */

function hideEmptyState() {
  const empty = $("empty-state");
  if (empty) empty.remove();
}

function appendUserMessage(text) {
  const el = document.createElement("div");
  el.className = "message user";
  el.innerHTML = `
    <div class="avatar">U</div>
    <div class="body">
      <div class="msg-role">You · ${escapeHtml(state.mode)}</div>
      <div class="msg-text">${escapeHtml(text)}</div>
    </div>`;
  $("chat").appendChild(el);
  scrollChatToBottom();
}

function appendLoadingMessage() {
  const el = document.createElement("div");
  el.className = "message assistant";
  el.innerHTML = `
    <div class="avatar">M</div>
    <div class="body">
      <div class="msg-role">Manager</div>
      <div class="msg-text muted">Analysing the goal → planning → delegating to agents…</div>
    </div>`;
  $("chat").appendChild(el);
  scrollChatToBottom();
  return el;
}

function appendError(err) {
  const el = document.createElement("div");
  el.className = "message assistant";
  el.innerHTML = `
    <div class="avatar">!</div>
    <div class="body">
      <div class="msg-role">Error</div>
      <div class="msg-text" style="border-color: var(--red);">${escapeHtml(err.message ?? err)}</div>
    </div>`;
  $("chat").appendChild(el);
}

function appendAssistantMessage(response) {
  const el = document.createElement("div");
  el.className = "message assistant";

  const timelineHtml = renderTimeline(response);
  el.innerHTML = `
    <div class="avatar">M</div>
    <div class="body">
      <div class="msg-role">Manager · run <code>${escapeHtml(response.run_id)}</code> · ${escapeHtml(response.mode_used)} mode</div>
      <div class="msg-text">${escapeHtml(response.output)}</div>
      ${timelineHtml}
    </div>`;
  $("chat").appendChild(el);
  bindTimelineToggles(el);
  scrollChatToBottom();
}

function scrollChatToBottom() {
  const chat = $("chat");
  chat.scrollTop = chat.scrollHeight;
}

/* ═══════════════════════════════════════════════════════════════
   4. Orchestration timeline: thinking · plan · agent tasks · synthesis
   ═══════════════════════════════════════════════════════════════ */

function renderTimeline(response) {
  const parts = ['<div class="timeline">'];

  // Manager / planner thinking blocks (collapsible summary).
  if (response.thinking?.length) {
    const firstThinking = response.thinking
      .map((t) => escapeHtml(t))
      .join("\n\n──────────\n\n");
    parts.push(tlStep({
      id: "thinking",
      kind: "manager_thinking",
      title: `Manager thinking · ${response.thinking.length} block(s)`,
      status: "completed",
      duration: null,
      sections: [{ label: "Thinking", pre: firstThinking, cls: "thinking" }],
    }));
  }

  // Planner plan: task pills with dependency arrows + wave grouping.
  if (response.plan) {
    const tasks = response.plan.tasks ?? [];
    const waves = planWaves(tasks);
    const pills = tasks.map((t) => {
      const dep = (t.depends_on ?? []).length
        ? ` <span class="dep">← ${escapeHtml(t.depends_on.join(","))}</span>`
        : "";
      return `<span class="plan-task-pill">${escapeHtml(t.id)} · ${escapeHtml(t.agent)}${dep}</span>`;
    }).join("");
    const waveText = waves
      .map((wave, i) => `Wave ${i + 1} (parallel): ${wave.join(", ")}`)
      .join("\n");

    parts.push(tlStep({
      id: "plan",
      kind: "plan",
      title: `Planner plan · ${tasks.length} task(s) · ${waves.length} wave(s)`,
      status: "completed",
      duration: null,
      extraHtml: `<div class="plan-tasks">${pills}</div>`,
      sections: [
        ...(response.plan.analysis ? [{ label: "Planner analysis", pre: escapeHtml(response.plan.analysis), cls: "thinking" }] : []),
        { label: "Execution waves", pre: escapeHtml(waveText), cls: "" },
      ],
    }));
  }

  // Per-task steps (agent work) + synthesis.
  for (const step of response.steps ?? []) {
    if (step.type === "plan") continue; // already rendered above
    const sections = [];
    if (step.thinking) sections.push({ label: "Agent thinking", pre: escapeHtml(step.thinking), cls: "thinking" });
    if (step.input) sections.push({ label: "Input", pre: escapeHtml(step.input), cls: "" });
    if (step.output) sections.push({ label: "Output", pre: escapeHtml(step.output), cls: "output" });
    if (step.error) sections.push({ label: "Error", pre: escapeHtml(step.error), cls: "" });

    const wave = step.data?.wave;
    const wavePill = wave !== undefined && step.type === "agent_task"
      ? `<span class="wave-pill">wave ${wave + 1}</span>`
      : "";
    const agentPill = step.data?.agent
      ? `<span class="wave-pill">${escapeHtml(step.data.agent)}</span>`
      : "";

    parts.push(tlStep({
      id: step.id,
      kind: step.type,
      title: escapeHtml(step.label || step.id),
      status: step.status,
      duration: step.duration_ms,
      extraHtml: `${agentPill}${wavePill}`,
      sections,
    }));
  }

  parts.push("</div>");
  return parts.join("");
}

/** Compute wave grouping client-side (mirrors the backend's scheduler). */
function planWaves(tasks) {
  const resolved = new Set();
  const waves = [];
  let remaining = [...tasks];
  while (remaining.length) {
    const ready = remaining.filter((t) => (t.depends_on ?? []).every((d) => resolved.has(d)));
    if (!ready.length) break; // safety against malformed plans
    waves.push(ready.map((t) => t.id));
    ready.forEach((t) => resolved.add(t.id));
    remaining = remaining.filter((t) => !resolved.has(t.id));
  }
  return waves;
}

/** Build one collapsible timeline step. */
function tlStep({ id, kind, title, status, duration, sections, extraHtml = "" }) {
  const durationLabel = duration != null ? `${Math.round(duration)} ms` : "";
  const body = (sections ?? [])
    .map(
      (s) => `
      <div class="tl-section-label">${escapeHtml(s.label)}</div>
      <pre class="tl-pre ${s.cls ?? ""}">${s.pre}</pre>`
    )
    .join("");
  return `
    <div class="tl-step" data-step="${escapeHtml(id)}">
      <div class="tl-head">
        <span class="status-dot ${escapeHtml(status)}"></span>
        <span class="tl-kind ${escapeHtml(kind)}">${escapeHtml(kind.replaceAll("_", " "))}</span>
        <span class="tl-title">${title}</span>
        ${extraHtml}
        <span class="tl-time">${escapeHtml(durationLabel)}</span>
      </div>
      <div class="tl-body">${body}</div>
    </div>`;
}

function bindTimelineToggles(root) {
  root.querySelectorAll(".tl-head").forEach((head) => {
    head.addEventListener("click", () => head.parentElement.classList.toggle("open"));
  });
}

/* ═══════════════════════════════════════════════════════════════
   5. Trace inspector (raw JSON — evaluation & debugging)
   ═══════════════════════════════════════════════════════════════ */

function showTraceJSON(obj, note) {
  state.lastTrace = obj;
  $("trace-json").textContent = pretty(obj);
  $("inspector-note").textContent =
    note ?? "Run trace (input · thinking · process · output).";
}

/** Open the persisted full trace of a past run. */
async function openTrace(runId) {
  try {
    const trace = await fetchJSON(`/api/traces/${runId}`);
    showTraceJSON(trace, `traces/${runId}.json — full persisted record`);
  } catch (err) {
    $("trace-json").textContent = `Failed to load trace ${runId}: ${err.message}`;
  }
}

/**
 * The /api/chat response is a *view* of the run; when we haven't loaded
 * the persisted file yet, show the same information in trace shape.
 */
function responseToTraceView(response) {
  return {
    run_id: response.run_id,
    session_id: response.session_id,
    mode_requested: response.mode_requested,
    mode_used: response.mode_used,
    input: response.input,
    output: response.output,
    thinking: response.thinking,
    plan: response.plan,
    steps: response.steps,
    metrics: response.metrics,
    success: response.success,
    trace_file: response.trace_file,
    error: response.error,
  };
}

/* ═══════════════════════════════════════════════════════════════
   6. Document upload (RAG corpus)
   ═══════════════════════════════════════════════════════════════ */

async function uploadFile(file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch("/api/documents", { method: "POST", body: form });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail ?? "Upload failed");
    await loadDocuments();
  } catch (err) {
    alert(`Upload failed: ${err.message}`);
  }
}

/* ═══════════════════════════════════════════════════════════════
   7. Misc wiring
   ═══════════════════════════════════════════════════════════════ */

function bindStaticEvents() {
  $("btn-send").addEventListener("click", sendPrompt);
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendPrompt();
    }
  });

  $("btn-clear").addEventListener("click", async () => {
    try {
      await fetch(`/api/sessions/${state.sessionId}`, { method: "DELETE" });
    } catch { /* ignore */ }
    // Fresh session + clean chat window.
    state.sessionId = `sess_${Math.random().toString(16).slice(2, 10)}`;
    $("chat").innerHTML = "";
    $("prompt").focus();
  });

  $("btn-refresh-traces").addEventListener("click", loadTraces);
  $("btn-close-inspector").addEventListener("click", () => {
    $("trace-json").textContent = "Select a run from the history — or send a prompt — to inspect its trace.";
  });
  $("btn-copy-trace").addEventListener("click", async () => {
    if (state.lastTrace) {
      await navigator.clipboard.writeText(pretty(state.lastTrace));
    }
  });

  $("file-input").addEventListener("change", (e) => uploadFile(e.target.files[0]));

  // Suggested example prompts on the empty state (delegated).
  document.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip[data-prompt]");
    if (chip) {
      $("prompt").value = chip.dataset.prompt;
      sendPrompt();
    }
  });
}

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }
  return body;
}

/* ─── go ─────────────────────────────────────────────────────── */
init();
