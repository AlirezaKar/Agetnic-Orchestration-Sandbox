/* ═══════════════════════════════════════════════════════════════
   Agetnic Orchestration Sandbox — test-harness frontend
   Talks to the FastAPI backend on the same origin:
     GET  /health /providers /providers/{name}/models /diagnose
     POST /chat /search
   No build step, no dependencies.
   ═══════════════════════════════════════════════════════════════ */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const MODES = [
    {
      id: "agent",
      title: "Agent (tools)",
      desc: "The model decides when to call web_search / fetch_url. Full trace.",
    },
    {
      id: "answer",
      title: "Answer only",
      desc: "Tools disabled — the model answers from its own knowledge.",
    },
    {
      id: "search",
      title: "Search only",
      desc: "Run the search chain directly (no model, no tool loop).",
    },
  ];

  const STORAGE = { runs: "agetnic.runs.v1", mode: "agetnic.mode.v1" };

  // Some browsers (private mode, embedded webviews) throw on localStorage.
  const store = (() => {
    try {
      const candidate = window.localStorage;
      candidate.setItem("__probe__", "1");
      candidate.removeItem("__probe__");
      return candidate;
    } catch (_) {
      const memory = new Map();
      return {
        getItem: (key) => (memory.has(key) ? memory.get(key) : null),
        setItem: (key, value) => memory.set(key, String(value)),
        removeItem: (key) => memory.delete(key),
      };
    }
  })();

  const state = {
    mode: store.getItem(STORAGE.mode) || "agent",
    pinnedSearch: null,
    runs: loadRuns(),
    diagnostics: null,
    providers: null,
    messages: [],
    busy: false,
  };

  /* ────────────────────────────── helpers ───────────────────── */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function loadRuns() {
    try {
      const parsed = JSON.parse(store.getItem(STORAGE.runs) || "[]");
      return Array.isArray(parsed) ? parsed.slice(0, 25) : [];
    } catch (_) {
      return [];
    }
  }

  function saveRuns() {
    try {
      store.setItem(STORAGE.runs, JSON.stringify(state.runs.slice(0, 25)));
    } catch (_) {
      /* storage may be disabled — history is then session-only */
    }
  }

  async function api(path, options = {}) {
    try {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        ...options,
      });
      let data = {};
      try {
        data = await response.json();
      } catch (_) {
        data = {};
      }
      return { ok: response.ok, status: response.status, data };
    } catch (error) {
      return {
        ok: false,
        status: 0,
        data: { message: "Cannot reach the backend: " + error.message },
      };
    }
  }

  function showInspector(payload, note) {
    $("trace-json").textContent =
      typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
    if (note) $("inspector-note").textContent = note;
    document.body.classList.add("inspector-open");
  }

  function errorText(result) {
    const data = result.data || {};
    const parts = [];
    parts.push(data.message || data.error || "Request failed");
    if (data.status) parts.push("(HTTP " + data.status + ")");
    else if (result.status) parts.push("(HTTP " + result.status + ")");
    const detail = data.detail;
    if (detail && typeof detail === "object" && detail.hint) parts.push("\n\nhint: " + detail.hint);
    if (data.hint) parts.push("\n\nhint: " + data.hint);
    if (detail && typeof detail === "object" && detail.message) parts.push("\n\n" + detail.message);
    return parts.join(" ");
  }

  /* ──────────────────────────── top bar ─────────────────────── */
  function setBadge(id, text, tone) {
    const badge = $(id);
    badge.textContent = text;
    badge.classList.remove("ok", "bad", "warn");
    if (tone) badge.classList.add(tone);
  }

  async function loadHealth() {
    const { ok, data } = await api("/health");
    if (!ok) {
      setBadge("badge-llm", "LLM: backend down", "bad");
      setBadge("badge-search", "Search: unknown", "bad");
      return;
    }
    const models = data.model_chain || [];
    const searches = data.search_chain || [];
    setBadge(
      "badge-llm",
      models.length ? "LLM: " + models.join(" → ") : "LLM: no key",
      models.length ? "ok" : "bad"
    );
    setBadge(
      "badge-search",
      searches.length ? "Search: " + searches.join(" → ") : "Search: none",
      searches.length ? "ok" : "bad"
    );
    document.body.dataset.modelReady = models.length ? "1" : "0";
  }

  /* ──────────────────────────── modes ───────────────────────── */
  function renderModes() {
    const list = $("mode-list");
    list.textContent = "";
    MODES.forEach((mode) => {
      const button = el("button", "mode-btn" + (state.mode === mode.id ? " active" : ""));
      button.type = "button";
      button.dataset.mode = mode.id;
      button.appendChild(el("span", "mode-title", mode.title));
      button.appendChild(el("span", "mode-desc", mode.desc));
      button.addEventListener("click", () => setMode(mode.id));
      list.appendChild(button);
    });
    updateComposerLabel();
  }

  function setMode(id) {
    state.mode = id;
    store.setItem(STORAGE.mode, id);
    document.querySelectorAll(".mode-btn").forEach((node) => {
      node.classList.toggle("active", node.dataset.mode === id);
    });
    updateComposerLabel();
  }

  function updateComposerLabel() {
    const mode = MODES.find((item) => item.id === state.mode) || MODES[0];
    const pinned = state.pinnedSearch ? " · backend: " + state.pinnedSearch : "";
    $("composer-mode-label").textContent =
      "Mode: " + mode.title + (state.mode === "search" ? pinned : "");
  }

  /* ──────────────────── search backend sidebar ──────────────── */
  function backendPill(preset) {
    const pill = el("span", "pill " + (preset.configured ? "ok" : "bad"));
    pill.textContent = preset.needs_server ? "server" : "no server";
    pill.title = preset.needs_server
      ? "This backend must run as a process you keep alive"
      : "Talks straight to a public HTTPS endpoint — nothing to run";
    return pill;
  }

  function renderSearchList(providers) {
    const list = $("search-list");
    list.textContent = "";

    const chainItem = el("div", "provider-item" + (state.pinnedSearch ? "" : " active"));
    const chainRow = el("div", "row1");
    chainRow.appendChild(el("span", "pname", "Chain (failover)"));
    chainRow.appendChild(el("span", "pill", "all"));
    chainItem.appendChild(chainRow);
    chainItem.appendChild(el("div", "pdesc", "Try each backend in order, first non-empty wins"));
    chainItem.addEventListener("click", () => {
      state.pinnedSearch = null;
      renderSearchList(state.providers);
      updateComposerLabel();
    });
    list.appendChild(chainItem);

    (providers || []).forEach((preset) => {
      const item = el(
        "div",
        "provider-item" + (state.pinnedSearch === preset.name ? " active" : "")
      );
      const row = el("div", "row1");
      row.appendChild(el("span", "pname", preset.label));
      row.appendChild(backendPill(preset));
      if (!preset.configured) row.appendChild(el("span", "pill bad", "no key"));
      item.appendChild(row);
      const detail = preset.needs_key
        ? "free key · " + preset.free_tier
        : preset.free_tier;
      item.appendChild(el("div", "pdesc", detail));
      item.title = preset.notes || "";
      item.addEventListener("click", () => {
        state.pinnedSearch = state.pinnedSearch === preset.name ? null : preset.name;
        renderSearchList(state.providers);
        updateComposerLabel();
        if (state.mode !== "search") setMode("search");
      });
      list.appendChild(item);
    });
  }

  async function loadProviders() {
    const { ok, data } = await api("/providers");
    if (!ok) return;
    state.providers = data.search ? data.search.providers : [];
    renderSearchList(state.providers);
    state.fullProviders = data;
  }

  /* ──────────────────────── diagnostics ─────────────────────── */
  function diagRow(ok, name, detail, pillText) {
    const row = el("div", "diag-row");
    row.appendChild(el("span", "status-dot " + (ok ? "completed" : "failed")));
    row.appendChild(el("span", "name", name));
    row.appendChild(el("span", "ms", detail || ""));
    if (pillText) row.appendChild(el("span", "pill " + (ok ? "ok" : "bad"), pillText));
    return row;
  }

  async function runDiagnostics() {
    setBadge("badge-diag", "Diag: running…", "warn");
    const summary = $("diag-summary");
    summary.className = "diag-summary";
    summary.textContent = "Running model ping + search backends…";

    const { ok, data } = await api("/diagnose");
    state.diagnostics = data;
    summary.textContent = "";

    if (!ok) {
      summary.appendChild(el("div", "muted", "Diagnostics failed: " + errorText({ ok, status: 0, data })));
      setBadge("badge-diag", "Diag: failed", "bad");
      return;
    }

    const model = data.model || {};
    if (model.ok) {
      summary.appendChild(diagRow(true, "model · " + model.provider, model.latency_ms + " ms"));
      summary.appendChild(
        el("div", "pdesc", model.model + (model.reply ? " · reply " + JSON.stringify(model.reply) : ""))
      );
    } else {
      const error = model.error || {};
      summary.appendChild(diagRow(false, "model · " + (error.provider || "none"), ""));
      if (error.message) summary.appendChild(el("div", "pdesc", error.message));
      if (error.hint) summary.appendChild(el("div", "pdesc", error.hint));
    }

    const search = data.search || { checked: [] };
    (search.checked || []).forEach((report) => {
      summary.appendChild(
        diagRow(
          !!report.ok,
          "search · " + report.provider,
          report.ok ? report.count + " hits · " + report.latency_ms + " ms" : ""
        )
      );
      if (!report.ok && report.error) {
        summary.appendChild(el("div", "pdesc", report.error.message || "failed"));
        if (report.error.hint) summary.appendChild(el("div", "pdesc", report.error.hint));
      }
    });
    (search.skipped || []).forEach((skip) => {
      summary.appendChild(diagRow(false, "search · " + skip.provider, "skipped", "skip"));
      summary.appendChild(el("div", "pdesc", skip.reason));
    });
    (data.hints || []).forEach((hint) => summary.appendChild(el("div", "pdesc", "→ " + hint)));

    const modelOk = !!model.ok;
    const anySearch = (search.ok || []).length > 0;
    setBadge(
      "badge-diag",
      "Diag: " + (modelOk ? "model ok" : "model failed") + " · " + (anySearch ? "search ok" : "search failed"),
      modelOk && anySearch ? "ok" : "bad"
    );
    showInspector(data, "Diagnostics report (GET /diagnose)");
  }

  /* ─────────────────────────── providers modal ─────────────── */
  function providerCard(provider) {
    const card = el("div", "provider-card");
    const head = el("div", "pc-head");
    head.appendChild(el("span", "pc-name", provider.label));
    const statusPill = el("span", "pill " + (provider.configured ? "ok" : "bad"));
    statusPill.textContent = provider.configured ? "configured" : "needs key";
    head.appendChild(statusPill);
    if (provider.is_active) head.appendChild(el("span", "pill warn", "active"));
    if (provider.needs_local_server) head.appendChild(el("span", "pill", "local"));
    card.appendChild(head);

    const links = el("div", "pc-links");
    const key = el("a", null, "Get API key ↗");
    key.href = provider.key_url;
    key.target = "_blank";
    key.rel = "noreferrer";
    const models = el("a", null, "See models ↗");
    models.href = provider.models_url;
    models.target = "_blank";
    models.rel = "noreferrer";
    const api = el("a", null, "Models API");
    api.href = "/providers/" + provider.name + "/models";
    api.target = "_blank";
    api.rel = "noreferrer";
    links.appendChild(key);
    links.appendChild(models);
    links.appendChild(api);
    card.appendChild(links);

    card.appendChild(el("div", "pc-note", provider.free_tier));
    const env = el("div", "pc-note");
    env.appendChild(document.createTextNode("env: "));
    const code = el("code", null, "LLM_PROVIDER=" + provider.name + "  " + provider.model_env + "=auto");
    env.appendChild(code);
    card.appendChild(env);
    if (provider.notes) card.appendChild(el("div", "pc-note", provider.notes));
    return card;
  }

  async function openProviders() {
    const body = $("providers-body");
    body.textContent = "loading…";
    $("providers-modal").classList.add("open");
    if (!state.fullProviders) await loadProviders();
    const data = state.fullProviders || {};
    body.textContent = "";

    body.appendChild(el("h3", null, "Core model"));
    (data.providers || []).forEach((provider) => body.appendChild(providerCard(provider)));

    body.appendChild(el("h3", null, "Web search (free)"));
    ((data.search && data.search.providers) || []).forEach((preset) => {
      const card = el("div", "provider-card");
      const head = el("div", "pc-head");
      head.appendChild(el("span", "pc-name", preset.label));
      const mode = el("span", "pill " + (preset.needs_server ? "warn" : "ok"));
      mode.textContent = preset.needs_server ? "needs server" : "no server";
      head.appendChild(mode);
      const status = el("span", "pill " + (preset.configured ? "ok" : "bad"));
      status.textContent = preset.configured ? "ready" : "needs key";
      head.appendChild(status);
      card.appendChild(head);

      const links = el("div", "pc-links");
      const setup = el("a", null, "Key / setup ↗");
      setup.href = preset.key_url;
      setup.target = "_blank";
      setup.rel = "noreferrer";
      const docs = el("a", null, "Docs ↗");
      docs.href = preset.docs_url;
      docs.target = "_blank";
      docs.rel = "noreferrer";
      links.appendChild(setup);
      links.appendChild(docs);
      card.appendChild(links);
      card.appendChild(el("div", "pc-note", preset.free_tier));
      if (preset.notes) card.appendChild(el("div", "pc-note", preset.notes));
      body.appendChild(card);
    });
  }

  /* ─────────────────────────── chat render ─────────────────── */
  function hideEmptyState() {
    const empty = $("empty-state");
    if (empty) empty.remove();
  }

  function appendUser(text) {
    hideEmptyState();
    const message = el("div", "message user");
    const avatar = el("div", "avatar", "You");
    const body = el("div", "body");
    body.appendChild(el("div", "msg-role", "You"));
    body.appendChild(el("div", "msg-text", text));
    message.appendChild(avatar);
    message.appendChild(body);
    $("chat").appendChild(message);
    scrollChat();
  }

  function appendAssistantSkeleton() {
    hideEmptyState();
    const message = el("div", "message assistant");
    const avatar = el("div", "avatar", "AI");
    const body = el("div", "body");
    body.appendChild(el("div", "msg-role", "Assistant"));
    const text = el("div", "msg-text");
    text.appendChild(el("span", "spinner"));
    text.appendChild(document.createTextNode(" thinking…"));
    body.appendChild(text);
    message.appendChild(avatar);
    message.appendChild(body);
    $("chat").appendChild(message);
    scrollChat();
    return { message, body, text };
  }

  function scrollChat() {
    const chat = $("chat");
    chat.scrollTop = chat.scrollHeight;
  }

  function renderTimeline(container, run) {
    const trace = run.trace || [];
    if (!trace.length) return;
    const timeline = el("div", "timeline");
    trace.forEach((step) => {
      const box = el("div", "tl-step");
      const head = el("div", "tl-head");
      const dot = el("span", "status-dot " + (step.type === "tool" && step.ok === false ? "failed" : "completed"));
      head.appendChild(dot);
      const title =
        step.type === "model"
          ? (step.provider || "") + "/" + (step.model || "") +
            (step.tool_calls && step.tool_calls.length ? " → " + step.tool_calls.map((c) => c.name).join(", ") : " → answer")
          : (step.name || "tool") + " " + JSON.stringify(step.arguments || {});
      head.appendChild(el("span", "tl-title", title));
      const kind = el("span", "tl-kind " + (step.type === "model" ? "model" : "tool"), step.type);
      head.appendChild(kind);
      head.appendChild(el("span", "tl-time", (step.latency_ms || 0) + " ms"));
      head.addEventListener("click", () => box.classList.toggle("open"));
      box.appendChild(head);

      const bodyEl = el("div", "tl-body");
      if (step.type === "model" && step.content) {
        bodyEl.appendChild(el("div", "tl-section-label", "model output"));
        bodyEl.appendChild(el("pre", "tl-pre output", step.content));
      }
      if (step.tool_calls && step.tool_calls.length) {
        bodyEl.appendChild(el("div", "tl-section-label", "tool calls"));
        bodyEl.appendChild(el("pre", "tl-pre", JSON.stringify(step.tool_calls, null, 2)));
      }
      if (step.type === "tool") {
        bodyEl.appendChild(el("div", "tl-section-label", "result preview"));
        bodyEl.appendChild(el("pre", "tl-pre", JSON.stringify(step.preview, null, 2)));
      }
      box.appendChild(bodyEl);
      timeline.appendChild(box);
    });
    container.appendChild(timeline);
  }

  function renderSources(container, urls) {
    if (!urls || !urls.length) return;
    container.appendChild(el("div", "tl-section-label", "sources"));
    const list = el("div", "sources");
    urls.forEach((url, index) => {
      const link = el("a", null, "[" + (index + 1) + "] " + url);
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer";
      list.appendChild(link);
    });
    container.appendChild(list);
  }

  function renderMeta(container, run) {
    const bits = [];
    if (run.provider) bits.push(run.provider + "/" + run.model);
    if (run.search_provider) bits.push("search: " + run.search_provider);
    if (run.steps) bits.push(run.steps + " step(s)");
    if (run.latency_ms) bits.push(run.latency_ms + " ms");
    if (bits.length) container.appendChild(el("div", "tl-meta", bits.join(" · ")));
  }

  function finishAssistant(skeleton, result, prompt, mode) {
    const { body, text } = skeleton;
    text.textContent = "";

    if (!result.ok) {
      text.appendChild(el("div", null, errorText(result)));
      body.appendChild(text);
      scrollChat();
      return null;
    }

    const data = result.data;
    let answer = data.answer;
    if (mode === "search") {
      const lines = (data.results || []).map(
        (item, index) =>
          (index + 1) + ". " + item.title + "\n   " + item.url + "\n   " + (item.snippet || "")
      );
      answer =
        "Search backend: " + data.provider + " · " + data.count + " result(s) · " + data.latency_ms + " ms\n\n" +
        (lines.length ? lines.join("\n\n") : "No results.");
    }

    text.textContent = answer || "(empty answer)";
    body.appendChild(text);
    renderTimeline(body, data);
    renderSources(body, data.sources);
    renderMeta(body, data);
    scrollChat();

    const run = {
      id: "run-" + Date.now(),
      ts: new Date().toISOString(),
      mode,
      prompt,
      provider: data.provider || "",
      model: data.model || "",
      latency_ms: data.latency_ms || 0,
      steps: data.steps || 0,
      payload: data,
    };
    state.runs.unshift(run);
    state.runs = state.runs.slice(0, 25);
    saveRuns();
    renderHistory();
    showInspector(data, "Run " + run.id + " — " + mode + " mode");
    return run;
  }

  /* ─────────────────────────── run history ─────────────────── */
  function renderHistory() {
    const list = $("trace-list");
    list.textContent = "";
    if (!state.runs.length) {
      list.appendChild(el("li", "muted", "No runs yet."));
      return;
    }
    state.runs.forEach((run) => {
      const item = el("li");
      item.appendChild(document.createTextNode(run.prompt.slice(0, 64)));
      const meta = el(
        "span",
        "trace-meta",
        run.mode + " · " + (run.provider || "search") + " · " + run.latency_ms + " ms"
      );
      item.appendChild(meta);
      item.addEventListener("click", () => {
        showInspector(run.payload, run.mode + " run · " + run.ts);
      });
      list.appendChild(item);
    });
  }

  /* ──────────────────────────── sending ────────────────────── */
  function historyPayload() {
    return state.messages.slice(-8);
  }

  async function send() {
    if (state.busy) return;
    const input = $("prompt");
    const text = input.value.trim();
    if (!text) return;

    state.busy = true;
    $("btn-send").disabled = true;
    const sendLabel = $("btn-send").textContent;
    $("btn-send").textContent = "…";

    const mode = state.mode;
    appendUser(text);
    input.value = "";
    const skeleton = appendAssistantSkeleton();

    let result;
    if (mode === "search") {
      result = await api("/search", {
        method: "POST",
        body: JSON.stringify({
          query: text,
          max_results: 5,
          provider: state.pinnedSearch || null,
        }),
      });
    } else {
      result = await api("/chat", {
        method: "POST",
        body: JSON.stringify({
          message: text,
          history: historyPayload(),
          allow_search: mode === "agent",
          max_steps: 4,
        }),
      });
    }

    const run = finishAssistant(skeleton, result, text, mode);
    if (run && mode !== "search") {
      state.messages.push({ role: "user", content: text });
      state.messages.push({ role: "assistant", content: (result.data.answer || "").slice(0, 4000) });
      state.messages = state.messages.slice(-12);
    }

    state.busy = false;
    $("btn-send").disabled = false;
    $("btn-send").textContent = sendLabel;
  }

  function clearSession() {
    state.messages = [];
    const chat = $("chat");
    chat.textContent = "";
    const empty = el("div", "empty-state");
    empty.id = "empty-state";
    empty.appendChild(el("h2", null, "Orchestration test console"));
    const p = el("p");
    p.appendChild(document.createTextNode("Conversation cleared. Pick a mode and send a prompt."));
    empty.appendChild(p);
    chat.appendChild(empty);
    showInspector("Session cleared — no active trace.", "Trace inspector");
  }

  /* ────────────────────────────── wire up ──────────────────── */
  function wire() {
    $("btn-send").addEventListener("click", send);
    $("btn-clear").addEventListener("click", clearSession);
    $("btn-diag").addEventListener("click", runDiagnostics);
    $("btn-diag-top").addEventListener("click", runDiagnostics);
    $("btn-providers").addEventListener("click", openProviders);
    $("btn-close-providers").addEventListener("click", () =>
      $("providers-modal").classList.remove("open")
    );
    $("providers-modal").addEventListener("click", (event) => {
      if (event.target === $("providers-modal")) $("providers-modal").classList.remove("open");
    });
    $("btn-refresh-search").addEventListener("click", () => {
      state.fullProviders = null;
      loadProviders();
      loadHealth();
    });
    $("btn-clear-history").addEventListener("click", () => {
      state.runs = [];
      saveRuns();
      renderHistory();
    });
    $("btn-copy-trace").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("trace-json").textContent || "");
        $("btn-copy-trace").textContent = "Copied ✓";
        setTimeout(() => ($("btn-copy-trace").textContent = "Copy JSON"), 1200);
      } catch (_) {
        /* clipboard may be blocked — the text stays selectable */
      }
    });
    $("btn-close-inspector").addEventListener("click", () => {
      $("trace-json").textContent = "Inspector closed. Select a run or send a prompt.";
      $("inspector-note").textContent = "The complete run record or the last diagnostics report.";
    });

    $("prompt").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });

    document.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        $("prompt").value = chip.dataset.prompt || chip.textContent;
        $("prompt").focus();
      });
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") $("providers-modal").classList.remove("open");
    });
  }

  function boot() {
    renderModes();
    renderHistory();
    wire();
    loadHealth();
    loadProviders();
    if (!state.runs.length) {
      showInspector(
        {
          hint: "Nothing inspected yet.",
          modes: MODES.map((mode) => mode.id),
          endpoints: ["/health", "/providers", "/chat", "/search", "/diagnose", "/search/diagnose"],
        },
        "Trace inspector — send a prompt or run diagnostics to fill this panel."
      );
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
