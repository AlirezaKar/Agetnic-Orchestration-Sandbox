/* ═══════════════════════════════════════════════════════════════
   Agetnic Orchestration Sandbox — studio frontend
   Talks to the FastAPI backend on the same origin:
     GET  /health /providers /providers/{name}/models /diagnose
     GET  /sessions /sessions/{id}   (harness memory)
     POST /chat /search /orchestrate /files
   No build step, no dependencies, no CDN: the Markdown renderer below is
   local so a conversation still reads correctly when the network is gone.
   ═══════════════════════════════════════════════════════════════ */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const MODES = [
    {
      id: "auto",
      title: "Auto",
      long: "Auto (orchestrated)",
      desc: "Manager plans, document / search / data / general agents run in parallel, manager wraps up.",
    },
    {
      id: "agent",
      title: "Agent",
      long: "Agent (tools)",
      desc: "The model decides when to call web_search / fetch_url. Full trace.",
    },
    {
      id: "answer",
      title: "Answer",
      long: "Answer only",
      desc: "Tools disabled — the model answers from its own knowledge.",
    },
    {
      id: "search",
      title: "Search",
      long: "Search only",
      desc: "Run the search chain directly (no model, no tool loop).",
    },
  ];

  const STORAGE = {
    runs: "agetnic.runs.v1",
    mode: "agetnic.mode.v1",
    session: "agetnic.session.v1",
    theme: "agetnic.theme.v1",
    layout: "agetnic.layout.v1",
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

  // Column widths: the design's defaults, overridable by dragging the
  // handles and remembered per browser. The middle column always keeps
  // ROOM_FOR_CHAT px, so a drag can never squeeze the conversation.
  const LAYOUT = {
    sidebar: { min: 248, max: 560, def: 322, cssVar: "--sidebar-width", handle: "gutter-left" },
    inspector: { min: 300, max: 680, def: 412, cssVar: "--inspector-width", handle: "gutter-right" },
  };
  const ROOM_FOR_CHAT = 360;

  const readLayout = () => {
    try {
      const parsed = JSON.parse(store.getItem(STORAGE.layout) || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  };
  const savedLayout = readLayout();
  const layout = {
    sidebar: Number(savedLayout.sidebar) || null,
    inspector: Number(savedLayout.inspector) || null,
  };

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
    // FILES_PERMISSION_PROMPT: ask before an agent may read an attached file.
    permissionPrompt: false,
    // Runs panel: "saved" = logs/orchestrator on the server, "local" = this tab.
    runsTab: "saved",
    runsSummary: "",
    historySummary: "",
    // Inspector: last payload + which tab is open.
    inspect: null,
    inspectNote: "Select a run or send a prompt to explore its execution trace and context.",
    inspectTab: "trace",
  };

  /* ────────────────────────────── helpers ───────────────────── */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function icon(paths, cls) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.7");
    if (cls) svg.setAttribute("class", cls);
    paths.forEach((d) => {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      svg.appendChild(path);
    });
    return svg;
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
    const workspace = document.querySelector(".workspace");
    if (!workspace) return true;
    const columns = window.getComputedStyle(workspace).gridTemplateColumns;
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

  /* ────────────────────────── toasts ────────────────────────── */
  function toast(text, tone) {
    const region = $("toast-region");
    if (!region) return;
    const node = el("div", "toast" + (tone ? " " + tone : ""));
    node.appendChild(icon(["M20 6 9 17l-5-5"], "tick"));
    node.appendChild(el("span", null, text));
    region.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
    setTimeout(() => {
      node.classList.remove("show");
      setTimeout(() => node.remove(), 240);
    }, 2600);
  }

  /* ───────────────────────── theme (light/dark) ────────────── */
  function applyTheme(theme) {
    const light = theme === "light";
    document.documentElement.dataset.theme = light ? "light" : "dark";
    const button = $("btn-theme");
    if (button) {
      const label = light ? "Switch to dark mode" : "Switch to light mode";
      button.setAttribute("aria-pressed", light ? "true" : "false");
      button.setAttribute("aria-label", label);
      button.title = label;
    }
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", light ? "#f2f5fb" : "#090d14");
    try {
      store.setItem(STORAGE.theme, light ? "light" : "dark");
    } catch (_) {
      /* storage may be disabled — the theme still applies for this visit */
    }
  }

  function toggleTheme() {
    applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  }

  /* ─────────────────── column widths (drag handles) ─────────── */
  function saveLayout() {
    try {
      store.setItem(STORAGE.layout, JSON.stringify(layout));
    } catch (_) {
      /* storage may be disabled — widths last for this visit */
    }
  }

  function gutterWidth() {
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--gutter-width");
    const value = parseFloat(raw);
    return Number.isFinite(value) ? value : 7;
  }

  // The width a column actually has right now: the inline drag value if any,
  // otherwise whatever the stylesheet resolved (it changes with the breakpoints).
  function currentWidth(side) {
    const workspace = $("workspace");
    if (!workspace) return LAYOUT[side].def;
    const inline = parseFloat(workspace.style.getPropertyValue(LAYOUT[side].cssVar));
    if (Number.isFinite(inline)) return inline;
    const resolved = parseFloat(
      getComputedStyle(workspace).getPropertyValue(LAYOUT[side].cssVar)
    );
    return Number.isFinite(resolved) ? resolved : LAYOUT[side].def;
  }

  function clampWidth(side, value) {
    const info = LAYOUT[side];
    const workspace = $("workspace");
    // clientWidth is 0 before layout (and in jsdom): the window is the safe bound
    const visible = (workspace && workspace.clientWidth) || window.innerWidth;
    const narrow = window.innerWidth <= 980;
    const collapsed = !!(workspace && workspace.classList.contains("inspector-collapsed"));
    // the middle column keeps ROOM_FOR_CHAT: that is the hard limit on a drag
    const budget = visible - ROOM_FOR_CHAT - gutterWidth() * 2;
    // below 980px the inspector is an overlay, so it costs the grid no width
    const inspectorShares = !narrow && !collapsed;
    const hardMax = side === "sidebar"
      ? budget - (inspectorShares ? currentWidth("inspector") : 0)
      : budget - currentWidth("sidebar");
    // a column that is already wider than the budget keeps its width: the first
    // drag must shrink it smoothly, never snap it somewhere else
    const upper = Math.max(
      Math.max(info.min, Math.min(info.max, hardMax)),
      currentWidth(side)
    );
    return Math.round(Math.min(Math.max(value, info.min), upper));
  }

  function setWidth(side, value, persist) {
    const info = LAYOUT[side];
    const width = clampWidth(side, value);
    layout[side] = width;
    const workspace = $("workspace");
    if (workspace) workspace.style.setProperty(info.cssVar, width + "px");
    const handle = $(info.handle);
    if (handle) handle.setAttribute("aria-valuenow", String(width));
    if (persist) saveLayout();
    return width;
  }

  function applyLayout() {
    const workspace = $("workspace");
    Object.keys(LAYOUT).forEach((side) => {
      const info = LAYOUT[side];
      const width = layout[side];
      if (!width) return;
      if (workspace) workspace.style.setProperty(info.cssVar, width + "px");
      const handle = $(info.handle);
      if (handle) handle.setAttribute("aria-valuenow", String(Math.round(width)));
    });
  }

  function resetWidth(side) {
    const info = LAYOUT[side];
    layout[side] = info.def;
    const workspace = $("workspace");
    if (workspace) workspace.style.removeProperty(info.cssVar);
    const handle = $(info.handle);
    if (handle) handle.setAttribute("aria-valuenow", String(info.def));
    saveLayout();
    toast("Column width reset", "");
  }

  function wireGutter(side) {
    const info = LAYOUT[side];
    const handle = $(info.handle);
    if (!handle) return;
    handle.setAttribute("aria-valuemin", String(info.min));
    handle.setAttribute("aria-valuemax", String(info.max));
    handle.setAttribute("aria-valuenow", String(Math.round(layout[side] || info.def)));
    let dragging = false;

    const measure = (event) => {
      const workspace = $("workspace");
      if (!workspace) return 0;
      const rect = workspace.getBoundingClientRect();
      return side === "sidebar" ? event.clientX - rect.left : rect.right - event.clientX;
    };

    const onMove = (event) => {
      if (!dragging) return;
      setWidth(side, measure(event), false);
      event.preventDefault();
    };

    const stop = () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove("dragging");
      document.body.classList.remove("resizing");
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", stop);
      document.removeEventListener("pointercancel", stop);
      saveLayout();
    };

    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      dragging = true;
      handle.classList.add("dragging");
      document.body.classList.add("resizing");
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", stop);
      document.addEventListener("pointercancel", stop);
      event.preventDefault();
    });

    handle.addEventListener("dblclick", () => resetWidth(side));

    handle.addEventListener("keydown", (event) => {
      const grow = side === "sidebar" ? "ArrowRight" : "ArrowLeft";
      const shrink = side === "sidebar" ? "ArrowLeft" : "ArrowRight";
      if (event.key !== grow && event.key !== shrink) {
        if (event.key === "Home" || event.key === "Enter") {
          event.preventDefault();
          resetWidth(side);
        }
        return;
      }
      event.preventDefault();
      const step = event.shiftKey ? 48 : 12;
      const base = layout[side] || info.def;
      setWidth(side, base + (event.key === grow ? step : -step), true);
    });
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
    if (!badge) return;
    badge.textContent = text;
    const pill = badge.closest(".status-pill");
    if (pill) {
      pill.classList.remove("success", "bad", "warn");
      // the design system calls the positive state "success"
      if (tone === "ok") pill.classList.add("success");
      else if (tone) pill.classList.add(tone);
    }
  }

  function setHealth(key, value, status) {
    const node = $("health-" + key);
    if (node) node.textContent = value;
    if (key === "state" && node) node.textContent = value;
    if (status !== undefined) {
      const card = document.querySelector(".health-live");
      if (card) {
        card.classList.remove("ok", "bad");
        if (status) card.classList.add(status);
      }
    }
  }

  async function loadHealth() {
    const { ok, data } = await api("/health");
    if (!ok) {
      setBadge("badge-llm", "LLM  offline", "bad");
      setBadge("badge-search", "SEARCH  unknown", "bad");
      setHealth("model", "offline");
      setHealth("search", "unknown");
      setHealth("state", "backend down", "bad");
      return;
    }
    const models = data.model_chain || [];
    const searches = data.search_chain || [];
    setBadge(
      "badge-llm",
      models.length ? "LLM  " + models.join(" → ") : "LLM  no key",
      models.length ? "ok" : "bad"
    );
    setBadge(
      "badge-search",
      searches.length ? "SEARCH  " + searches.join(" → ") : "SEARCH  none",
      searches.length ? "ok" : "warn"
    );
    setHealth("model", models[0] || "no key", models.length ? "ok" : "bad");
    setHealth("search", searches[0] || "none");
    setHealth("state", "connected", "ok");
    document.body.dataset.modelReady = models.length ? "1" : "0";
    const permission = Boolean((data.files || {}).permission_prompt);
    if (permission !== state.permissionPrompt) {
      state.permissionPrompt = permission;
      // a file added before the flag was known starts as "waiting to be allowed"
      renderAttachments();
    }
  }

  /* ──────────────────────────── modes ───────────────────────── */
  function renderModes() {
    const list = $("mode-list");
    if (!list) return;
    list.textContent = "";
    MODES.forEach((mode) => {
      const button = el("button", "mode-btn" + (state.mode === mode.id ? " active" : ""));
      button.type = "button";
      button.dataset.mode = mode.id;
      button.title = mode.desc;
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
    const label = $("composer-mode-label");
    if (label) {
      const bits = [];
      if (state.mode === "auto") bits.push("Manager-led · parallel agents");
      else if (state.mode === "search") {
        bits.push("Chain " + (state.pinnedSearch || "auto"));
      } else bits.push(mode.long);
      if (state.files.length) bits.push(state.files.length + " file(s)");
      const waiting = state.files.filter((item) => item.consent === false).length;
      if (waiting && state.permissionPrompt) bits.push(waiting + " awaiting permission");
      bits.push("v" + UI_VERSION);
      label.textContent = bits.join(" · ");
    }
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
      const chip = el(
        "span",
        "att-chip attachment-chip" + (item.consent === false ? " att-locked" : "")
      );
      chip.appendChild(el("span", "att-name", item.name));
      chip.appendChild(el("span", "att-size", humanSize(item.size)));
      if (item.consent === false) chip.appendChild(el("span", "att-lock", "awaiting permission"));
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
    renderConsent();
    updateComposerLabel();
  }

  /* Permission prompt: the file is only handed to an agent after "Allow".
     FILES_PERMISSION_PROMPT=false (the default) never shows this panel. */
  function pendingFiles() {
    return state.files.filter((item) => item.consent === false);
  }

  function renderConsent() {
    const panel = $("consent");
    const list = $("consent-list");
    if (!panel || !list) return;
    const pending = state.permissionPrompt ? pendingFiles() : [];
    list.textContent = "";
    panel.hidden = pending.length === 0;
    if (!pending.length) return;
    $("consent-count").textContent =
      pending.length === 1 ? "1 file needs your permission" : pending.length + " files need your permission";
    pending.forEach((item) => {
      const row = el("div", "consent-row");
      const who = el("div", "consent-who");
      who.appendChild(el("span", "consent-name", item.name));
      who.appendChild(el("span", "consent-size", humanSize(item.size) + " · the agent cannot read it yet"));
      row.appendChild(who);
      const allow = el("button", "consent-allow", "Allow");
      allow.type = "button";
      allow.addEventListener("click", () => decideFile(item, true));
      const deny = el("button", "consent-deny", "Don't allow");
      deny.type = "button";
      deny.addEventListener("click", () => decideFile(item, false));
      row.appendChild(allow);
      row.appendChild(deny);
      list.appendChild(row);
    });
  }

  function decideFile(item, allowed) {
    if (allowed) {
      item.consent = true;
    } else {
      state.files = state.files.filter((existing) => existing !== item);
      showBanner("File not allowed", item.name + " will not be sent.");
      setTimeout(hideBanner, 5000);
    }
    renderAttachments();
    updateComposerLabel();
  }

  function decideAll(allowed) {
    pendingFiles().slice().forEach((item) => decideFile(item, allowed));
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
      state.files.push({
        file,
        name: file.name,
        size: file.size,
        consent: !state.permissionPrompt,
      });
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
    const warnings = [];
    for (const item of state.files) {
      let payload;
      try {
        payload = await readAsBase64(item.file);
      } catch (error) {
        return { error: "Could not read " + item.name + ": " + error.message };
      }
      const result = await api("/files", {
        method: "POST",
        body: JSON.stringify({
          name: item.name,
          data_b64: payload,
          // explicit answer: the server refuses a file nobody allowed
          consent: item.consent !== false,
        }),
      });
      if (!result.ok) {
        return { error: item.name + ": " + errorText(result) };
      }
      if (result.data && result.data.needs_consent) {
        return { error: item.name + " is waiting for permission — allow it first" };
      }
      if (result.data && result.data.warning) {
        warnings.push(item.name + ": " + result.data.warning);
      }
      ids.push(result.data.id);
    }
    return { ids, warnings };
  }

  /* ═════════════════════ local Markdown renderer ═════════════════
     Offline-safe (no CDN): headings, fenced code, tables, lists,
     blockquotes/callouts, rules, paragraphs, inline code / bold /
     italic / links / bare URLs. DOM-only, never innerHTML, so model
     output cannot inject markup.
     ═══════════════════════════════════════════════════════════════ */
  // bold → code → *emphasis* → _emphasis_ → [label](url) → bare URL.
  // The underscore form is boundary-anchored so LLM_PROVIDER stays literal.
  const INLINE = /\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*|\b_[^_\n]+_\b|\[[^\]]+\]\([^)]+\)|https?:\/\/[^\s<)]+/g;

  function inlineInto(parent, text) {
    let last = 0;
    const source = String(text || "");
    source.replace(INLINE, (match, offset) => {
      if (offset > last) parent.appendChild(document.createTextNode(source.slice(last, offset)));
      if (match.startsWith("**")) {
        parent.appendChild(el("strong", null, match.slice(2, -2)));
      } else if (match.startsWith("`")) {
        parent.appendChild(el("code", null, match.slice(1, -1)));
      } else if (match.startsWith("[")) {
        const split = match.indexOf("](");
        const label = match.slice(1, split);
        const url = match.slice(split + 2, -1);
        const link = el("a", null, label);
        link.href = url;
        link.target = "_blank";
        link.rel = "noreferrer";
        parent.appendChild(link);
      } else if (match.startsWith("http")) {
        const link = el("a", null, match.replace(/^https?:\/\//, "").slice(0, 60));
        link.href = match;
        link.target = "_blank";
        link.rel = "noreferrer";
        parent.appendChild(link);
      } else {
        parent.appendChild(el("em", null, match.slice(1, -1)));
      }
      last = offset + match.length;
      return match;
    });
    if (last < source.length) parent.appendChild(document.createTextNode(source.slice(last)));
    return parent;
  }

  function splitRow(line) {
    return line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => cell.trim());
  }

  function renderMarkdownInto(container, text) {
    container.textContent = "";
    container.classList.add("markdown-body");
    const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
    let index = 0;
    let paragraph = [];

    const flush = () => {
      if (!paragraph.length) return;
      const node = el("p");
      inlineInto(node, paragraph.join(" "));
      container.appendChild(node);
      paragraph = [];
    };

    while (index < lines.length) {
      const line = lines[index];

      if (/^\s*```/.test(line)) {
        flush();
        const lang = line.replace(/^\s*```/, "").trim();
        const code = [];
        index += 1;
        while (index < lines.length && !/^\s*```/.test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        index += 1;
        const block = el("div", "code-block");
        const head = el("div", "code-head");
        head.appendChild(el("span", null, lang || "code"));
        const copy = el("button", "code-copy", "copy");
        copy.type = "button";
        const body = el("pre");
        body.appendChild(el("code", null, code.join("\n")));
        copy.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(code.join("\n"));
            copy.textContent = "copied";
            setTimeout(() => (copy.textContent = "copy"), 1200);
          } catch (_) {
            /* clipboard blocked — the code stays selectable */
          }
        });
        head.appendChild(copy);
        block.appendChild(head);
        block.appendChild(body);
        container.appendChild(block);
        continue;
      }

      if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|?[\s:-]*-[\s|:-]*$/.test(lines[index + 1] || "")) {
        flush();
        const head = splitRow(line);
        index += 2;
        const rows = [];
        while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) {
          rows.push(splitRow(lines[index]));
          index += 1;
        }
        const table = el("table", "markdown-table");
        const thead = el("thead");
        const headRow = el("tr");
        head.forEach((cell) => {
          const th = el("th");
          inlineInto(th, cell);
          headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);
        const tbody = el("tbody");
        rows.forEach((cells) => {
          const tr = el("tr");
          cells.forEach((cell) => {
            const td = el("td");
            inlineInto(td, cell);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        const scroll = el("div", "table-wrap");
        scroll.appendChild(table);
        container.appendChild(scroll);
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flush();
        const node = el("h" + Math.min(heading[1].length, 4));
        inlineInto(node, heading[2]);
        container.appendChild(node);
        index += 1;
        continue;
      }

      if (/^\s*(---|___|\*\*\*)\s*$/.test(line)) {
        flush();
        container.appendChild(el("hr"));
        index += 1;
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        flush();
        const quoted = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        const callout = el("div", "callout");
        const copy = el("p");
        inlineInto(copy, quoted.join(" "));
        callout.appendChild(copy);
        container.appendChild(callout);
        continue;
      }

      const bullet = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
      if (bullet) {
        flush();
        const ordered = /\d/.test(bullet[1]);
        const list = el(ordered ? "ol" : "ul");
        while (index < lines.length) {
          const item = lines[index].match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
          if (!item || /\d/.test(item[1]) !== ordered) break;
          const li = el("li");
          inlineInto(li, item[2]);
          list.appendChild(li);
          index += 1;
        }
        container.appendChild(list);
        continue;
      }

      if (!line.trim()) {
        flush();
        index += 1;
        continue;
      }

      paragraph.push(line.trim());
      index += 1;
    }
    flush();
    return container;
  }

  /* ───────────────────── the transcript ─────────────────────── */
  function messagesBox() {
    return $("messages") || $("chat");
  }

  function hideEmptyState() {
    const empty = $("empty-state");
    if (empty) empty.remove();
  }

  function stamp() {
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    return hh + ":" + mm;
  }

  function messageShell(role) {
    const message = el("div", "message " + role);
    const avatar = el("div", "message-avatar");
    avatar.appendChild(
      role === "user"
        ? icon(["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M4.5 20a7.5 7.5 0 0 1 15 0"])
        : icon(["M12 3.2 13.7 9l5.8 1.7-5.8 1.7L12 18.2 10.3 12.4 4.5 10.7 10.3 9 12 3.2Z"])
    );
    const body = el("div", "message-body");
    const meta = el("div", "message-meta");
    meta.appendChild(el("strong", null, role === "user" ? "You" : "Orchestration manager"));
    meta.appendChild(el("span", null, "·"));
    meta.appendChild(el("span", null, stamp()));
    body.appendChild(meta);
    message.appendChild(avatar);
    message.appendChild(body);
    messagesBox().appendChild(message);
    return { message, body, meta };
  }

  function appendUser(text, fileNames) {
    hideEmptyState();
    const { message, body } = messageShell("user");
    const bubble = el("div", "message-bubble");
    bubble.appendChild(el("div", "msg-text", text));
    body.appendChild(bubble);
    if (fileNames && fileNames.length) {
      const files = el("div", "msg-files");
      files.appendChild(icon(["m8 12.5 7.1-7.1a3 3 0 0 1 4.2 4.2l-8.5 8.5a5 5 0 0 1-7.1-7.1l8.1-8.1"]));
      files.appendChild(document.createTextNode(" " + fileNames.join(" · ")));
      body.appendChild(files);
    }
    scrollChat();
    return message;
  }

  function appendAssistantSkeleton() {
    hideEmptyState();
    const { message, body } = messageShell("assistant");
    const bubble = el("div", "message-bubble");
    const text = el("div", "msg-text");
    text.appendChild(el("span", "msg-skeleton", "thinking · planning agents…"));
    bubble.appendChild(text);
    body.appendChild(bubble);
    scrollChat();
    return { message, body, bubble, text };
  }

  function scrollChat() {
    const chat = $("chat");
    if (chat) chat.scrollTop = chat.scrollHeight;
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

  function metaLine(container, bits) {
    const clean = bits.filter(Boolean);
    if (!clean.length) return;
    const meta = el("div", "message-meta answer-meta");
    clean.forEach((bit, index) => {
      if (index) meta.appendChild(el("span", null, "·"));
      meta.appendChild(el("span", "msg-model", bit));
    });
    container.appendChild(meta);
  }

  function copyButton(label, value) {
    const button = el("button");
    button.type = "button";
    button.appendChild(icon(["M9 9h10v10H9z", "M5 15V5h10"]));
    button.appendChild(el("span", null, label));
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(value);
        toast("Answer copied");
      } catch (_) {
        toast("Clipboard blocked — select the text instead", "warn");
      }
    });
    return button;
  }

  // One renderer for a live answer and for one read back out of memory:
  // same bubble, same "How this answer was built" panel, same meta line.
  function renderAnswerInto(body, data, mode, existingText) {
    let text = existingText;
    if (!text) {
      const bubble = el("div", "message-bubble");
      text = el("div", "msg-text");
      bubble.appendChild(text);
      body.appendChild(bubble);
    }
    const raw = answerText(data, mode) || "(empty answer)";
    renderMarkdownInto(text, raw);

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
      bits.push((orch.plan.agents || []).length + " agent(s) in " + (orch.waves || []).length + " wave(s)");
      const gate = orch.evaluation || {};
      if (gate.ran) {
        bits.push(
          "evaluator " + gate.quality + "/5" + (gate.revised ? " · " + gate.revised + " revised" : "")
        );
      }
      if (orch.model_calls) bits.push(orch.model_calls + " model call(s)");
      bits.push(orch.parallel_ms + " ms agents");
      bits.push(orch.latency_ms + " ms total");
      metaLine(body, bits);
    } else {
      renderMeta(body, data);
    }

    const actions = el("div", "message-actions");
    actions.appendChild(copyButton("Copy answer", raw));
    const inspect = el("button");
    inspect.type = "button";
    inspect.appendChild(icon(["M4 5h16v14H4z", "M15 5v14"]));
    inspect.appendChild(el("span", null, "Inspect run"));
    inspect.addEventListener("click", () => {
      showInspector(data, "Run payload — " + mode + " mode");
      openInspector();
    });
    actions.appendChild(inspect);
    body.appendChild(actions);
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

    const { message, body } = messageShell("assistant");
    const assistant = turn.assistant || {};
    if (assistant.ok === false) {
      const bubble = el("div", "message-bubble");
      const text = el("div", "msg-text msg-error");
      text.textContent = errorText(errorStub(turn));
      bubble.appendChild(text);
      body.appendChild(bubble);
    } else {
      const data = assistant.response || {
        answer: assistant.text || assistant.answer || "",
        sources: [],
      };
      renderAnswerInto(body, data, turn.mode || "auto");
    }
    return message;
  }

  /* ─────────────────── welcome / empty state ────────────────── */
  function welcomeNode(text) {
    const wrap = el("div", "welcome");
    wrap.id = "empty-state";
    const orb = el("div", "welcome-orb");
    orb.appendChild(icon(["M12 2.8 14.1 9.9 21.2 12 14.1 14.1 12 21.2 9.9 14.1 2.8 12 9.9 9.9 12 2.8Z"]));
    wrap.appendChild(orb);
    wrap.appendChild(el("div", "eyebrow", text ? "New conversation" : "Multi-agent test environment"));
    wrap.appendChild(el("h1", null, text ? "A fresh conversation." : "Explore your orchestration flow."));
    wrap.appendChild(
      el(
        "p",
        "welcome-lead",
        text ||
          "Run a prompt through the manager, watch the specialists work in parallel waves, see " +
            "their reports evaluated and tightened, and follow exactly how the answer was " +
            "assembled. It streams while it runs — the planner, every agent, the evaluator and " +
            "the manager, each in its own group above the answer — and every stage stays " +
            "auditable afterwards: the plan, the prompts, the tool calls, the verdicts, the record."
      )
    );

    if (!text) {
      const architecture = el("div", "architecture");
      [
        ["01", "Manager plans", "The manager reads the request, picks the specialists and writes each one's brief."],
        ["02", "Agents execute", "Document, search, data and general agents work in parallel waves with tools."],
        ["03", "Evaluator checks", "Every report is scored, gaps are named and the weak ones are rewritten before anyone reads them."],
        ["04", "Manager synthesizes", "It reviews the tightened reports, may ask for another wave, then writes the answer."],
      ].forEach(([number, title, copy]) => {
        const card = el("article", "architecture-step");
        const top = el("div", "step-top");
        top.appendChild(el("span", "step-number", number));
        top.appendChild(el("span", "step-title", title));
        card.appendChild(top);
        card.appendChild(el("p", "step-copy", copy));
        architecture.appendChild(card);
      });
      wrap.appendChild(architecture);

      const heading = el("div", "suggestion-heading");
      heading.appendChild(el("span", null, "Try a sample task"));
      heading.appendChild(el("span", null, "Choose a prompt to get started"));
      wrap.appendChild(heading);

      const suggestions = el("div", "suggestions");
      [
        ["Research a topic", "Use web search and synthesize multiple sources.", "What changed in the latest Python release? Cite your sources."],
        ["Analyze a document", "Attach a CSV, DOCX or PDF and inspect the findings.", "Average revenue per month in the attached CSV, and which month grew fastest?"],
        ["Compare providers", "Evaluate free tiers, rate limits and capabilities.", "Search for the newest free LLM APIs and compare their rate limits."],
      ].forEach(([title, copy, prompt]) => {
        const card = el("button", "suggestion");
        card.type = "button";
        card.dataset.prompt = prompt;
        const mark = el("span", "suggestion-icon");
        mark.appendChild(icon(["M12 4v16M4 12h16"]));
        card.appendChild(mark);
        const copyBox = el("span");
        copyBox.appendChild(el("strong", null, title));
        copyBox.appendChild(el("small", null, copy));
        card.appendChild(copyBox);
        card.addEventListener("click", () => {
          $("prompt").value = prompt;
          $("prompt").focus();
        });
        suggestions.appendChild(card);
      });
      wrap.appendChild(suggestions);
    }

    const note = el("p", "demo-disclaimer");
    note.appendChild(document.createTextNode("Every run is written to "));
    note.appendChild(el("code", null, "logs/orchestrator/"));
    note.appendChild(document.createTextNode(" and every conversation to "));
    note.appendChild(el("code", null, "memory/"));
    note.appendChild(document.createTextNode(". Reopen this chat from the sidebar and the model gets the earlier turns back."));
    wrap.appendChild(note);
    return wrap;
  }

  function renderEmptyState(text) {
    const chat = $("chat");
    if (!chat) return;
    chat.textContent = "";
    const messages = el("div", "messages");
    messages.id = "messages";
    chat.appendChild(welcomeNode(text));
    chat.appendChild(messages);
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

  function renderSources(container, urls) {
    if (!urls || !urls.length) return;
    const list = el("div", "sources");
    urls.forEach((url, index) => {
      const link = el("a", "source-link");
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.title = url;
      link.appendChild(el("span", "source-index", String(index + 1)));
      link.appendChild(el("span", null, url.replace(/^https?:\/\//, "").slice(0, 46)));
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
    metaLine(container, bits);
  }

  /* ───────────── single-agent trace (agent / answer / search) ─── */
  function renderTimeline(container, run) {
    const trace = run.trace || [];
    if (!trace.length) return;
    const panel = el("div", "orch-panel");
    const head = el("div", "orch-toolbar");
    head.appendChild(el("span", "orch-title", "Execution trace"));
    head.appendChild(el("span", "orch-runid", (trace.length || 0) + " step(s)"));
    panel.appendChild(head);

    const rows = el("div", "trace-rows");
    trace.forEach((step, index) => {
      const isTool = step.type === "tool";
      const row = el("div", "trace-row" + (isTool && step.ok === false ? " open" : ""));
      const summary = el("div", "trace-row-head");
      summary.appendChild(el("span", "stage-icon", isTool ? "⚙" : "T"));
      summary.appendChild(el("span", "stage", step.type));
      summary.appendChild(el("span", "actor", isTool ? step.name || "tool" : step.model || "model"));
      summary.appendChild(
        el(
          "span",
          "step-title",
          isTool
            ? JSON.stringify(step.arguments || {}).slice(0, 60)
            : step.tool_calls && step.tool_calls.length
              ? "→ " + step.tool_calls.map((call) => call.name).join(", ")
              : "→ answer"
        )
      );
      summary.appendChild(el("span", "ms", (step.latency_ms || 0) + " ms"));
      summary.appendChild(el("span", "chev", "#" + (index + 1)));
      row.appendChild(summary);

      const body = el("div", "trace-body trace-detail");
      if (step.type === "model" && step.content) {
        body.appendChild(preBlock(step.content, "msg"));
      }
      if (step.tool_calls && step.tool_calls.length) {
        body.appendChild(renderJson(step.tool_calls, "tool calls"));
      }
      if (isTool) {
        body.appendChild(renderJson(step.preview, "result preview"));
      }
      if (body.childElementCount) {
        row.appendChild(body);
        summary.addEventListener("click", () => row.classList.toggle("open"));
      }
      rows.appendChild(row);
    });
    panel.appendChild(rows);
    container.appendChild(panel);
  }

  /* ────────── orchestration: how this answer was built ────────── */
  const STAGE_ICON = {
    run: "▶",
    manager: "M",
    planner: "P",
    agent: "A",
    tool: "⚙",
    evaluate: "⚖",
    synthesis: "✎",
    merge: "⇄",
    done: "✓",
  };

  function actorTone(actor) {
    return ["manager", "planner", "evaluator"].includes(actor) ? "core" : "agent-" + actor;
  }

  function preBlock(text, cls) {
    const pre = el("pre", "trace-pre" + (cls ? " " + cls : ""));
    pre.textContent = text;
    return pre;
  }

  function renderMessages(messages) {
    const box = el("div", "trace-messages");
    (messages || []).forEach((message) => {
      const row = el("div", "trace-message");
      row.appendChild(el("span", "role", message.role || "?"));
      row.appendChild(preBlock(message.content || ""));
      box.appendChild(row);
    });
    return box;
  }

  function renderJson(value, label) {
    const box = el("details", "trace-json-block");
    box.appendChild(el("summary", null, label || "payload"));
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
    const body = el("div", "trace-detail");
    let rendered = false;

    if (detail.messages) {
      body.appendChild(renderMessages(detail.messages));
      rendered = true;
    }
    if (detail.content) {
      body.appendChild(el("div", "trace-sub", "output"));
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
      ["round", detail.round],
      ["quality", detail.quality],
      ["method", detail.method],
      ["revised", detail.revised],
      ["review", detail.review],
      ["synthesize", detail.synthesize],
      ["has model", detail.has_model],
    ];
    if (Array.isArray(detail.items) && detail.items.some((item) => item && item.verdict)) {
      (detail.items || []).forEach((item) => {
        if (!item || !item.verdict) return;
        const card = el("div", "trace-card eval-card");
        const row = el("div", "trace-card-row");
        row.appendChild(el("strong", null, item.label || item.agent));
        row.appendChild(
          el(
            "span",
            "trace-duration",
            item.verdict + " · " + item.score + "/5" + (item.revised ? " · rewritten" : "")
          )
        );
        card.appendChild(row);
        if (item.issues && item.issues.length) {
          card.appendChild(el("p", null, item.issues.join(" · ")));
        }
        body.appendChild(card);
      });
      rendered = true;
    }
    if (Array.isArray(detail.gaps) && detail.gaps.length) {
      body.appendChild(el("div", "trace-sub", "gaps the team did not close"));
      body.appendChild(preBlock("- " + detail.gaps.join("\n- ")));
      rendered = true;
    }
    if (detail.before && detail.after) {
      body.appendChild(el("div", "trace-sub", "before · the agent's own report"));
      body.appendChild(preBlock(detail.before));
      body.appendChild(el("div", "trace-sub", "after · what the manager reads"));
      body.appendChild(preBlock(detail.after));
      rendered = true;
    }
    if (detail.summary && !detail.steps) {
      body.appendChild(el("div", "trace-sub", "summary"));
      body.appendChild(preBlock(detail.summary));
      rendered = true;
    }

    const chips = renderChips(pairs);
    if (chips) {
      body.appendChild(chips);
      rendered = true;
    }
    if (detail.summary && detail.steps) {
      body.appendChild(el("div", "trace-sub", "plan"));
      body.appendChild(preBlock(detail.summary));
    }
    if (detail.requests) {
      body.appendChild(renderJson(detail.requests, "follow-up requested"));
      rendered = true;
    }
    if (detail.steps && Array.isArray(detail.steps)) {
      (detail.steps || []).forEach((step) => {
        const card = el("div", "trace-card");
        const row = el("div", "trace-card-row");
        row.appendChild(el("strong", null, step.agent));
        row.appendChild(el("span", "trace-duration", "wave " + (step.wave || 1)));
        card.appendChild(row);
        if (step.task) card.appendChild(el("p", null, step.task));
        if (step.depends_on && step.depends_on.length) {
          card.appendChild(el("p", null, "waits for " + step.depends_on.join(", ")));
        }
        if (step.reason) card.appendChild(el("p", null, "why: " + step.reason));
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
      body.appendChild(el("div", "trace-sub", "computed by code"));
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
      body.appendChild(el("div", "trace-sub", "sources: " + detail.sources.join(" · ")));
      rendered = true;
    }
    if (!rendered) {
      body.appendChild(renderJson(detail, "event payload"));
    }
    return body;
  }

  function renderTraceEvent(event, expandAll) {
    const detail = event.detail || {};
    const row = el("div", "trace-row" + (expandAll ? " open" : ""));
    const head = el("div", "trace-row-head");
    head.appendChild(el("span", "stage-icon", STAGE_ICON[event.stage] || "•"));
    head.appendChild(el("span", "stage", event.stage));
    head.appendChild(el("span", "actor " + actorTone(event.actor), event.actor));
    head.appendChild(el("span", "step-title", event.title || ""));
    if (detail.latency_ms !== undefined) {
      head.appendChild(el("span", "ms", detail.latency_ms + " ms"));
    } else {
      head.appendChild(el("span", "ms", event.offset_ms + " ms"));
    }
    head.appendChild(el("span", "chev", "#" + event.seq));
    const body = renderEventBody(event);
    row.appendChild(head);
    if (body.childElementCount) {
      row.appendChild(body);
      head.addEventListener("click", () => row.classList.toggle("open"));
    }
    return row;
  }

  function evaluationFor(orch, agent) {
    const items = (orch.evaluation && orch.evaluation.items) || [];
    return items.find((item) => item.agent === agent) || null;
  }

  function renderPlanSummary(panel, orch) {
    const plan = orch.plan || {};
    const box = el("div", "plan-summary");
    const top = el("div", "plan-intent");
    top.appendChild(el("span", null, plan.intent || "plan"));
    top.appendChild(el("span", "pill", "selection: " + (plan.strategy || "?")));
    top.appendChild(el("span", "pill", "briefs: " + (plan.detail_source || "rules")));
    if (orch.model_calls) top.appendChild(el("span", "pill", orch.model_calls + " model call(s)"));
    box.appendChild(top);
    if (plan.summary) box.appendChild(el("p", null, plan.summary));

    const chips = el("div", "plan-agents");
    (plan.steps || []).forEach((step) => {
      const chip = el("span", "agent-chip");
      chip.appendChild(el("span", null, step.label || step.agent));
      chip.appendChild(el("span", "wave", "w" + (step.wave || 1)));
      if (step.depends_on && step.depends_on.length) {
        chip.appendChild(el("span", "wave", "← " + step.depends_on.join(",")));
      }
      const verdict = evaluationFor(orch, step.agent);
      if (verdict) {
        const tone = verdict.verdict === "fail" ? " bad" : verdict.verdict === "revise" ? " warn" : "";
        chip.appendChild(
          el("span", "verdict" + tone, verdict.revised ? "revised " + verdict.score + "/5" : verdict.score + "/5")
        );
      }
      chip.title = step.task || "";
      chips.appendChild(chip);
    });
    if (chips.childElementCount) box.appendChild(chips);
    panel.appendChild(box);
  }

  function renderOrchestration(container, data) {
    const orch = data.orchestration;
    if (!orch) return;

    const panel = el("div", "orch-panel");
    const head = el("div", "orch-head");
    head.appendChild(el("span", "orch-title", "How this answer was built"));
    head.appendChild(el("span", "orch-chip", (orch.plan.agents || []).join(" → ") || "no agents"));
    head.appendChild(el("span", "orch-chip", (orch.waves || []).length + " wave(s)"));
    const evaluation = orch.evaluation || {};
    if (evaluation.requested && evaluation.ran) {
      const tone = evaluation.quality >= 4 ? " good" : evaluation.failed ? " bad" : " warn";
      head.appendChild(
        el(
          "span",
          "orch-chip" + tone,
          "evaluator " + evaluation.quality + "/5" + (evaluation.revised ? " · " + evaluation.revised + " revised" : "")
        )
      );
    }
    if (orch.synthesized) head.appendChild(el("span", "orch-chip good", "manager synthesis"));
    head.appendChild(
      el("span", "orch-time", orch.parallel_ms + " ms agents · " + orch.latency_ms + " ms total")
    );
    panel.appendChild(head);

    renderPlanSummary(panel, orch);

    const tools = el("div", "orch-toolbar");
    const expand = el("button", "ghost-btn", "Expand all steps");
    expand.type = "button";
    const collapse = el("button", "ghost-btn", "Collapse");
    collapse.type = "button";
    const raw = el("button", "ghost-btn", "Inspect");
    raw.type = "button";
    tools.appendChild(expand);
    tools.appendChild(collapse);
    tools.appendChild(raw);
    if (orch.run_id) tools.appendChild(el("span", "orch-runid", orch.run_id));
    panel.appendChild(tools);

    const timeline = el("div", "trace-rows");
    (orch.trace || []).forEach((event) => timeline.appendChild(renderTraceEvent(event, false)));
    panel.appendChild(timeline);

    expand.addEventListener("click", () =>
      panel.querySelectorAll(".trace-row").forEach((node) => node.classList.add("open"))
    );
    collapse.addEventListener("click", () =>
      panel.querySelectorAll(".trace-row").forEach((node) => node.classList.remove("open"))
    );
    raw.addEventListener("click", () => {
      showInspector(data, "Raw orchestration payload");
      openInspector();
    });

    if (evaluation.requested && evaluation.ran) {
      const note = el("div", "orch-note eval-note");
      note.appendChild(el("strong", null, "Evaluator · "));
      const judged = (evaluation.items || []).length;
      note.appendChild(
        document.createTextNode(
          (evaluation.summary || "reviewed " + judged + " report(s)") +
            (evaluation.method === "rules" ? " (rule check — no model)" : "")
        )
      );
      if (evaluation.gaps && evaluation.gaps.length) {
        const gaps = el("div", "orch-note-sub", "gaps: " + evaluation.gaps.join(" · "));
        note.appendChild(gaps);
      }
      const revised = (evaluation.items || []).filter((item) => item.revised);
      if (revised.length) {
        note.appendChild(
          el(
            "div",
            "orch-note-sub",
            "rewritten: " + revised.map((item) => item.label || item.agent).join(", ")
          )
        );
      }
      panel.appendChild(note);
    } else if (evaluation.requested === false && evaluation.reason) {
      panel.appendChild(el("div", "orch-note", "Evaluator · " + evaluation.reason));
    }
    if (orch.review && orch.review.asked) {
      const note = el("div", "orch-note");
      note.appendChild(el("strong", null, "Manager review · "));
      note.appendChild(
        document.createTextNode(
          (orch.review.rounds > 1 ? "round " + orch.review.rounds + " — " : "") +
            (orch.review.note || orch.review.reason || "done") +
            (orch.review.follow_up && orch.review.follow_up.length
              ? " — asked for " + orch.review.follow_up.map((f) => f.agent).join(", ")
              : " — no extra work needed")
        )
      );
      panel.appendChild(note);
    }
    if (orch.files && orch.files.length) {
      panel.appendChild(el("div", "orch-note", "files: " + orch.files.map((f) => f.name).join(", ")));
    }
    (orch.notes || []).forEach((note) => panel.appendChild(el("div", "orch-note", note)));
    if (orch.log_path) {
      panel.appendChild(el("div", "orch-log", "full unclipped trace: " + orch.log_path));
    }
    container.appendChild(panel);
  }

  function finishAssistant(skeleton, result, prompt, mode) {
    const { body, text } = skeleton;
    text.textContent = "";

    if (!result.ok) {
      text.classList.add("msg-error");
      text.textContent = errorText(result);
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
      steps: data.steps || (data.orchestration ? (data.orchestration.plan.agents || []).length : 0),
      agents: data.orchestration ? (data.orchestration.plan.agents || []).join(",") : "",
      payload: data,
    };
    state.runs.unshift(run);
    state.runs = state.runs.slice(0, 25);
    saveRuns();
    renderHistory();
    showInspector(data, "Run " + run.id + " — " + mode + " mode");
    return run;
  }

  /* ─────────────────────── agents roster ───────────────────── */
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
    summary.className = "panel-meta";
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

  /* ───────────────── orchestration run log (log base) ───────── */
  async function loadRunLog() {
    const list = $("runs-list");
    if (!list) return;
    const result = await api("/runs?limit=8");
    if (!result.ok) {
      state.runsSummary = "Run log unavailable (GET /runs).";
      updateRunsSummary();
      return;
    }
    const runs = result.data.runs || [];
    state.runsSummary = runs.length
      ? runs.length + " saved · " + (result.data.dir || "logs/orchestrator")
      : "No saved runs yet — send an Auto prompt.";
    updateRunsSummary();
    list.textContent = "";
    runs.forEach((run) => {
      const row = el("button", "run-row");
      row.type = "button";
      row.appendChild(el("span", "run-intent", run.intent || "run"));
      row.appendChild(el("span", "run-agents", (run.agents || []).join(",")));
      row.appendChild(el("span", "run-ms", (run.latency_ms || 0) + " ms"));
      row.title = run.message + "\n" + run.run_id;
      row.addEventListener("click", () => openRun(run.run_id));
      list.appendChild(row);
    });
  }

  function updateRunsSummary() {
    const summary = $("runs-summary");
    if (!summary) return;
    summary.textContent = state.runsTab === "local" ? state.historySummary : state.runsSummary;
  }

  function setRunsTab(tab) {
    state.runsTab = tab === "local" ? "local" : "saved";
    document.querySelectorAll("#runs-tabs .seg-btn").forEach((button) => {
      button.classList.toggle("active", button.dataset.runs === state.runsTab);
    });
    const saved = $("runs-list");
    const local = $("trace-list");
    if (saved) saved.hidden = state.runsTab !== "saved";
    if (local) local.hidden = state.runsTab !== "local";
    updateRunsSummary();
  }

  async function openRun(runId) {
    const result = await api("/runs/" + encodeURIComponent(runId));
    if (!result.ok) {
      showInspector({ error: "run not found", run_id: runId }, "Run " + runId);
      openInspector();
      return;
    }
    showInspector(result.data, "Logged run " + runId + " (full, unclipped)");
    openInspector();
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

  function providerRow(selected, name, tagText, tagTone, desc, onPick) {
    const row = el("button", "provider-row" + (selected ? " selected" : ""));
    row.type = "button";
    row.dataset.provider = name;
    row.appendChild(el("span", "provider-radio"));
    const content = el("span", "provider-content");
    const line = el("span", "provider-name-line");
    line.appendChild(el("span", "provider-name", name));
    line.appendChild(el("span", "tag" + (tagTone ? " " + tagTone : ""), tagText));
    content.appendChild(line);
    if (desc) content.appendChild(el("span", "provider-desc", desc));
    row.appendChild(content);
    row.addEventListener("click", onPick);
    return row;
  }

  function renderSearchList(providers) {
    const list = $("search-list");
    if (!list) return;
    list.textContent = "";

    list.appendChild(
      providerRow(
        !state.pinnedSearch,
        "Chain (failover)",
        "all",
        "",
        "Try each backend in order, first non-empty wins.",
        () => {
          state.pinnedSearch = null;
          renderSearchList(state.providers);
          updateComposerLabel();
        }
      )
    );

    (providers || []).forEach((preset) => {
      const tagText = preset.needs_server ? "server" : "no server";
      const tone = preset.configured ? "" : "warn";
      const desc = preset.needs_key ? "free key · " + preset.free_tier : preset.free_tier;
      const row = providerRow(
        state.pinnedSearch === preset.name,
        preset.label,
        tagText,
        tone,
        desc,
        () => {
          state.pinnedSearch = state.pinnedSearch === preset.name ? null : preset.name;
          renderSearchList(state.providers);
          updateComposerLabel();
          if (state.mode !== "search") setMode("search");
        }
      );
      row.title = preset.notes || "";
      const nameLine = row.querySelector(".provider-name-line");
      if (nameLine && !preset.configured) nameLine.appendChild(el("span", "tag error", "no key"));
      list.appendChild(row);
    });
  }

  async function loadProviders() {
    const { ok, data } = await api("/providers");
    if (!ok) return;
    state.providers = data.search ? data.search.providers : [];
    renderSearchList(state.providers);
    state.fullProviders = data;
  }

  /* ────────────────── inspector (trace/context/json) ────────── */
  function summaryCard(title, ok, metrics) {
    const card = el("div", "run-summary");
    const top = el("div", "run-summary-top");
    top.appendChild(el("div", "run-summary-name", title));
    const status = el("span", "success-label" + (ok ? "" : " bad"));
    status.appendChild(el("span", "dot"));
    status.appendChild(document.createTextNode(ok ? "complete" : "failed"));
    top.appendChild(status);
    card.appendChild(top);
    const grid = el("div", "summary-grid");
    metrics.forEach(([value, label]) => {
      const metric = el("div", "summary-metric");
      metric.appendChild(el("strong", null, value));
      metric.appendChild(el("span", null, label));
      grid.appendChild(metric);
    });
    card.appendChild(grid);
    return card;
  }

  function timelineItem(name, detail, duration, tone) {
    const item = el("div", "trace-item");
    item.appendChild(el("span", "trace-marker " + (tone || ""), tone === "bad" ? "!" : tone === "pending" ? "·" : "✓"));
    const copy = el("div", "trace-copy");
    const head = el("div", "trace-name");
    head.appendChild(el("span", null, name));
    if (duration) head.appendChild(el("span", "trace-duration", duration));
    copy.appendChild(head);
    if (detail) copy.appendChild(el("div", "trace-detail", detail));
    item.appendChild(copy);
    return item;
  }

  function section(title, extra) {
    const box = el("div", "inspect-section");
    const head = el("div", "inspect-section-title");
    head.appendChild(el("span", null, title));
    if (extra) head.appendChild(el("span", null, extra));
    box.appendChild(head);
    return box;
  }

  function traceView(payload) {
    const box = el("div");
    if (!payload || typeof payload === "string") {
      box.appendChild(emptyInspector(String(payload || "")));
      return box;
    }

    if (payload.turns) {
      // a stored session record
      const head = section("Session");
      head.appendChild(
        summaryCard(payload.title || payload.session_id, true, [
          [String((payload.turns || []).length), "turns"],
          [String(payload.files || 0), "files"],
          [formatWhen(payload.updated_at) || "—", "updated"],
        ])
      );
      box.appendChild(head);
      const list = section("Turns", (payload.turns || []).length + " total");
      const timeline = el("div", "timeline");
      (payload.turns || []).forEach((turn, index) => {
        const question = (turn.user || {}).text || "(no question)";
        const answer = (turn.assistant || {}).answer || (turn.assistant || {}).text || "";
        timeline.appendChild(
          timelineItem(
            "Turn " + (index + 1) + " · " + (turn.mode || "auto"),
            question.slice(0, 160) + (answer ? "\n→ " + String(answer).slice(0, 160) : ""),
            "",
            turn.assistant && turn.assistant.ok === false ? "bad" : ""
          )
        );
      });
      list.appendChild(timeline);
      box.appendChild(list);
      return box;
    }

    const orch = payload.orchestration;
    if (orch) {
      const head = section("Run summary");
      const evaluation = orch.evaluation || {};
      const metrics = [
        [String((orch.plan.agents || []).length), "agents"],
        [String((orch.waves || []).length), "waves"],
        [(orch.latency_ms || 0) + " ms", "total"],
      ];
      if (evaluation.ran) {
        metrics.splice(2, 0, [
          String(evaluation.quality) + (evaluation.revised ? " (" + evaluation.revised + "✎)" : ""),
          "evaluator /5",
        ]);
      }
      head.appendChild(
        summaryCard(
          (orch.plan && (orch.plan.intent || orch.plan.summary)) || payload.answer ? "Orchestrated run" : "Run",
          true,
          metrics
        )
      );
      box.appendChild(head);

      if (evaluation.requested) {
        const gate = section(
          "Evaluation",
          evaluation.ran ? evaluation.method + " · " + evaluation.quality + "/5" : "skipped"
        );
        if (evaluation.summary) {
          const card = el("div", "trace-card");
          card.appendChild(el("p", null, evaluation.summary));
          gate.appendChild(card);
        }
        (evaluation.items || []).forEach((item) => {
          const card = el("div", "trace-card eval-card");
          const row = el("div", "trace-card-row");
          row.appendChild(el("strong", null, item.label || item.agent));
          row.appendChild(
            el(
              "span",
              "trace-duration",
              item.verdict + " · " + item.score + "/5" + (item.revised ? " · rewritten" : "")
            )
          );
          card.appendChild(row);
          if (item.issues && item.issues.length) card.appendChild(el("p", null, item.issues.join(" · ")));
          gate.appendChild(card);
        });
        if (evaluation.gaps && evaluation.gaps.length) {
          gate.appendChild(el("p", "trace-card", "gaps: " + evaluation.gaps.join(" · ")));
        }
        if (!evaluation.ran && evaluation.reason) {
          gate.appendChild(el("p", "trace-card", evaluation.reason));
        }
        box.appendChild(gate);
      }

      const steps = section("Timeline", (orch.trace || []).length + " events");
      const timeline = el("div", "timeline");
      (orch.trace || []).forEach((event) => {
        const detail = event.detail || {};
        timeline.appendChild(
          timelineItem(
            event.title || event.stage,
            [event.actor, event.stage, detail.task || detail.query || detail.note || detail.error || ""]
              .filter(Boolean)
              .join(" · ").slice(0, 220),
            detail.latency_ms !== undefined ? detail.latency_ms + " ms" : event.offset_ms + " ms",
            /error|fail/i.test(event.title || "") ? "bad" : event.stage === "tool" ? "pending" : ""
          )
        );
      });
      steps.appendChild(timeline);
      box.appendChild(steps);

      const tools = (orch.trace || []).filter((event) => event.stage === "tool");
      if (tools.length) {
        const cards = section("Tool activity", tools.length + " call(s)");
        tools.forEach((event) => {
          const card = el("div", "trace-card");
          const row = el("div", "trace-card-row");
          row.appendChild(el("strong", null, event.actor + " · " + (event.title || "tool")));
          row.appendChild(
            el("span", "trace-duration", ((event.detail || {}).latency_ms || 0) + " ms")
          );
          card.appendChild(row);
          const detail = event.detail || {};
          const text = detail.query || detail.error || (detail.output ? JSON.stringify(detail.output) : "");
          if (text) card.appendChild(el("p", null, String(text).slice(0, 220)));
          cards.appendChild(card);
        });
        box.appendChild(cards);
      }
    }

    const events = (payload.trace || []).filter((step) => step.type);
    if (!orch && events.length) {
      const head = section("Execution");
      head.appendChild(
        summaryCard(payload.provider ? payload.provider + "/" + payload.model : "Run", true, [
          [String(events.length), "steps"],
          [(payload.latency_ms || 0) + " ms", "total"],
          [payload.search_provider || "—", "search"],
        ])
      );
      box.appendChild(head);
      const timeline = section("Timeline");
      const list = el("div", "timeline");
      events.forEach((step) =>
        list.appendChild(
          timelineItem(
            step.type === "tool" ? step.name || "tool" : (step.provider || "") + "/" + (step.model || "model"),
            step.type === "tool"
              ? JSON.stringify(step.arguments || {}).slice(0, 200)
              : (step.content || "").slice(0, 200),
            (step.latency_ms || 0) + " ms",
            step.ok === false ? "bad" : step.type === "tool" ? "pending" : ""
          )
        )
      );
      timeline.appendChild(list);
      box.appendChild(timeline);
    }

    if (payload.session && payload.session.session_id) {
      const s = section("Session");
      s.appendChild(
        el("div", "context-item", "Continues " + payload.session.session_id + " · " + (payload.session.turns || 0) + " turn(s)")
      );
      box.appendChild(s);
    }

    if (!box.childElementCount) {
      box.appendChild(emptyInspector(payload.hint || "This payload has no readable trace."));
      if (Array.isArray(payload.modes) && payload.modes.length) {
        const modes = el("div", "trace-chips");
        payload.modes.forEach((mode) => modes.appendChild(el("span", "trace-chip", mode)));
        box.appendChild(modes);
      }
      if (Array.isArray(payload.endpoints) && payload.endpoints.length) {
        const endpoints = el("div", "trace-chips");
        payload.endpoints.forEach((endpoint) => endpoints.appendChild(el("span", "trace-chip", endpoint)));
        box.appendChild(endpoints);
      }
    }
    return box;
  }

  function emptyInspector(text) {
    const box = el("div", "inspector-empty");
    const mark = el("div", "empty-icon");
    mark.appendChild(icon(["M5.5 4.5h9l4 4v11h-13z", "M14.5 4.5v4h4", "M9 13h6M9 16h4"]));
    box.appendChild(mark);
    box.appendChild(el("h3", null, "Nothing to inspect yet"));
    box.appendChild(el("p", null, text || "Send a prompt, pick a run, or run diagnostics."));
    return box;
  }

  function contextView(payload) {
    const box = el("div");
    if (!payload || typeof payload === "string") {
      box.appendChild(emptyInspector(String(payload || "")));
      return box;
    }
    const rows = [];
    const mode = state.mode;
    rows.push(["Mode", mode === "auto" ? "Auto — manager + parallel agents" : mode]);
    if (payload.provider || payload.model) {
      rows.push(["Model", (payload.provider || "?") + "/" + (payload.model || "?")]);
    }
    if (payload.search_provider) rows.push(["Search", payload.search_provider]);
    if (payload.latency_ms) rows.push(["Latency", payload.latency_ms + " ms"]);
    if (payload.orchestration) {
      const orch = payload.orchestration;
      rows.push(["Agents", (orch.plan.agents || []).join(" → ") || "none"]);
      rows.push(["Waves", String((orch.waves || []).length)]);
      rows.push(["Planner", (orch.plan.strategy || "?") + " · briefs by " + (orch.plan.detail_source || "rules")]);
      if (orch.model_calls) rows.push(["Model calls", String(orch.model_calls)]);
      if (orch.log_path) rows.push(["Log", orch.log_path]);
      if (orch.run_id) rows.push(["Run id", orch.run_id]);
      if (orch.files && orch.files.length) {
        rows.push(["Files", orch.files.map((f) => f.name).join(", ")]);
      }
    }
    if (payload.session && payload.session.session_id) {
      rows.push(["Session", payload.session.session_id + " · " + (payload.session.turns || 0) + " turn(s)"]);
    }
    if (payload.turns) rows.push(["Turns", String((payload.turns || []).length)]);
    if (payload.sources && payload.sources.length) {
      rows.push(["Sources", payload.sources.length + " link(s)"]);
    }
    rows.push(["History sent", historyPayload().length + " message(s)"]);

    const list = el("div", "context-list");
    rows.forEach(([label, value]) => {
      const item = el("div", "context-item");
      item.appendChild(el("strong", null, label));
      item.appendChild(el("span", null, value));
      list.appendChild(item);
    });
    const head = section("This run's context");
    head.appendChild(list);
    box.appendChild(head);
    return box;
  }

  function renderInspectorTab(tab) {
    state.inspectTab = ["trace", "context", "json"].includes(tab) ? tab : "trace";
    document.querySelectorAll(".inspector-tab").forEach((button) => {
      const active = button.dataset.tab === state.inspectTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const views = {
      trace: $("inspector-trace"),
      context: $("inspector-context"),
      json: $("trace-json"),
    };
    Object.keys(views).forEach((key) => {
      const node = views[key];
      if (!node) return;
      node.classList.toggle("panel-hidden", key !== state.inspectTab);
      node.hidden = key !== state.inspectTab;
    });
    const diag = $("diag-summary");
    if (diag && diag.dataset.report === "1") diag.hidden = state.inspectTab !== "trace";
  }

  function renderInspector() {
    const payload = state.inspect;
    $("trace-json").textContent =
      typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
    if (state.inspectNote) $("inspector-note").textContent = state.inspectNote;

    const trace = $("inspector-trace");
    trace.textContent = "";
    const built = traceView(payload);
    Array.from(built.childNodes).forEach((node) => trace.appendChild(node));

    const context = $("inspector-context");
    context.textContent = "";
    const ctx = contextView(payload);
    Array.from(ctx.childNodes).forEach((node) => context.appendChild(node));

    // the diagnostics rows belong to the diagnostics report only
    const diag = $("diag-summary");
    if (diag) diag.hidden = true;
    renderInspectorTab(state.inspectTab);
  }

  function showInspector(payload, note) {
    state.inspect = payload;
    if (note) state.inspectNote = note;
    renderInspector();
    document.body.classList.add("inspector-open");
  }

  function openInspector() {
    const workspace = $("workspace");
    const inspector = $("inspector");
    if (workspace) workspace.classList.remove("inspector-collapsed");
    if (inspector) inspector.classList.remove("closed");
    document.body.classList.add("inspector-open");
    showInspectorTab("trace");
  }

  function showInspectorTab(tab) {
    renderInspectorTab(tab === "auto" ? "trace" : tab);
  }

  function closeInspector() {
    const workspace = $("workspace");
    const inspector = $("inspector");
    if (workspace) workspace.classList.add("inspector-collapsed");
    if (inspector) inspector.classList.add("closed");
    document.body.classList.remove("inspector-open");
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
    setBadge("badge-diag", "Diag  running…", "warn");
    setHealth("state", "running", "");
    // The rows live at the top of the inspector; if that block is missing the
    // report still opens as JSON, so build into a detached node instead.
    const summary = $("diag-summary") || document.createElement("div");
    summary.className = "diag-summary";
    summary.hidden = false;
    summary.textContent = "Running model ping + search backends…";

    const { ok, data } = await api("/diagnose");
    state.diagnostics = data;
    summary.textContent = "";

    if (!ok) {
      summary.appendChild(el("div", "pdesc", "Diagnostics failed: " + errorText({ ok, status: 0, data })));
      setBadge("badge-diag", "Diag  failed", "bad");
      setHealth("state", "failed", "bad");
      return;
    }

    const model = data.model || {};
    if (model.ok) {
      summary.appendChild(diagRow(true, "model · " + model.provider, model.latency_ms + " ms"));
      summary.appendChild(
        el("div", "pdesc", model.model + (model.reply ? " · reply " + JSON.stringify(model.reply) : ""))
      );
      setHealth("model", model.model || model.provider, "ok");
    } else {
      const error = model.error || {};
      summary.appendChild(diagRow(false, "model · " + (error.provider || "none"), ""));
      if (error.message) summary.appendChild(el("div", "pdesc", error.message));
      if (error.hint) summary.appendChild(el("div", "pdesc", error.hint));
      setHealth("model", "failed", "bad");
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
    if (data.aras) {
      (data.aras.checked || []).forEach((report) => {
        summary.appendChild(
          diagRow(!!report.ok, "aras · " + report.service, report.ok ? report.latency_ms + " ms" : "")
        );
        if (!report.ok && report.error) summary.appendChild(el("div", "pdesc", report.error));
      });
      const ragReady = data.aras.rag_ready ? "rag ready" : "rag off";
      const dataReady = data.aras.data_ready ? "data ready" : "data off";
      summary.appendChild(el("div", "pdesc", "aras wiring · " + ragReady + " · " + dataReady));
    }
    (data.hints || []).forEach((hint) => summary.appendChild(el("div", "pdesc", "→ " + hint)));

    const modelOk = !!model.ok;
    const anySearch = (search.ok || []).length > 0;
    const okCount = (search.checked || []).filter((report) => report.ok).length;
    setHealth("search", okCount + " ok", anySearch ? "ok" : "bad");
    setHealth("state", "checked", modelOk && anySearch ? "ok" : "bad");
    setBadge(
      "badge-diag",
      "Diag  " + (modelOk ? "model ok" : "model failed") + " · " + (anySearch ? "search ok" : "search failed"),
      modelOk && anySearch ? "ok" : "bad"
    );
    showInspector(data, "Diagnostics report (GET /diagnose)");
    openInspector();
    summary.hidden = false; // showInspector hides the rows for run records
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
    env.appendChild(el("code", null, "LLM_PROVIDER=" + provider.name + "  " + provider.model_env + "=auto"));
    card.appendChild(env);
    if (provider.notes) card.appendChild(el("div", "pc-note", provider.notes));
    return card;
  }

  function openProvidersModal() {
    $("providers-modal").classList.remove("panel-hidden");
    $("providers-modal").classList.add("open");
  }

  function closeProvidersModal() {
    $("providers-modal").classList.remove("open");
    $("providers-modal").classList.add("panel-hidden");
  }

  async function openProviders() {
    const body = $("providers-body");
    body.textContent = "loading…";
    openProvidersModal();
    if (!state.fullProviders) await loadProviders();
    const data = state.fullProviders || {};
    body.textContent = "";

    body.appendChild(el("h3", "provider-group-title", "Core model"));
    (data.providers || []).forEach((provider) => body.appendChild(providerCard(provider)));

    body.appendChild(el("h3", "provider-group-title", "Web search (free)"));
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

  /* ─────────────────────── harness memory ──────────────────── */
  // The conversation lives on the server (memory/), not only in this tab:
  // reopening a session re-renders every turn from the stored payload.
  function updateSessionPill() {
    const pill = $("session-pill");
    if (pill) {
      if (!state.sessionId) {
        pill.textContent = "new conversation";
        pill.className = "session-time";
        pill.title = "The next message starts a conversation in memory/";
      } else {
        pill.textContent = "memory: " + (state.sessionTurns || 0) + " turn(s) · " + state.sessionId;
        pill.className = "session-time live";
        pill.title =
          "Session " + state.sessionId +
          " — stored in memory/, restored on reload, and sent as context with the next message";
      }
    }
    const name = $("session-name");
    if (name) {
      const current = state.sessions.find((item) => item.session_id === state.sessionId);
      name.textContent = (current && current.title) || (state.sessionId ? "Restored conversation" : "New conversation");
    }
    const memoryCount = $("memory-count");
    if (memoryCount) memoryCount.textContent = state.sessions.length ? String(state.sessions.length) : "—";
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
    const list = $("sessions-list");
    if (!list) return;
    const { ok, data } = await api("/sessions?limit=25");
    if (!ok) return;
    state.sessions = data.sessions || [];
    renderSessions();
    updateSessionPill();
  }

  function renderSessions() {
    const list = $("sessions-list");
    if (!list) return;
    list.textContent = "";
    if (!state.sessions.length) {
      list.appendChild(el("li", "panel-meta muted", "No conversations in memory yet."));
      return;
    }
    state.sessions.slice(0, 12).forEach((item) => {
      const row = el(
        "li",
        "session-item session-card" + (item.session_id === state.sessionId ? " selected" : "")
      );
      const mark = el("span", "session-icon");
      mark.appendChild(icon(["M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5v-15Z", "M4 6h12M8 10h8"]));
      row.appendChild(mark);
      const main = el("span", "session-main session-copy");
      main.appendChild(el("span", "session-title", item.title || item.session_id));
      const bits = [(item.turns || 0) + " turn(s)", formatWhen(item.updated_at)];
      if (item.agents && item.agents.length) bits.push(item.agents.join(" → "));
      else if (item.modes && item.modes.length) bits.push(item.modes.join(" · "));
      main.appendChild(el("span", "session-meta session-time", bits.join(" · ")));
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
    if (chat) chat.textContent = "";
    const messages = el("div", "messages");
    messages.id = "messages";
    if (chat) chat.appendChild(messages);
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

  /* ──────────────────────── live thoughts ───────────────────── */
  // A run takes a while, and the interesting part is what happens during it:
  // the planner choosing agents, each agent's prompt and reply, the tools it
  // called, the evaluator's verdict, the manager's waves. While a run is in
  // flight the page reads that from POST /orchestrate/stream (server-sent
  // events) and draws it above the answer: one row per actor, each row opening
  // to the real text, with a spinner on whoever is working right now. The
  // answer itself still arrives once, in its own bubble.

  const THOUGHT_KIND_LABELS = {
    prompt: "sent",
    reply: "reply",
    tool: "tool",
    verdict: "verdict",
    rewrite: "rewritten",
    status: "status",
    note: "note",
  };

  function timeLabel(ms) {
    const value = Number(ms) || 0;
    if (value < 1000) return Math.round(value) + " ms";
    const seconds = value / 1000;
    if (seconds < 60) return (seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)) + " s";
    return Math.floor(seconds / 60) + ":" + String(Math.round(seconds % 60)).padStart(2, "0");
  }

  // Server-sent events: ``event: name`` + ``data: json``, frames split on a
  // blank line, ``:`` lines are keep-alive pings.
  function readSseFrame(frame) {
    let name = "message";
    const data = [];
    frame.split("\n").forEach((line) => {
      if (!line || line.charAt(0) === ":") return;
      if (line.indexOf("event:") === 0) name = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) data.push(line.slice(5).trim());
    });
    if (!data.length) return null;
    try {
      return { event: name, data: JSON.parse(data.join("\n")) };
    } catch (_) {
      return null;
    }
  }

  async function streamOrchestrate(body, panel) {
    const path = "/orchestrate";
    let response;
    try {
      response = await fetch(apiUrl("/orchestrate/stream"), {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body,
      });
    } catch (error) {
      return {
        ok: false,
        status: 0,
        path,
        data: { message: "Cannot reach the backend: " + error.message },
      };
    }

    // Validation still answers with plain JSON — nothing was streamed.
    if (!response.ok) {
      let data = {};
      try {
        data = await response.json();
      } catch (_) {
        data = {};
      }
      return { ok: false, status: response.status, path, data };
    }

    let result = null;
    let failure = null;
    let broke = null;

    function consume(frame) {
      const parsed = readSseFrame(frame);
      if (!parsed) return;
      if (parsed.event === "open") {
        if (panel) panel.open(parsed.data);
      } else if (parsed.event === "thought") {
        if (panel) panel.add(parsed.data);
      } else if (parsed.event === "result") {
        result = parsed.data;
      } else if (parsed.event === "error") {
        failure = parsed.data;
      }
    }

    if (!response.body || !response.body.getReader || typeof TextDecoder !== "function") {
      // No streaming reader (old browser, stubbed fetch): same frames, all at
      // once. The answer still arrives; only the live part is lost.
      const whole = await response.text();
      whole.split("\n\n").forEach(consume);
    } else {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        let chunk;
        try {
          chunk = await reader.read();
        } catch (error) {
          broke = error;
          break;
        }
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        let cut = buffer.indexOf("\n\n");
        while (cut >= 0) {
          consume(buffer.slice(0, cut));
          buffer = buffer.slice(cut + 2);
          cut = buffer.indexOf("\n\n");
        }
      }
    }

    if (result) return { ok: true, status: 200, path, data: result };
    if (failure) {
      return {
        ok: false,
        status: failure.status || 502,
        path,
        data: failure.message ? failure : { ...failure, message: "The run failed." },
      };
    }
    return {
      ok: false,
      status: 0,
      path,
      data: {
        message: broke
          ? "The stream broke: " + (broke.message || broke)
          : "The stream ended before the answer was written.",
        hint: "The thoughts above show how far the run got; try again.",
      },
    };
  }

  function thoughtChips(detail) {
    const chips = [];
    if (detail.tool) chips.push(detail.tool + (detail.query ? " · " + detail.query : ""));
    if (detail.model) chips.push(detail.provider ? detail.provider + " / " + detail.model : detail.model);
    if (detail.status) chips.push("status: " + detail.status);
    if (detail.wave) chips.push("wave " + detail.wave);
    if (detail.round) chips.push("round " + detail.round);
    if (detail.quality) chips.push("quality: " + detail.quality);
    if (detail.agents && detail.agents.length) chips.push(detail.agents.join(", "));
    if (detail.latency_ms) chips.push(timeLabel(detail.latency_ms));
    if (detail.chars) chips.push(detail.chars + " chars");
    if (detail.revised) chips.push("revised");
    return chips.slice(0, 5);
  }

  function thoughtExtras(item) {
    const detail = item.detail || {};
    const chips = thoughtChips(detail);
    const rows = Array.isArray(detail.items) ? detail.items : [];
    const gaps = Array.isArray(detail.gaps) ? detail.gaps : [];
    if (!chips.length && !rows.length && !gaps.length) return null;

    const wrap = el("div", "ti-detail");
    if (chips.length) {
      const chipRow = el("div", "ti-chips");
      chips.forEach((text) => chipRow.appendChild(el("span", "ti-chip", text)));
      wrap.appendChild(chipRow);
    }
    if (rows.length) {
      const scores = el("div", "ti-scores");
      rows.forEach((row) => {
        const line = el("div", "ti-score");
        line.appendChild(el("span", "ti-score-name", row.label || row.agent || "agent"));
        line.appendChild(
          el("span", "ti-score-value", row.score == null ? "–" : row.score + "/5")
        );
        if (row.verdict) line.appendChild(el("span", "ti-score-verdict", row.verdict));
        scores.appendChild(line);
      });
      wrap.appendChild(scores);
    }
    if (gaps.length) {
      const list = el("ul", "ti-gaps");
      gaps.slice(0, 4).forEach((gap) => list.appendChild(el("li", null, gap)));
      wrap.appendChild(list);
    }
    return wrap;
  }

  function thoughtItemNode(item) {
    const text = String(item.text || "");
    const kind = THOUGHT_KIND_LABELS[item.kind] || item.kind || "note";
    // Short, self-contained lines (status, one-clause notes) read better flat;
    // everything with a body of text opens.
    const flat = item.kind === "status" || (text.length <= 96 && text === item.title);
    if (flat) {
      const row = el("div", "thought-item flat");
      row.dataset.kind = item.kind || "note";
      row.appendChild(el("span", "ti-kind", kind));
      row.appendChild(el("span", "ti-flat", text || item.title));
      row.appendChild(el("span", "ti-time", timeLabel(item.offset_ms)));
      row.title = item.title || "";
      return row;
    }

    const details = el("details", "thought-item");
    details.dataset.kind = item.kind || "note";
    const summary = el("summary");
    summary.appendChild(el("span", "ti-kind", kind));
    summary.appendChild(el("span", "ti-title", item.title || "Thought"));
    summary.appendChild(el("span", "ti-time", timeLabel(item.offset_ms)));
    const body = el("div", "ti-body");
    if (text) body.appendChild(el("p", "ti-text", text));
    const extras = thoughtExtras(item);
    if (extras) body.appendChild(extras);
    details.appendChild(summary);
    details.appendChild(body);
    return details;
  }

  function thoughtsPanel(skeleton) {
    const root = el("div", "thoughts");
    root.dataset.state = "live";
    const head = el("div", "thoughts-head");
    const spinner = el("span", "thoughts-spinner");
    spinner.setAttribute("aria-hidden", "true");
    const title = el("span", "thoughts-title", "Handing the request to the manager…");
    const timer = el("span", "thoughts-timer", "0:00");
    head.appendChild(spinner);
    head.appendChild(title);
    head.appendChild(timer);
    const list = el("div", "thoughts-list");
    root.appendChild(head);
    root.appendChild(list);
    skeleton.body.insertBefore(root, skeleton.bubble);

    const started = Date.now();
    const groups = new Map();
    let active = null;
    let count = 0;

    const clock = setInterval(() => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      timer.textContent = Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
    }, 500);

    function groupFor(item) {
      let group = groups.get(item.group);
      if (group) return group;
      const details = el("details", "thought-group");
      details.dataset.category = item.category || "manager";
      details.open = true;
      const summary = el("summary");
      const label = el("span", "tg-label", item.label || "Manager");
      const meta = el("span", "tg-meta", "0");
      const live = el("span", "tg-live");
      live.setAttribute("aria-hidden", "true");
      summary.appendChild(el("span", "tg-dot"));
      summary.appendChild(label);
      summary.appendChild(meta);
      summary.appendChild(live);
      const items = el("div", "tg-items");
      details.appendChild(summary);
      details.appendChild(items);
      // once the user opens or closes a group, we stop deciding for them
      summary.addEventListener("click", () => {
        details.dataset.touched = "1";
      });
      list.appendChild(details);
      group = { details, items, meta, live, label: item.label || "Manager" };
      groups.set(item.group, group);
      return group;
    }

    function nearBottom() {
      const chat = $("chat");
      if (!chat) return true;
      return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 180;
    }

    return {
      open() {
        title.textContent = "Manager is reading the request…";
      },
      add(item) {
        if (!item) return;
        const stick = nearBottom();
        count += 1;
        const group = groupFor(item);
        group.items.appendChild(thoughtItemNode(item));
        group.meta.textContent = String(group.items.childNodes.length);
        if (active && active !== group) active.live.classList.remove("on");
        group.live.classList.add("on");
        active = group;

        // the working group is open; finished ones fold away with their count
        // still visible — unless the user has opened them themselves
        groups.forEach((other) => {
          if (other.details.dataset.touched) return;
          other.details.open = other === group;
        });
        title.textContent = group.label + " · " + (item.title || "working");
        if (stick) scrollChat();
      },
      finish(ok) {
        clearInterval(clock);
        root.dataset.state = ok ? "done" : "stopped";
        spinner.classList.add("off");
        if (!count) {
          root.remove();
          return;
        }
        const seconds = Math.round((Date.now() - started) / 100);
        title.textContent = ok
          ? "Thought through " + count + " step" + (count === 1 ? "" : "s")
          : "Stopped after " + count + " step" + (count === 1 ? "" : "s");
        timer.textContent = timeLabel(seconds * 100);
        if (active) active.live.classList.remove("on");
        groups.forEach((group) => {
          if (!group.details.dataset.touched) group.details.open = false;
        });
        scrollChat();
      },
    };
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

    const waiting = state.permissionPrompt ? pendingFiles() : [];
    if (waiting.length) {
      showBanner(
        "Permission needed",
        "Allow or remove " + waiting.map((item) => item.name).join(", ") + " before sending."
      );
      setTimeout(hideBanner, 6000);
      renderConsent();
      return;
    }

    state.busy = true;
    const sendBtn = $("btn-send");
    sendBtn.disabled = true;
    const sendLabel = sendBtn.textContent;
    sendBtn.textContent = "Running…";
    const runStatus = $("run-status");
    if (runStatus) runStatus.textContent = "Run in progress…";

    const mode = state.mode;
    const hasFiles = state.files.length > 0;
    appendUser(text, state.files.map((item) => item.name));
    input.value = "";
    const skeleton = appendAssistantSkeleton();

    let result;
    try {
      if (mode === "auto") {
        let fileIds = [];
        skeleton.text.textContent = "thinking · planning agents…";
        if (hasFiles) {
          skeleton.text.textContent = "uploading " + state.files.length + " file(s)…";
          const uploaded = await uploadAttachments();
          if (uploaded.error) {
            result = { ok: false, status: 0, path: "/files", data: { message: uploaded.error } };
            state.busy = false;
            sendBtn.disabled = false;
            sendBtn.textContent = sendLabel;
            finishAssistant(skeleton, result, text, mode);
            if (runStatus) runStatus.textContent = "Last run failed";
            return;
          }
          fileIds = uploaded.ids;
          if (uploaded.warnings && uploaded.warnings.length) {
            showBanner("Files kept in memory only", uploaded.warnings.join(" · "));
            setTimeout(hideBanner, 8000);
          }
          state.files = [];
          renderAttachments();
        }
        skeleton.text.textContent = "";
        const panel = thoughtsPanel(skeleton);
        result = await streamOrchestrate(
          JSON.stringify({
            message: text,
            history: historyPayload(),
            file_ids: fileIds,
            // continue the conversation that is in memory (created when null)
            session_id: state.sessionId,
          }),
          panel
        );
        panel.finish(result.ok);
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

    if (runStatus) {
      runStatus.textContent = result && result.ok
        ? "Preview complete · " + (result.data.latency_ms || 0) + " ms"
        : "Last run failed — open the inspector";
    }

    state.busy = false;
    sendBtn.disabled = false;
    sendBtn.textContent = sendLabel;
    if (mode === "auto" && result && result.ok) loadRunLog();
    scrollChat();
  }

  /* ─────────────────────────── run history ─────────────────── */
  function renderHistory() {
    const list = $("trace-list");
    if (!list) return;
    state.historySummary = state.runs.length
      ? state.runs.length + " in this tab"
      : "Nothing in this tab yet.";
    updateRunsSummary();
    const count = $("run-count");
    if (count) count.textContent = String(state.runs.length).padStart(2, "0");
    list.textContent = "";
    if (!state.runs.length) {
      list.appendChild(el("li", "panel-meta muted", "Send a prompt — runs land here."));
      return;
    }
    state.runs.forEach((run) => {
      const item = el("li");
      const button = el("button", "history-item");
      button.type = "button";
      button.appendChild(document.createTextNode(run.prompt.slice(0, 64)));
      button.appendChild(
        el(
          "span",
          "history-meta",
          run.mode + (run.agents ? " · " + run.agents : "") +
            " · " + (run.provider || "search") + " · " + run.latency_ms + " ms"
        )
      );
      button.addEventListener("click", () => {
        showInspector(run.payload, run.mode + " run · " + run.ts);
        openInspector();
      });
      item.appendChild(button);
      list.appendChild(item);
    });
  }

  /* ─────────────────────── self-audit (Evaluate) ───────────── */
  // One button, eighteen checks, one scorecard. The list is known before a run
  // (GET /audit), so the panel opens as a visible checklist instead of a
  // spinner; a run then fills each row as its verdict lands. Offline by
  // default — the checks drive the real pipeline through scripted ports — and
  // the two live checks are opt-in and skip loudly when no key is configured.
  const AUDIT = {
    specs: [],
    levels: [],
    results: {},      // id → check record from the current/last run
    running: false,
    tab: "results",
    card: null,
    live: false,
    liveAvailable: false,
    queue: [],        // verdicts waiting to be revealed, one at a time
    draining: null,
    pending: null,    // the finished scorecard, held until the last row is shown
    startedAt: 0,
  };

  function auditTone(status) {
    if (status === "pass") return "ok";
    if (status === "warn") return "warn";
    if (status === "fail") return "bad";
    return "";
  }

  function auditTotal() {
    return AUDIT.specs.length;
  }

  function auditSettled() {
    return Object.keys(AUDIT.results).length;
  }

  function auditSpec(id) {
    return AUDIT.specs.find((spec) => spec.id === id) || null;
  }

  function auditMetricsValue(value) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "boolean") return value ? "yes" : "no";
    if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function auditRow(spec, result) {
    const status = result ? result.status : AUDIT.running ? "queued" : "queued";
    const row = el("div", "audit-row");
    row.dataset.status = status;
    row.dataset.id = spec.id;
    row.appendChild(el("span", "audit-dot"));

    const main = el("div", "audit-row-main");
    const title = el("div", "audit-row-title");
    title.appendChild(el("span", null, spec.title));
    title.appendChild(el("code", null, spec.id));
    main.appendChild(title);
    main.appendChild(el("div", "audit-row-proves", spec.proves));

    if (result && result.detail) main.appendChild(el("div", "audit-row-detail", result.detail));
    if (result && result.findings && result.findings.length) {
      const list = el("ul", "audit-row-findings");
      result.findings.slice(0, 6).forEach((finding) => list.appendChild(el("li", null, finding)));
      main.appendChild(list);
    }

    const meta = el("div", "audit-row-meta");
    if (spec.needs && spec.needs !== "nothing") {
      const chip = el("span", "audit-chip");
      chip.appendChild(document.createTextNode("needs "));
      chip.appendChild(el("b", null, spec.needs));
      meta.appendChild(chip);
    }
    if (spec.live) meta.appendChild(el("span", "audit-chip", "live"));
    if (result && result.metrics && Object.keys(result.metrics).length) {
      const details = el("details", "audit-metrics");
      details.appendChild(el("summary", null, "metrics"));
      const table = el("table");
      Object.keys(result.metrics).forEach((key) => {
        const tr = el("tr");
        tr.appendChild(el("th", null, key));
        tr.appendChild(el("td", null, auditMetricsValue(result.metrics[key])));
        table.appendChild(tr);
      });
      details.appendChild(table);
      main.appendChild(details);
    }
    if (meta.childNodes.length) main.appendChild(meta);
    row.appendChild(main);
    row.appendChild(el("span", "audit-row-ms", result ? String(result.ms) + " ms" : ""));

    if (status === "fail") row.appendChild(el("span", "sr-only", "failed"));
    return row;
  }

  function renderAudit() {
    const host = $("audit-results");
    if (!host) return;
    host.textContent = "";
    if (!AUDIT.specs.length) {
      const empty = el("div", "audit-empty");
      empty.appendChild(document.createTextNode("The audit catalogue did not load. "));
      empty.appendChild(el("span", null, "Check the backend with Run diagnostics, then reopen this panel."));
      host.appendChild(empty);
      return;
    }
    const order = AUDIT.levels.length
      ? AUDIT.levels
      : [{ id: "plan", title: "Planning" }];
    order.forEach((level) => {
      const specs = AUDIT.specs.filter((spec) => spec.level === level.id);
      if (!specs.length) return;
      const group = el("div", "audit-level");
      group.appendChild(el("div", "audit-level-head", level.title.split(" — ")[0]));
      specs.forEach((spec) => group.appendChild(auditRow(spec, AUDIT.results[spec.id])));
      host.appendChild(group);
    });
    renderAuditSummary();
  }

  function renderAuditSummary() {
    const host = $("audit-summary");
    if (!host) return;
    const card = AUDIT.card;
    host.textContent = "";
    if (!card) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    const summary = card.summary || {};
    const score = el("span", "audit-score " + (card.status || "pass"));
    score.textContent =
      (card.status === "pass" ? "✓ all clear" : card.status === "warn" ? "! warnings" : "× failures");
    host.appendChild(score);
    [
      ["pass", summary.pass || 0],
      ["warn", summary.warn || 0],
      ["fail", summary.fail || 0],
      ["skip", summary.skip || 0],
    ].forEach(([name, count]) => {
      const chip = el("span", "audit-chip");
      chip.appendChild(document.createTextNode(name + " "));
      chip.appendChild(el("b", null, String(count)));
      host.appendChild(chip);
    });
    const time = el("span", "audit-chip");
    time.appendChild(document.createTextNode("total "));
    time.appendChild(el("b", null, (card.ms || 0) + " ms"));
    host.appendChild(time);
    const mode = el("span", "audit-chip");
    mode.appendChild(document.createTextNode(card.environment && card.environment.live_models ? "live" : "offline"));
    host.appendChild(mode);
    if (card.audit_id) {
      const id = el("span", "audit-chip");
      id.appendChild(el("b", null, card.audit_id));
      host.appendChild(id);
    }
    if (card.files && card.files.json) {
      host.appendChild(el("span", "audit-chip", "saved · " + String(card.files.json).split("/").pop()));
    }
  }

  function auditProgress() {
    const total = auditTotal();
    const settled = auditSettled();
    const fill = $("audit-bar-fill");
    if (fill) {
      fill.style.width = total ? Math.round((settled / total) * 100) + "%" : "0%";
      fill.classList.toggle("done", !AUDIT.running && settled === total && total > 0);
    }
    const count = $("audit-count");
    if (!count) return;
    if (AUDIT.running) {
      const waiting = AUDIT.specs.find((spec) => !AUDIT.results[spec.id]);
      count.textContent = settled + "/" + total + (waiting ? " · " + waiting.title.toLowerCase() + "…" : " · finishing…");
      return;
    }
    if (AUDIT.card) {
      count.textContent = settled + "/" + total + " · " + (AUDIT.card.ms || 0) + " ms";
      return;
    }
    count.textContent = total ? "ready · " + total + " checks" : "idle";
  }

  function renderAuditJson() {
    const view = $("audit-json");
    if (!view) return;
    view.textContent = AUDIT.card
      ? JSON.stringify(AUDIT.card, null, 2)
      : '{\n  "hint": "Run the audit to produce a scorecard."\n}';
  }

  function setAuditTab(tab) {
    AUDIT.tab = tab === "json" ? "json" : "results";
    document.querySelectorAll(".audit-tab").forEach((button) => {
      const active = button.dataset.auditTab === AUDIT.tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    const results = $("audit-results");
    const json = $("audit-json");
    if (results) results.classList.toggle("panel-hidden", AUDIT.tab !== "results");
    if (json) json.classList.toggle("panel-hidden", AUDIT.tab !== "json");
  }

  function openAudit() {
    const modal = $("audit-modal");
    if (!modal) return;
    modal.classList.remove("panel-hidden");
    const live = $("audit-live");
    if (live) {
      live.checked = AUDIT.live;
      live.disabled = !AUDIT.liveAvailable;
      const wrap = $("audit-live-wrap");
      if (wrap) {
        wrap.classList.toggle("disabled", !AUDIT.liveAvailable);
        wrap.title = AUDIT.liveAvailable
          ? "Also call the configured model: planner agreement + stage latency"
          : "No model provider configured — live checks would skip";
      }
    }
    setAuditTab(AUDIT.tab);
    renderAudit();
    renderAuditJson();
    auditProgress();
    if (!AUDIT.specs.length) loadAuditCatalogue();
    const run = $("btn-audit-run");
    if (run) run.focus();
  }

  function closeAudit() {
    const modal = $("audit-modal");
    if (modal) modal.classList.add("panel-hidden");
  }

  async function loadAuditCatalogue() {
    const { ok, data } = await api("/audit");
    if (!ok) {
      setBadge("badge-audit", "Audit  unavailable", "bad");
      return;
    }
    AUDIT.specs = data.checks || [];
    AUDIT.levels = data.levels || [];
    AUDIT.liveAvailable = Boolean(data.live_available);
    const env = data.environment || {};
    const sub = $("audit-sub");
    if (sub) {
      // say what this host is wired to, so a skip is never a surprise
      sub.textContent =
        "Offline checks drive the real pipeline through scripted ports: no key, no network, under a second." +
        " · planner " + (env.planner || "?") +
        " · evaluator " + (env.evaluate === false ? "off" : "on") +
        " · review " + (env.review === false ? "off" : "on") +
        " · model " + (env.model_ready ? "ready" : "no key (live checks skip)");
    }
    if (data.last) {
      applyAuditCard(data.last);
    } else {
      setBadge("badge-audit", "Audit: not run", "");
    }
    renderAudit();
    renderAuditJson();
    auditProgress();
  }

  function applyAuditCard(card, options) {
    const quiet = options && options.quiet;
    AUDIT.card = card;
    AUDIT.results = {};
    (card.checks || []).forEach((row) => {
      AUDIT.results[row.id] = row;
    });
    AUDIT.running = false;
    const summary = card.summary || {};
    const total = summary.total || (card.checks || []).length;
    let text = "Audit: " + (summary.pass || 0) + "/" + total + " pass";
    if (summary.fail) text += " · " + summary.fail + " fail";
    else if (summary.warn) text += " · " + summary.warn + " warn";
    setBadge("badge-audit", text, auditTone(card.status));
    renderAudit();
    renderAuditJson();
    auditProgress();
    if (!quiet) {
      const note = $("audit-note");
      if (note) {
        note.textContent =
          "Scorecard saved to " +
          ((card.files && card.files.json) || "logs/audit") +
          " — and to latest.json, so the next version can be diffed against this one.";
      }
    }
  }

  // Verdicts arrive faster than a reader can follow (most checks take ~1 ms), so
  // they are revealed one at a time. Ordering still matches the run: `start`
  // marks the row that is running, the drain reveals the finished ones.
  function queueAuditReveal(record) {
    AUDIT.queue.push(record);
    if (AUDIT.draining) return;
    AUDIT.draining = setInterval(() => {
      const next = AUDIT.queue.shift();
      if (!next) {
        clearInterval(AUDIT.draining);
        AUDIT.draining = null;
        // the verdicts are all on screen: publish the scorecard they came from
        if (AUDIT.pending) finishAudit();
        return;
      }
      if (AUDIT.results[next.id]) return;
      AUDIT.results[next.id] = next;
      renderAudit();
      auditProgress();
    }, 90);
  }

  function finishAudit() {
    const card = AUDIT.pending;
    AUDIT.pending = null;
    if (!card) return;
    applyAuditCard(card);
    const summary = card.summary || {};
    toast(
      "Audit finished: " + (summary.pass || 0) + " passed" +
        (summary.skip ? " · " + summary.skip + " skipped" : "") +
        (summary.fail ? " · " + summary.fail + " failed" : ""),
      auditTone(card.status) || ""
    );
  }

  async function runAudit() {
    if (AUDIT.running) return;
    AUDIT.running = true;
    AUDIT.card = null;
    AUDIT.results = {};
    AUDIT.queue = [];
    AUDIT.pending = null;
    AUDIT.startedAt = performance.now();
    if (AUDIT.draining) {
      clearInterval(AUDIT.draining);
      AUDIT.draining = null;
    }
    setBadge("badge-audit", "Audit  running…", "warn");
    renderAudit();
    renderAuditSummary();
    renderAuditJson();
    auditProgress();

    const live = AUDIT.live && AUDIT.liveAvailable;
    const path = "/audit/stream" + (live ? "?live=1" : "");
    let response;
    try {
      response = await fetch(apiUrl(path), { headers: { Accept: "text/event-stream" } });
    } catch (error) {
      AUDIT.running = false;
      setBadge("badge-audit", "Audit  failed", "bad");
      toast("Cannot reach the backend: " + error.message, "bad");
      renderAudit();
      return;
    }
    if (!response.ok) {
      AUDIT.running = false;
      setBadge("badge-audit", "Audit  failed", "bad");
      toast("The audit did not start (HTTP " + response.status + ")", "warn");
      renderAudit();
      return;
    }

    let done = false;
    const consume = (frame) => {
      const parsed = readSseFrame(frame);
      if (!parsed) return;
      if (parsed.event === "start") {
        const row = document.querySelector('.audit-row[data-id="' + parsed.data.id + '"]');
        if (row) row.dataset.status = "running";
        auditProgress();
      } else if (parsed.event === "check") {
        queueAuditReveal(parsed.data);
      } else if (parsed.event === "result") {
        done = true;
        // hold the scorecard until the reveal queue has caught up, so the last
        // rows animate in instead of the whole list snapping to its final state
        AUDIT.pending = parsed.data;
        if (!AUDIT.queue.length && !AUDIT.draining) finishAudit();
      } else if (parsed.event === "error") {
        done = true;
        AUDIT.running = false;
        AUDIT.pending = null;
        AUDIT.queue = [];
        setBadge("badge-audit", "Audit  failed", "bad");
        toast(parsed.data.message || "The audit failed", "bad");
        renderAudit();
      }
    };

    if (!response.body || !response.body.getReader || typeof TextDecoder !== "function") {
      (await response.text()).split("\n\n").forEach(consume);
    } else {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        let chunk;
        try {
          chunk = await reader.read();
        } catch (_) {
          break;
        }
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        let cut = buffer.indexOf("\n\n");
        while (cut >= 0) {
          consume(buffer.slice(0, cut));
          buffer = buffer.slice(cut + 2);
          cut = buffer.indexOf("\n\n");
        }
      }
    }
    if (!done && AUDIT.running) {
      // the socket closed without a verdict: say so instead of spinning forever
      AUDIT.running = false;
      setBadge("badge-audit", "Audit  interrupted", "warn");
      toast("The audit stream closed early", "warn");
      renderAudit();
      auditProgress();
    }
  }

  function auditJsonText() {
    return AUDIT.card ? JSON.stringify(AUDIT.card, null, 2) : "";
  }

  async function copyAuditJson() {
    const text = auditJsonText();
    if (!text) {
      toast("Run the audit first", "warn");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast("Scorecard copied");
    } catch (_) {
      setAuditTab("json");
      toast("Clipboard blocked — the JSON is in the JSON tab", "warn");
    }
  }

  function downloadAuditJson() {
    const text = auditJsonText();
    if (!text) {
      toast("Run the audit first", "warn");
      return;
    }
    const name = (AUDIT.card.audit_id || "audit") + ".json";
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Downloaded " + name);
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

    const sendBtn = $("btn-send");
    if (sendBtn) sendBtn.addEventListener("click", send);
    if ($("consent-allow-all")) $("consent-allow-all").addEventListener("click", () => decideAll(true));
    if ($("consent-deny-all")) $("consent-deny-all").addEventListener("click", () => decideAll(false));

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

    const on = (id, event, handler) => {
      const node = $(id);
      if (node) node.addEventListener(event, handler);
    };

    on("btn-theme", "click", toggleTheme);
    // the stored choice is the source of truth; the head script already
    // applied it before paint, this keeps the button in sync
    const savedTheme = store.getItem(STORAGE.theme);
    applyTheme(
      savedTheme === "light" || savedTheme === "dark"
        ? savedTheme
        : document.documentElement.dataset.theme === "light"
          ? "light"
          : "dark"
    );
    wireGutter("sidebar");
    wireGutter("inspector");
    window.addEventListener("resize", () => {
      applyLayout();
      Object.keys(LAYOUT).forEach((side) => {
        if (layout[side]) setWidth(side, layout[side], false);
      });
    });

    on("btn-refresh-agents", "click", loadAgents);
    on("btn-refresh-runs", "click", () => {
      renderHistory();
      loadRunLog();
    });
    on("btn-clear", "click", newSession);
    on("btn-new-session", "click", newSession);
    on("btn-diag-top", "click", runDiagnostics);
    on("btn-diag-side", "click", runDiagnostics);
    on("btn-audit-top", "click", openAudit);
    on("btn-close-audit", "click", closeAudit);
    on("btn-audit-close", "click", closeAudit);
    on("btn-audit-run", "click", runAudit);
    on("btn-audit-copy", "click", copyAuditJson);
    on("btn-audit-download", "click", downloadAuditJson);
    on("audit-live", "change", (event) => {
      AUDIT.live = Boolean(event.target.checked);
    });
    document.querySelectorAll(".audit-tab").forEach((button) => {
      button.addEventListener("click", () => setAuditTab(button.dataset.auditTab));
    });
    const auditBackdrop = $("audit-modal");
    if (auditBackdrop) {
      auditBackdrop.addEventListener("click", (event) => {
        if (event.target === auditBackdrop) closeAudit();
      });
    }
    on("btn-providers", "click", openProviders);
    on("btn-providers-close", "click", closeProvidersModal);
    on("btn-close-providers", "click", closeProvidersModal);
    on("btn-refresh-search", "click", () => {
      state.fullProviders = null;
      loadProviders();
      loadHealth();
    });
    on("btn-clear-history", "click", () => {
      state.runs = [];
      saveRuns();
      renderHistory();
      setRunsTab("local");
    });
    on("btn-copy-trace", "click", async () => {
      try {
        await navigator.clipboard.writeText($("trace-json").textContent || "");
        toast("Payload copied");
      } catch (_) {
        /* clipboard may be blocked — the text stays selectable */
      }
    });
    on("btn-close-inspector", "click", closeInspector);
    on("btn-toggle-inspector", "click", () => {
      const workspace = $("workspace");
      if (workspace && workspace.classList.contains("inspector-collapsed")) openInspector();
      else closeInspector();
    });
    on("btn-menu", "click", () => {
      const sidebar = $("sidebar");
      if (sidebar) sidebar.classList.toggle("open");
    });
    on("workspace-switcher", "click", () => toast("This sandbox runs one workspace: the repo you cloned", ""));

    document.querySelectorAll("#runs-tabs .seg-btn").forEach((button) => {
      button.addEventListener("click", () => {
        setRunsTab(button.dataset.runs);
        if (state.runsTab === "saved") loadRunLog();
      });
    });
    setRunsTab(state.runsTab);

    document.querySelectorAll(".inspector-tab").forEach((button) => {
      button.addEventListener("click", () => renderInspectorTab(button.dataset.tab));
    });

    // sidebar nav: jump to a section (and pull fresh data for it)
    document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
      item.addEventListener("click", () => {
        document.querySelectorAll(".nav-item[data-view]").forEach((node) =>
          node.classList.toggle("active", node === item)
        );
        const view = item.dataset.view;
        if (view === "providers") {
          openProviders();
          return;
        }
        const target = view === "runs" ? $("section-runs") : view === "memory" ? $("section-sessions") : $("chat");
        if (target && target.scrollIntoView) target.scrollIntoView({ block: "nearest", behavior: "smooth" });
        if (view === "runs") loadRunLog();
        if (view === "memory") loadSessions();
        // on a phone the sidebar is a drawer: picking a section closes it
        const sidebar = $("sidebar");
        if (sidebar && window.innerWidth <= 700) sidebar.classList.remove("open");
      });
    });

    const prompt = $("prompt");
    if (prompt) {
      prompt.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          send();
        }
      });
    }

    document.querySelectorAll(".suggestion[data-prompt]").forEach((card) => {
      card.addEventListener("click", () => {
        if (!prompt) return;
        prompt.value = card.dataset.prompt || "";
        prompt.focus();
      });
    });

    const providers = $("providers-modal");
    if (providers) {
      providers.addEventListener("click", (event) => {
        if (event.target === providers) closeProvidersModal();
      });
    }

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeProvidersModal();
        closeAudit();
        const sidebar = $("sidebar");
        if (sidebar && sidebar.classList.contains("open")) sidebar.classList.remove("open");
      }
      // ⌘/Ctrl + I toggles the inspector
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "i") {
        event.preventDefault();
        const workspace = $("workspace");
        if (workspace && workspace.classList.contains("inspector-collapsed")) openInspector();
        else closeInspector();
      }
    });
  }

  async function boot() {
    // saved column widths first: the grid should never jump once painted
    applyLayout();
    // below 980px the inspector is a drawer and must not cover the chat on load
    if (window.innerWidth <= 980) closeInspector();
    renderModes();
    renderAttachments();
    renderHistory();
    wire();
    checkStyles();

    const online = await negotiate();
    if (!online) {
      renderSearchList([]);
      setBadge("badge-llm", "LLM  offline", "bad");
      setBadge("badge-search", "SEARCH  offline", "bad");
      setHealth("model", "offline");
      setHealth("search", "offline");
      setHealth("state", "backend down", "bad");
      renderAgentsPanel(null);
    } else {
      loadHealth();
      loadProviders();
      loadAgents();
      loadRunLog();
      loadAuditCatalogue();
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
            "/files", "/search", "/diagnose", "/search/diagnose", "/audit", "/audit/run",
            "/audit/stream",
            "/sessions", "/sessions/{id}",
          ],
        },
        "Nothing inspected yet — send a prompt or run diagnostics."
      );
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
