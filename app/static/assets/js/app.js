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
      id: "auto",
      title: "Auto (orchestrated)",
      desc: "Manager plans, document / search / data / general agents run in parallel, manager wraps up.",
    },
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
  const UI_VERSION =
    document.querySelector('meta[name="agetnic-version"]')?.content || "dev";

  // Where the API lives. Negotiated at boot so the harness works when it is
  // served from the app root, from a sub-path, or behind a proxy that puts the
  // API under /api — instead of blindly fetching "/chat" and getting a 404.
  const conn = { base: "", version: null, tried: [] };

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
    mode: store.getItem(STORAGE.mode) || "auto",
    files: [],
    agents: null,
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

  function apiUrl(path) {
    // Join like a URL, not like a string: base "/api/" + path "/health" must be
    // "/api/health", not "/api//health".
    const base = String(conn.base || "").replace(/\/+$/, "");
    const suffix = String(path || "").replace(/^\/+/, "");
    return (base ? base + "/" : "") + suffix;
  }

  function candidateBases() {
    const bases = [];
    const push = (value) => {
      const base = value === "" ? "" : value.endsWith("/") ? value : value + "/";
      if (!bases.includes(base)) bases.push(base);
    };
    push(""); // relative to the current document (works at any mount path)
    try {
      push(new URL(".", window.location.href).pathname);
    } catch (_) {
      /* ignore */
    }
    push("api");
    push("/api/");
    push("/");
    return bases;
  }

  async function negotiate() {
    conn.tried = [];
    for (const base of candidateBases()) {
      const url = base + "health";
      try {
        const response = await fetch(url, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) {
          conn.tried.push(url + " → HTTP " + response.status);
          continue;
        }
        const data = await response.json().catch(() => null);
        if (data && (data.status === "ok" || data.version)) {
          conn.base = base;
          conn.version = data.version || null;
          hideBanner();
          return true;
        }
        conn.tried.push(url + " → not this API");
      } catch (error) {
        conn.tried.push(url + " → " + error.message);
      }
    }
    conn.base = "";
    showBanner(
      "Backend not reachable",
      "Tried: " + conn.tried.join(" · ") +
        ". The page is probably a static copy, or the server is not running. " +
        "Start it with `python run.py` (or `uvicorn app.main:app --port 8000`) " +
        "and open http://localhost:8000/ — a static file server cannot answer /chat."
    );
    return false;
  }

  async function api(path, options = {}) {
    const url = apiUrl(path);
    try {
      const response = await fetch(url, {
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        ...options,
      });
      let data = {};
      try {
        data = await response.json();
      } catch (_) {
        data = {};
      }
      return { ok: response.ok, status: response.status, data, path };
    } catch (error) {
      return {
        ok: false,
        status: 0,
        path,
        data: { message: "Cannot reach the backend: " + error.message },
      };
    }
  }

  function showBanner(title, text) {
    const banner = $("conn-banner");
    if (!banner) return;
    $("conn-banner-title").textContent = title;
    $("conn-banner-text").textContent = " " + text;
    banner.classList.add("show");
  }

  function hideBanner() {
    const banner = $("conn-banner");
    if (banner) banner.classList.remove("show");
  }

  function checkStyles() {
    const layout = document.querySelector(".layout");
    if (!layout) return true;
    const columns = window.getComputedStyle(layout).gridTemplateColumns;
    if (!columns || columns === "none") {
      showBanner(
        "Styles failed to load",
        "The stylesheet did not apply, so the panels are stacked instead of laid out. " +
          "Hard-reload (Ctrl/Cmd+Shift+R). If it persists, confirm that " +
          "app/static/assets/css/ exists next to app/main.py and that the URL you opened " +
          "points at the FastAPI app, not at index.html on disk."
      );
      return false;
    }
    return true;
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
    const shown = apiUrl(result.path || "?");
    parts.push("\n\nrequested: " + (shown.startsWith("/") ? shown : "/" + shown));
    const detail = data.detail;
    if (detail && typeof detail === "object" && detail.hint) parts.push("\n\nhint: " + detail.hint);
    if (data.hint) parts.push("\n\nhint: " + data.hint);
    if (detail && typeof detail === "object" && detail.message) parts.push("\n\n" + detail.message);
    if (!data.hint && !(detail && detail.hint) && result.status === 404) {
      parts.push(
        "\n\nhint: this path is not served here. The API answers on /chat and /api/chat; " +
          "open the app root instead of index.html on disk."
      );
    }
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
    if (id !== "auto" && state.files.length) {
      showBanner(
        "Attachments need Auto mode",
        "the document agent reads files through /orchestrate — switch back to Auto to use them."
      );
      setTimeout(hideBanner, 6000);
    }
  }

  function updateComposerLabel() {
    const mode = MODES.find((item) => item.id === state.mode) || MODES[0];
    const pinned = state.pinnedSearch ? " · backend: " + state.pinnedSearch : "";
    const files = state.files.length ? " · " + state.files.length + " file(s)" : "";
    $("composer-mode-label").textContent =
      "Mode: " + mode.title +
      (state.mode === "search" ? pinned : "") +
      files +
      " · v" + UI_VERSION;
  }

  /* ──────────────────────── attachments ─────────────────────── */
  const MAX_FILE_BYTES = 10 * 1024 * 1024;

  function humanSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  function renderAttachments() {
    const box = $("attachments");
    const list = $("att-list");
    if (!box || !list) return;
    list.textContent = "";
    state.files.forEach((item, index) => {
      const chip = el("span", "att-chip");
      chip.appendChild(el("span", "att-name", item.name));
      chip.appendChild(el("span", "att-size", humanSize(item.size)));
      const remove = el("button", "att-remove", "✕");
      remove.type = "button";
      remove.title = "Remove";
      remove.addEventListener("click", () => {
        state.files.splice(index, 1);
        renderAttachments();
        updateComposerLabel();
      });
      chip.appendChild(remove);
      list.appendChild(chip);
    });
    box.hidden = state.files.length === 0;
    updateComposerLabel();
  }

  function addFiles(fileList) {
    const rejected = [];
    Array.from(fileList || []).forEach((file) => {
      if (file.size > MAX_FILE_BYTES) {
        rejected.push(file.name + " (" + humanSize(file.size) + " > 10 MB)");
        return;
      }
      if (state.files.some((existing) => existing.name === file.name && existing.size === file.size)) {
        return;
      }
      state.files.push({ file, name: file.name, size: file.size });
    });
    renderAttachments();
    if (rejected.length) {
      showBanner("Some files were not attached", rejected.join(" · "));
      setTimeout(hideBanner, 6000);
    }
  }

  function readAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new window.FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        const comma = result.indexOf(",");
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(reader.error || new Error("could not read " + file.name));
      reader.readAsDataURL(file);
    });
  }

  async function uploadAttachments() {
    const ids = [];
    for (const item of state.files) {
      let payload;
      try {
        payload = await readAsBase64(item.file);
      } catch (error) {
        return { error: "Could not read " + item.name + ": " + error.message };
      }
      const result = await api("/files", {
        method: "POST",
        body: JSON.stringify({ name: item.name, data_b64: payload }),
      });
      if (!result.ok) {
        return { error: errorText(result) };
      }
      ids.push(result.data.id);
    }
    return { ids };
  }

  /* ───────────────── orchestration rendering ────────────────── */
  function agentStatusTone(status) {
    if (status === "ok") return "completed";
    if (status === "failed") return "failed";
    return "queued";
  }

  function renderOrchestration(container, data) {
    const orch = data.orchestration;
    if (!orch) return;

    const panel = el("div", "orch-panel");
    const head = el("div", "orch-head");
    head.appendChild(el("span", "orch-title", "Manager plan"));
    head.appendChild(el("span", "orch-chip", orch.strategy + " planner"));
    head.appendChild(el("span", "orch-chip", orch.plan.parallel ? "parallel · " + orch.agents.length + " agents" : "single agent"));
    if (orch.synthesized) head.appendChild(el("span", "orch-chip good", "synthesised"));
    head.appendChild(el("span", "orch-time", orch.parallel_ms + " ms parallel · " + orch.latency_ms + " ms total"));
    panel.appendChild(head);
    panel.appendChild(el("div", "orch-summary", orch.plan.summary));

    const steps = el("div", "orch-steps");
    (orch.agents || []).forEach((agent) => {
      const card = el("div", "orch-step");
      const top = el("div", "orch-step-head");
      top.appendChild(el("span", "status-dot " + agentStatusTone(agent.status)));
      top.appendChild(el("span", "orch-agent", agent.label || agent.agent));
      top.appendChild(el("span", "orch-status " + agentStatusTone(agent.status), agent.status));
      top.appendChild(el("span", "orch-ms", (agent.latency_ms || 0) + " ms"));
      if (agent.model) top.appendChild(el("span", "orch-model", agent.provider + "/" + agent.model));
      top.addEventListener("click", () => card.classList.toggle("open"));
      card.appendChild(top);

      const body = el("div", "orch-step-body");
      if (agent.reason) body.appendChild(el("div", "orch-reason", "why: " + agent.reason));
      if (agent.task) body.appendChild(el("div", "orch-task", agent.task));
      const text = agent.status === "failed"
        ? agent.error + (agent.hint ? "\n\n" + agent.hint : "")
        : agent.status === "skipped"
          ? agent.detail || "skipped"
          : agent.answer || agent.detail || "(no output)";
      body.appendChild(el("pre", "orch-answer", text));
      if (agent.sources && agent.sources.length) {
        body.appendChild(el("div", "orch-sources", "sources: " + agent.sources.join(" · ")));
      }
      card.appendChild(body);
      steps.appendChild(card);
    });
    panel.appendChild(steps);

    if (orch.files && orch.files.length) {
      panel.appendChild(
        el("div", "orch-files", "files: " + orch.files.map((f) => f.name).join(", "))
      );
    }
    (orch.notes || []).forEach((note) => panel.appendChild(el("div", "orch-note", note)));
    container.appendChild(panel);
  }

  function renderAgentsPanel(info) {
    state.agents = info;
    const summary = $("agents-summary");
    const list = $("agents-list");
    if (!summary || !list) return;
    if (!info) {
      summary.textContent = "Agent roster unavailable.";
      return;
    }
    summary.textContent =
      (info.planner || "heuristic") + " planner · synthesis " +
      (info.synthesize ? "on" : "off") + " · " + (info.parallel || 4) + " in parallel";
    summary.className = "diag-summary";
    list.textContent = "";
    (info.agents || []).forEach((agent) => {
      const active = (info.roster || []).includes(agent.name);
      const row = el("div", "agent-row" + (active ? "" : " disabled"));
      row.appendChild(el("span", "agent-name", agent.label));
      row.appendChild(el("span", "agent-kind", agent.name));
      row.title = agent.description;
      list.appendChild(row);
    });
  }

  async function loadAgents() {
    const result = await api("/agents");
    renderAgentsPanel(result.ok ? result.data : null);
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

  function appendUser(text, fileNames) {
    hideEmptyState();
    const message = el("div", "message user");
    const avatar = el("div", "avatar", "You");
    const body = el("div", "body");
    body.appendChild(el("div", "msg-role", "You"));
    body.appendChild(el("div", "msg-text", text));
    if (fileNames && fileNames.length) {
      body.appendChild(el("div", "msg-files", "📎 " + fileNames.join(" · ")));
    }
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
    if (mode === "auto") {
      renderOrchestration(body, data);
    } else {
      renderTimeline(body, data);
    }
    renderSources(body, data.sources);
    if (mode === "auto" && data.orchestration) {
      const orch = data.orchestration;
      const bits = [];
      if (orch.model) bits.push(orch.provider + "/" + orch.model);
      bits.push(orch.strategy + " planner");
      bits.push(orch.agents.length + " agent(s)");
      bits.push(orch.parallel_ms + " ms parallel · " + orch.latency_ms + " ms total");
      body.appendChild(el("div", "tl-meta", bits.join(" · ")));
    } else {
      renderMeta(body, data);
    }
    scrollChat();

    const run = {
      id: "run-" + Date.now(),
      ts: new Date().toISOString(),
      mode,
      prompt,
      provider: data.provider || "",
      model: data.model || "",
      latency_ms: data.latency_ms || 0,
      steps: data.steps || (data.orchestration ? data.orchestration.agents.length : 0),
      agents: data.orchestration ? data.orchestration.plan.agents.join(",") : "",
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
        run.mode + (run.agents ? " · " + run.agents : "") +
          " · " + (run.provider || "search") + " · " + run.latency_ms + " ms"
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
    const hasFiles = state.files.length > 0;
    appendUser(text, state.files.map((item) => item.name));
    input.value = "";
    const skeleton = appendAssistantSkeleton();

    let result;
    try {
      if (mode === "auto") {
        let fileIds = [];
        if (hasFiles) {
          skeleton.text.textContent = " uploading " + state.files.length + " file(s)…";
          const uploaded = await uploadAttachments();
          if (uploaded.error) {
            result = { ok: false, status: 0, path: "/files", data: { message: uploaded.error } };
            state.busy = false;
            $("btn-send").disabled = false;
            $("btn-send").textContent = sendLabel;
            finishAssistant(skeleton, result, text, mode);
            return;
          }
          fileIds = uploaded.ids;
          state.files = [];
          renderAttachments();
        }
        skeleton.text.textContent = " thinking · planning agents…";
        result = await api("/orchestrate", {
          method: "POST",
          body: JSON.stringify({
            message: text,
            history: historyPayload(),
            file_ids: fileIds,
          }),
        });
      } else if (mode === "search") {
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
    } catch (error) {
      result = {
        ok: false,
        status: 0,
        path: mode === "auto" ? "/orchestrate" : "/chat",
        data: { message: "Request failed: " + (error && error.message ? error.message : error) },
      };
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
    scrollChat();
  }

  function clearSession() {
    state.messages = [];
    state.files = [];
    renderAttachments();
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
    const retry = $("conn-banner-retry");
    if (retry) {
      retry.addEventListener("click", async () => {
        hideBanner();
        const online = await negotiate();
        if (online) {
          loadHealth();
          loadProviders();
        }
      });
    }
    $("btn-send").addEventListener("click", send);

    // attachments: button, file picker, drag & drop on the whole console
    const fileInput = $("file-input");
    if (fileInput) {
      $("btn-attach").addEventListener("click", () => fileInput.click());
      fileInput.addEventListener("change", () => {
        addFiles(fileInput.files);
        fileInput.value = "";
      });
    }
    const dropTarget = document.body;
    ["dragenter", "dragover"].forEach((name) =>
      dropTarget.addEventListener(name, (event) => {
        if (!event.dataTransfer || !event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        document.body.classList.add("dragging");
      })
    );
    ["dragleave", "drop"].forEach((name) =>
      dropTarget.addEventListener(name, (event) => {
        if (name === "dragleave" && event.relatedTarget) return;
        document.body.classList.remove("dragging");
        if (name !== "drop") return;
        event.preventDefault();
        if (event.dataTransfer && event.dataTransfer.files.length) {
          addFiles(event.dataTransfer.files);
        }
      })
    );
    const refreshAgents = $("btn-refresh-agents");
    if (refreshAgents) refreshAgents.addEventListener("click", loadAgents);
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

  async function boot() {
    renderModes();
    renderAttachments();
    renderHistory();
    wire();
    checkStyles();

    const online = await negotiate();
    if (!online) {
      renderSearchList([]);
      $("badge-llm").textContent = "LLM: offline";
      $("badge-llm").className = "badge bad";
      $("badge-search").textContent = "Search: offline";
      $("badge-search").className = "badge bad";
      renderAgentsPanel(null);
    } else {
      loadHealth();
      loadProviders();
      loadAgents();
    }

    if (!state.runs.length) {
      showInspector(
        {
          hint: "Nothing inspected yet.",
          modes: MODES.map((mode) => mode.id),
          endpoints: [
            "/health", "/providers", "/agents", "/chat", "/orchestrate",
            "/files", "/search", "/diagnose", "/search/diagnose",
          ],
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
