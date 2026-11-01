/* ═══════════════════════════════════════════════════════════════
   Agetnic Orchestration Sandbox — test-harness frontend
   Talks to the FastAPI backend on the same origin:
     GET  /health /providers /providers/{name}/models /diagnose
     GET  /sessions /sessions/{id}   (harness memory)
     POST /chat /search /orchestrate
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

  const STORAGE = {
    runs: "agetnic.runs.v1",
    mode: "agetnic.mode.v1",
    session: "agetnic.session.v1",
  };
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
    sessions: [],
    sessionId: null,
    session: null,
    sessionTurns: 0,
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
    updateSessionPill();
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
  const STAGE_ICON = {
    run: "▶",
    manager: "M",
    planner: "P",
    agent: "A",
    tool: "⚙",
    synthesis: "✎",
    merge: "⇄",
    done: "✓",
  };

  function agentStatusTone(status) {
    if (status === "ok") return "completed";
    if (status === "failed") return "failed";
    return "queued";
  }

  function actorTone(actor) {
    return ["manager", "planner"].includes(actor) ? "core" : "agent-" + actor;
  }

  function detailText(detail, key) {
    const value = detail[key];
    if (value === null || value === undefined) return "";
    return typeof value === "string" ? value : JSON.stringify(value);
  }

  function preBlock(text, cls) {
    const pre = el("pre", "trace-pre" + (cls ? " " + cls : ""));
    pre.textContent = text;
    return pre;
  }

  function renderMessages(messages) {
    const box = el("div", "trace-messages");
    (messages || []).forEach((message) => {
      const row = el("div", "trace-message " + (message.role || "user"));
      row.appendChild(el("span", "trace-role", message.role || "?"));
      row.appendChild(preBlock(message.content || "", "msg"));
      box.appendChild(row);
    });
    return box;
  }

  function renderJson(value, label) {
    const box = el("div", "trace-json-block");
    if (label) box.appendChild(el("div", "trace-kv-label", label));
    box.appendChild(preBlock(JSON.stringify(value, null, 2)));
    return box;
  }

  function renderChips(pairs) {
    const row = el("div", "trace-chips");
    pairs.forEach(([label, value]) => {
      if (value === undefined || value === null || value === "" || value === false) return;
      row.appendChild(el("span", "trace-chip", label + ": " + String(value).slice(0, 160)));
    });
    return row.childElementCount ? row : null;
  }

  function renderEventBody(event) {
    const detail = event.detail || {};
    const body = el("div", "trace-body");
    let rendered = false;

    if (detail.messages) {
      body.appendChild(renderMessages(detail.messages));
      rendered = true;
    }
    if (detail.content) {
      body.appendChild(el("div", "trace-kv-label", "output"));
      body.appendChild(preBlock(detail.content));
      rendered = true;
    }
    const pairs = [
      ["model", detail.provider && detail.model ? detail.provider + "/" + detail.model : detail.model],
      ["step", detail.step],
      ["finish", detail.finish_reason],
      ["task", detail.task],
      ["why", detail.reason],
      ["note", detail.note],
      ["error", detail.error],
      ["query", detail.query],
      ["excerpts", detail.excerpts],
      ["context chars", detail.context_chars],
      ["wave", detail.wave],
      ["agents", Array.isArray(detail.agents) ? detail.agents.join(", ") : detail.agents],
      ["from", Array.isArray(detail.from_agents) ? detail.from_agents.join(", ") : detail.from_agents],
      ["status", detail.status],
      ["latency", detail.latency_ms !== undefined ? detail.latency_ms + " ms" : ""],
      ["files", Array.isArray(detail.files) ? detail.files.map((f) => (f.name || f)).join(", ") : detail.files],
      ["roster", Array.isArray(detail.roster) ? detail.roster.join(", ") : detail.roster],
      ["planner", detail.planner],
      ["review", detail.review],
      ["synthesize", detail.synthesize],
      ["has model", detail.has_model],
    ];
    const chips = renderChips(pairs);
    if (chips) {
      body.appendChild(chips);
      rendered = true;
    }
    if (detail.summary && detail.steps) {
      body.appendChild(el("div", "trace-kv-label", "plan"));
      body.appendChild(preBlock(detail.summary));
    }
    if (detail.requests) {
      body.appendChild(renderJson(detail.requests, "follow-up requested"));
      rendered = true;
    }
    if (detail.steps && Array.isArray(detail.steps)) {
      (detail.steps || []).forEach((step) => {
        const card = el("div", "trace-step");
        card.appendChild(el("span", "trace-chip strong", step.agent));
        card.appendChild(el("div", "trace-step-task", step.task || ""));
        if (step.depends_on && step.depends_on.length) {
          card.appendChild(el("div", "trace-step-dep", "waits for " + step.depends_on.join(", ")));
        }
        if (step.reason) card.appendChild(el("div", "trace-step-why", "why: " + step.reason));
        body.appendChild(card);
      });
      rendered = true;
    }
    if (detail.arguments) {
      body.appendChild(renderJson(detail.arguments, "tool arguments"));
      rendered = true;
    }
    if (detail.output) {
      body.appendChild(renderJson(detail.output, "tool result"));
      rendered = true;
    }
    if (detail.results && Array.isArray(detail.results)) {
      body.appendChild(renderJson(detail.results, "results"));
      rendered = true;
    }
    if (detail.computed) {
      body.appendChild(el("div", "trace-kv-label", "computed by code"));
      body.appendChild(preBlock(detail.computed));
      rendered = true;
    }
    if (detail.excerpts && Array.isArray(detail.excerpts)) {
      body.appendChild(renderJson(detail.excerpts, "retrieved excerpts"));
      rendered = true;
    }
    if (detail.parsed !== undefined) {
      body.appendChild(renderChips([["plan accepted", detail.parsed], ["reason", detail.reason]]));
      rendered = true;
    }
    if (detail.sources && Array.isArray(detail.sources) && detail.sources.length) {
      body.appendChild(el("div", "trace-sources", "sources: " + detail.sources.join(" · ")));
      rendered = true;
    }
    if (!rendered) {
      body.appendChild(renderJson(detail, ""));
    }
    return body;
  }

  function renderTraceEvent(event, expandAll) {
    const detail = event.detail || {};
    const row = el("div", "trace-row stage-" + event.stage);
    if (expandAll) row.classList.add("open");

    const head = el("div", "trace-row-head");
    head.appendChild(el("span", "trace-icon", STAGE_ICON[event.stage] || "•"));
    head.appendChild(el("span", "trace-actor " + actorTone(event.actor), event.actor));
    head.appendChild(el("span", "trace-title", event.title || ""));
    if (detail.latency_ms !== undefined) {
      head.appendChild(el("span", "trace-ms", detail.latency_ms + " ms"));
    } else {
      head.appendChild(el("span", "trace-ms", event.offset_ms + " ms"));
    }
    head.appendChild(el("span", "trace-seq", "#" + event.seq));
    const body = renderEventBody(event);
    if (!body.childElementCount) {
      row.appendChild(head);
      return row;
    }
    head.addEventListener("click", () => row.classList.toggle("open"));
    row.appendChild(head);
    row.appendChild(body);
    return row;
  }

  function renderPlanSummary(orch) {
    const plan = orch.plan || {};
    const box = el("div", "plan-summary");
    const top = el("div", "plan-top");
    top.appendChild(el("span", "plan-intent", plan.intent || "plan"));
    top.appendChild(el("span", "plan-chip", "selection: " + (plan.strategy || "?")));
    top.appendChild(el("span", "plan-chip", "briefs: " + (plan.detail_source || "rules")));
    if (orch.model_calls) top.appendChild(el("span", "plan-chip", orch.model_calls + " model call(s)"));
    box.appendChild(top);
    if (plan.summary) box.appendChild(el("div", "plan-text", plan.summary));
    (plan.steps || []).forEach((step) => {
      const line = el("div", "plan-step");
      line.appendChild(el("span", "plan-agent", step.label || step.agent));
      line.appendChild(el("span", "plan-wave", "wave " + (step.wave || 1)));
      if (step.depends_on && step.depends_on.length) {
        line.appendChild(el("span", "plan-dep", "← " + step.depends_on.join(", ")));
      }
      box.appendChild(line);
    });
    (orch.waves || []).forEach((wave) => {
      const line = el("div", "plan-wave-line");
      line.textContent =
        "wave " + wave.wave + ": " + (wave.agents || []).join(" + ") +
        (wave.depends_on && Object.keys(wave.depends_on).length
          ? "  (uses " + Object.entries(wave.depends_on).map(([a, d]) => a + " ← " + d.join(",")).join("; ") + ")"
          : "");
      box.appendChild(line);
    });
    return box;
  }

  function renderOrchestration(container, data) {
    const orch = data.orchestration;
    if (!orch) return;

    const panel = el("div", "orch-panel");
    const head = el("div", "orch-head");
    head.appendChild(el("span", "orch-title", "How this answer was built"));
    head.appendChild(el("span", "orch-chip", (orch.plan.agents || []).join(" → ") || "no agents"));
    head.appendChild(el("span", "orch-chip", (orch.waves || []).length + " wave(s)"));
    if (orch.synthesized) head.appendChild(el("span", "orch-chip good", "manager synthesis"));
    head.appendChild(
      el("span", "orch-time", orch.parallel_ms + " ms agents · " + orch.latency_ms + " ms total")
    );
    panel.appendChild(head);

    panel.appendChild(renderPlanSummary(orch));

    const tools = el("div", "orch-toolbar");
    const expand = el("button", "ghost-btn", "Expand all steps");
    expand.type = "button";
    const collapse = el("button", "ghost-btn", "Collapse");
    collapse.type = "button";
    const raw = el("button", "ghost-btn", "Raw JSON");
    raw.type = "button";
    tools.appendChild(expand);
    tools.appendChild(collapse);
    tools.appendChild(raw);
    if (orch.run_id) tools.appendChild(el("span", "orch-runid", orch.run_id));
    panel.appendChild(tools);

    const timeline = el("div", "trace-timeline");
    (orch.trace || []).forEach((event) => timeline.appendChild(renderTraceEvent(event, false)));
    panel.appendChild(timeline);

    expand.addEventListener("click", () =>
      panel.querySelectorAll(".trace-row").forEach((node) => node.classList.add("open"))
    );
    collapse.addEventListener("click", () =>
      panel.querySelectorAll(".trace-row").forEach((node) => node.classList.remove("open"))
    );
    raw.addEventListener("click", () => showInspector(data, "Raw orchestration payload"));

    if (orch.review && orch.review.asked) {
      panel.appendChild(
        el(
          "div",
          "orch-note",
          "manager review: " + (orch.review.note || orch.review.reason || "done") +
            (orch.review.follow_up && orch.review.follow_up.length
              ? " — asked for " + orch.review.follow_up.map((f) => f.agent).join(", ")
              : " — no extra work needed")
        )
      );
    }
    if (orch.files && orch.files.length) {
      panel.appendChild(
        el("div", "orch-files", "files: " + orch.files.map((f) => f.name).join(", "))
      );
    }
    (orch.notes || []).forEach((note) => panel.appendChild(el("div", "orch-note", note)));
    if (orch.log_path) {
      panel.appendChild(el("div", "orch-log", "full unclipped trace: " + orch.log_path));
    }
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

  /* ─────────────────── orchestration run log (log base) ───────────── */
  async function loadRunLog() {
    const list = $("runs-list");
    const summary = $("runs-summary");
    if (!list) return;
    const result = await api("/runs?limit=8");
    if (!result.ok) {
      if (summary) summary.textContent = "Run log unavailable (GET /runs).";
      return;
    }
    const runs = result.data.runs || [];
    if (summary) {
      summary.textContent = runs.length
        ? runs.length + " recent run(s) · " + (result.data.dir || "logs/orchestrator")
        : "No runs yet — send a prompt in Auto mode.";
      summary.className = "diag-summary";
    }
    list.textContent = "";
    runs.forEach((run) => {
      const row = el("div", "run-row");
      row.appendChild(el("span", "run-intent", run.intent || "run"));
      row.appendChild(el("span", "run-agents", (run.agents || []).join(",")));
      row.appendChild(el("span", "run-ms", (run.latency_ms || 0) + " ms"));
      row.title = run.message + "\n" + run.run_id;
      row.addEventListener("click", () => openRun(run.run_id));
      list.appendChild(row);
    });
  }

  async function openRun(runId) {
    const result = await api("/runs/" + encodeURIComponent(runId));
    if (!result.ok) {
      showInspector({ error: "run not found", run_id: runId }, "Run " + runId);
      return;
    }
    showInspector(result.data, "Logged run " + runId + " (full, unclipped)");
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

  // The text of one answer. Search-only turns are formatted here and mirrored
  // by app/memory.py, so a stored turn replays with exactly the same words.
  function answerText(data, mode) {
    if (mode === "search") {
      if (data.answer) return data.answer; // restored turns carry their text
      const lines = (data.results || []).map(
        (item, index) =>
          index + 1 + ". " + item.title + "\n   " + item.url + "\n   " + (item.snippet || "")
      );
      return (
        "Search backend: " + data.provider + " · " + data.count + " result(s) · " +
        data.latency_ms + " ms\n\n" +
        (lines.length ? lines.join("\n\n") : "No results.")
      );
    }
    return data.answer || "";
  }

  // One renderer for a live answer and for one read back out of memory:
  // same bubble, same "How this answer was built" panel, same meta line.
  function renderAnswerInto(body, data, mode, existingText) {
    const text = existingText || el("div", "msg-text");
    text.textContent = answerText(data, mode) || "(empty answer)";
    if (text.parentNode !== body) body.appendChild(text);

    if (mode === "auto" && data.orchestration) {
      renderOrchestration(body, data);
    } else {
      renderTimeline(body, data);
    }
    renderSources(body, data.sources);

    if (mode === "auto" && data.orchestration) {
      const orch = data.orchestration;
      const bits = [];
      if (orch.model) bits.push(orch.provider + "/" + orch.model);
      bits.push(orch.strategy + " planner / " + (orch.plan.detail_source || "rules") + " briefs");
      bits.push((orch.plan.agents || []).length + " agent(s) in " + (orch.waves || []).length + " wave(s)");
      if (orch.model_calls) bits.push(orch.model_calls + " model call(s)");
      bits.push(orch.parallel_ms + " ms agents · " + orch.latency_ms + " ms total");
      body.appendChild(el("div", "tl-meta", bits.join(" · ")));
    } else {
      renderMeta(body, data);
    }
    return text;
  }

  function errorStub(turn) {
    const error = (turn.assistant || {}).error || {};
    const body = error.body || {};
    const mode = turn.mode || "auto";
    return {
      ok: false,
      status: error.status || body.status || 0,
      path: mode === "auto" ? "/orchestrate" : mode === "search" ? "/search" : "/chat",
      data: Object.keys(body).length ? body : { message: "This turn failed when it was sent." },
    };
  }

  // One stored turn: the user bubble, then the assistant bubble rendered from
  // the payload that was saved — no network, no regeneration.
  function appendStoredTurn(turn) {
    const user = turn.user || {};
    const files = (user.files || []).map((file) => file.name).filter(Boolean);
    appendUser(user.text || "", files);

    const message = el("div", "message assistant");
    const avatar = el("div", "avatar", "AI");
    const body = el("div", "body");
    body.appendChild(el("div", "msg-role", "Assistant"));
    message.appendChild(avatar);
    message.appendChild(body);
    $("chat").appendChild(message);

    const assistant = turn.assistant || {};
    if (assistant.ok === false) {
      body.appendChild(el("div", "msg-text", errorText(errorStub(turn))));
    } else {
      const data = assistant.response || {
        answer: assistant.text || assistant.answer || "",
        sources: [],
      };
      renderAnswerInto(body, data, turn.mode || "auto");
    }
    return message;
  }

  function renderEmptyState(text) {
    const chat = $("chat");
    chat.textContent = "";
    const empty = el("div", "empty-state");
    empty.id = "empty-state";
    empty.appendChild(el("h2", null, text ? "New conversation" : "Orchestration test console"));
    empty.appendChild(
      el(
        "p",
        null,
        text ||
          "Auto is the full architecture: a manager plans, the agents run in parallel waves and the manager writes the answer — every step stays open under the reply."
      )
    );
    empty.appendChild(
      el(
        "p",
        "muted",
        "This conversation is remembered in memory/ as you go. Reopen it later from the Memory panel and the model gets the earlier turns back."
      )
    );
    chat.appendChild(empty);
  }

  function formatWhen(iso) {
    if (!iso) return "";
    const when = new Date(iso);
    if (isNaN(when.getTime())) return iso;
    const seconds = Math.round((Date.now() - when.getTime()) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
    if (seconds < 86400) return Math.round(seconds / 3600) + " h ago";
    if (seconds < 86400 * 7) return Math.round(seconds / 86400) + " d ago";
    return when.toISOString().slice(0, 10);
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
    renderAnswerInto(body, data, mode, text);
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

  /* ─────────────────────── harness memory ──────────────────── */
  // The conversation lives on the server (memory/), not only in this tab:
  // reopening a session re-renders every turn from the stored payload.
  function updateSessionPill() {
    const pill = $("session-pill");
    if (!pill) return;
    if (!state.sessionId) {
      pill.textContent = "new conversation";
      pill.className = "session-pill";
      pill.title = "The next message starts a conversation in memory/";
      return;
    }
    pill.textContent =
      "memory: " + (state.sessionTurns || 0) + " turn(s) · " + state.sessionId;
    pill.className = "session-pill live";
    pill.title =
      "Session " + state.sessionId +
      " — stored in memory/, restored on reload, and sent as context with the next message";
  }

  function saveSessionId(sessionId) {
    try {
      if (sessionId) store.setItem(STORAGE.session, sessionId);
      else store.removeItem(STORAGE.session);
    } catch (_) {
      /* storage may be disabled — the session still lives on the server */
    }
  }

  async function loadSessions() {
    const summary = $("sessions-summary");
    const list = $("sessions-list");
    if (!list) return;
    const { ok, data } = await api("/sessions?limit=25");
    if (!ok) {
      if (summary) summary.textContent = "Memory unavailable — " + (data.message || "backend down");
      return;
    }
    state.sessions = data.sessions || [];
    if (summary) {
      if (data.enabled === false) {
        summary.textContent = "Memory is off (HARNESS_MEMORY=false)";
      } else {
        const turns = state.sessions.reduce((sum, item) => sum + (item.turns || 0), 0);
        summary.textContent =
          state.sessions.length + " session(s) · " + turns + " turn(s) · " + (data.dir || "memory/");
        summary.className = "diag-summary";
      }
    }
    renderSessions();
  }

  function renderSessions() {
    const list = $("sessions-list");
    if (!list) return;
    list.textContent = "";
    if (!state.sessions.length) {
      list.appendChild(el("li", "muted small", "No conversations in memory yet."));
      return;
    }
    state.sessions.forEach((item) => {
      const row = el("li", "session-item" + (item.session_id === state.sessionId ? " active" : ""));
      const main = el("div", "session-main");
      main.appendChild(el("div", "session-title", item.title || item.session_id));
      const bits = [(item.turns || 0) + " turn(s)", formatWhen(item.updated_at)];
      if (item.agents && item.agents.length) bits.push(item.agents.join(" → "));
      else if (item.modes && item.modes.length) bits.push(item.modes.join(" · "));
      main.appendChild(el("div", "session-meta", bits.join(" · ")));
      row.appendChild(main);
      row.title = (item.last_user || "") + (item.last_answer ? "\n→ " + item.last_answer : "");
      main.addEventListener("click", () => openSession(item.session_id));

      const forget = el("button", "session-forget", "✕");
      forget.type = "button";
      forget.title = "Forget this conversation (deletes it from memory/)";
      let armed = false;
      forget.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!armed) {
          armed = true;
          forget.textContent = "delete?";
          forget.classList.add("armed");
          setTimeout(() => {
            armed = false;
            forget.textContent = "✕";
            forget.classList.remove("armed");
          }, 4000);
          return;
        }
        deleteSession(item.session_id);
      });
      row.appendChild(forget);
      list.appendChild(row);
    });
  }

  function restoreSession(record) {
    if (!record || !record.session_id) return false;
    const turns = record.turns || [];
    state.sessionId = record.session_id;
    state.session = record;
    state.sessionTurns = turns.length;
    state.messages = [];
    state.files = [];
    saveSessionId(record.session_id);
    renderAttachments();

    hideEmptyState();
    const chat = $("chat");
    chat.textContent = "";
    if (!turns.length) {
      renderEmptyState("This conversation is stored but has no messages yet — send the first one.");
    }
    turns.forEach((turn) => {
      appendStoredTurn(turn);
      // keep the local replay too: it is what gets sent if memory is switched off
      const question = (turn.user || {}).text || "";
      if (question) state.messages.push({ role: "user", content: question });
      const answer =
        (turn.assistant || {}).answer ||
        (turn.assistant || {}).text ||
        ((turn.assistant || {}).response || {}).answer ||
        "";
      if (answer) state.messages.push({ role: "assistant", content: String(answer).slice(0, 4000) });
    });
    state.messages = state.messages.slice(-12);

    updateSessionPill();
    renderSessions();
    scrollChat();
    showInspector(
      record,
      "Session " + record.session_id + " — " + turns.length + " turn(s) restored from memory, ready to continue."
    );
    return true;
  }

  async function openSession(sessionId) {
    if (!sessionId) return false;
    const { ok, data } = await api("/sessions/" + encodeURIComponent(sessionId));
    if (!ok) {
      saveSessionId(null);
      state.sessionId = null;
      state.sessionTurns = 0;
      updateSessionPill();
      return false;
    }
    return restoreSession(data);
  }

  async function deleteSession(sessionId) {
    if (!sessionId) return;
    const { ok } = await api("/sessions/" + encodeURIComponent(sessionId), { method: "DELETE" });
    if (!ok) return;
    if (state.sessionId === sessionId) newSession();
    loadSessions();
  }

  function newSession() {
    state.sessionId = null;
    state.session = null;
    state.sessionTurns = 0;
    state.messages = [];
    state.files = [];
    saveSessionId(null);
    renderAttachments();
    renderEmptyState();
    updateSessionPill();
    renderSessions();
    showInspector(
      "New conversation — memory starts with your first message (the previous one stays in memory/).",
      "Harness memory"
    );
    const prompt = $("prompt");
    if (prompt) prompt.focus();
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
            // continue the conversation that is in memory (created when null)
            session_id: state.sessionId,
          }),
        });
      } else if (mode === "search") {
        result = await api("/search", {
          method: "POST",
          body: JSON.stringify({
            query: text,
            max_results: 5,
            provider: state.pinnedSearch || null,
            session_id: state.sessionId,
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
            session_id: state.sessionId,
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

    // The server stored this turn in the harness memory and tells us which
    // session it belongs to — adopt it so the next message continues it.
    const stored = result && result.data && result.data.session;
    if (stored && stored.session_id) {
      state.sessionId = stored.session_id;
      state.sessionTurns = stored.turns || state.sessionTurns + 1;
      saveSessionId(stored.session_id);
      updateSessionPill();
      loadSessions();
    }

    state.busy = false;
    $("btn-send").disabled = false;
    $("btn-send").textContent = sendLabel;
    if (mode === "auto" && result && result.ok) loadRunLog();
    scrollChat();
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
    const refreshRuns = $("btn-refresh-runs");
    if (refreshRuns) refreshRuns.addEventListener("click", loadRunLog);
    $("btn-clear").addEventListener("click", newSession);
    const newSessionBtn = $("btn-new-session");
    if (newSessionBtn) newSessionBtn.addEventListener("click", newSession);
    const refreshSessions = $("btn-refresh-sessions");
    if (refreshSessions) refreshSessions.addEventListener("click", loadSessions);
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
      loadRunLog();
      loadSessions().then(async () => {
        const last = store.getItem(STORAGE.session);
        if (last) await openSession(last);
        updateSessionPill();
      });
    }

    if (!state.runs.length) {
      showInspector(
        {
          hint: "Nothing inspected yet.",
          modes: MODES.map((mode) => mode.id),
          endpoints: [
            "/health", "/providers", "/agents", "/chat", "/orchestrate",
            "/files", "/search", "/diagnose", "/search/diagnose",
            "/sessions", "/sessions/{id}",
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
