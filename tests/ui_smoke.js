#!/usr/bin/env node
/**
 * UI smoke test for the reported bug: "Not Found" on send + dead buttons.
 *
 * The Python suite tests the server; this tests the *page* against three ways it
 * can be served. It loads the real index.html and the real app.js in jsdom — no
 * build step, no browser.
 *
 *   npm i jsdom            # once, anywhere on NODE_PATH
 *   node tests/ui_smoke.js                       # static-host scenario only
 *   node tests/ui_smoke.js http://localhost:8000 # all three scenarios
 *
 * Scenario A — normal host: badges load, a prompt reaches /chat.
 * Scenario B — static host / copied file: every probe 404s, so the red banner
 *              must appear *and* the mode buttons must still be rendered.
 * Scenario C — backend only under /api (proxy): negotiation must find it and
 *              join base + path without producing "//".
 * Scenario I — the ARAS services: the sidebar list, the services panel, the
 *              status pills and the wiring report, configured and not (stubbed).
 *
 * The backend runs on the ARAS microservices only, so every stub below answers
 * in the shapes GET /health and GET /providers have: one model (the router),
 * one search (the web service), seven services.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML = fs.readFileSync(path.join(ROOT, 'app/static/index.html'), 'utf8');
const JS = fs.readFileSync(path.join(ROOT, 'app/static/assets/js/app.js'), 'utf8');
const CSS =
  fs.readFileSync(path.join(ROOT, 'app/static/assets/css/styles.css'), 'utf8') +
  fs.readFileSync(path.join(ROOT, 'app/static/assets/css/extra.css'), 'utf8');
const THEME_CSS = fs.readFileSync(path.join(ROOT, 'app/static/assets/css/theme.css'), 'utf8');
const liveBase = (process.argv[2] || '').replace(/\/+$/, '');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  console.log('SKIP tests/ui_smoke.js — jsdom is not installed (`npm i jsdom`)');
  process.exit(0);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const failures = [];

/* ── the ARAS backend, as the page sees it ────────────────────────────────
   Mirrors app/aras.py (SERVICES / catalogue) and app/main.py (/health,
   /providers, /aras). `configured: false` is the empty ARAS_* block. */
const ARAS_MODEL = 'sharif-rayen-gemma4-31b';
const ARAS_ANSWERS_AS = 'rayen-gemma4-31b';
const ARAS_SERVICES = [
  ['router', 'Router', 8101, 'The model: every planner, agent, evaluator and manager call.', 'POST /api/v1/chat/completions/', ['ARAS_ROUTER_TOKEN', 'ARAS_MODEL'], '/api/v1'],
  ['web', 'Web', 8108, 'Web search: the web_search tool of the search agent.', 'POST /v1/search/', ['ARAS_WEB_TOKEN'], ''],
  ['ocr', 'OCR', 8102, 'Reads uploaded PDFs into text for the document agent.', 'POST /api/v1/ocr/', ['ARAS_OCR_TOKEN'], ''],
  ['retrieval', 'Retrieval', 8105, "The document agent's knowledge store (RAG) when no file is attached.", 'POST /v1/search/', ['ARAS_RETRIEVAL_TOKEN', 'ARAS_COLLECTION', 'ARAS_RETRIEVAL_MODE'], ''],
  ['ingestion', 'Ingestion', 8104, 'Indexes accepted uploads into the collection when ARAS_AUTO_INGEST=true.', 'POST /v1/ingest/', ['ARAS_INGESTION_TOKEN', 'ARAS_COLLECTION', 'ARAS_AUTO_INGEST'], ''],
  ['data', 'Data', 8107, "The data agent's analytics backend when there is nothing local to compute.", 'POST /v1/query/', ['ARAS_DATA_TOKEN', 'ARAS_TENANT', 'ARAS_OWNER', 'ARAS_DATA_SOURCE_ID'], ''],
  ['vector', 'Vector store', 8103, 'Health and diagnostics only — ingestion and retrieval are the working doors.', 'GET /v1/collections/', ['ARAS_VECTOR_TOKEN'], ''],
];
// what each service still needs when only URL + tokens + the model id are set
const ARAS_PARTIAL = {
  retrieval: ['ARAS_COLLECTION'],
  ingestion: ['ARAS_COLLECTION'],
  data: ['ARAS_TENANT', 'ARAS_OWNER', 'ARAS_DATA_SOURCE_ID'],
};
const ARAS_UNSET = {
  router: ['ARAS_BASE_URL', 'ARAS_ROUTER_TOKEN', 'ARAS_MODEL'],
  web: ['ARAS_BASE_URL', 'ARAS_WEB_TOKEN'],
  ocr: ['ARAS_BASE_URL', 'ARAS_OCR_TOKEN'],
  retrieval: ['ARAS_BASE_URL', 'ARAS_RETRIEVAL_TOKEN', 'ARAS_COLLECTION'],
  ingestion: ['ARAS_BASE_URL', 'ARAS_INGESTION_TOKEN', 'ARAS_COLLECTION'],
  data: ['ARAS_BASE_URL', 'ARAS_DATA_TOKEN', 'ARAS_TENANT', 'ARAS_OWNER', 'ARAS_DATA_SOURCE_ID'],
  vector: ['ARAS_BASE_URL', 'ARAS_VECTOR_TOKEN'],
};

function arasMissing(configured) {
  return configured ? ARAS_PARTIAL : ARAS_UNSET;
}

function arasHealth(version, configured = true) {
  const missing = arasMissing(configured);
  return {
    status: 'ok',
    version,
    api_prefixes: ['', '/api'],
    model_provider_ready: configured,
    model_chain: configured ? ['aras'] : [],
    search_chain: configured ? ['aras'] : [],
    model: {
      provider: 'aras', service: 'router', ready: configured,
      endpoint: configured ? 'http://127.0.0.1:8101/api/v1' : '',
      model: configured ? ARAS_MODEL : '',
      missing: missing.router || [],
    },
    search: {
      provider: 'aras', service: 'web', ready: configured,
      endpoint: configured ? 'http://127.0.0.1:8108' : '',
      missing: missing.web || [],
    },
    aras: { configured: configured ? ARAS_SERVICES.map((row) => row[0]) : [], rag_ready: false, data_ready: false },
    env_file: configured,
  };
}

function arasProviders(version, configured = true) {
  const missing = arasMissing(configured);
  const health = arasHealth(version, configured);
  return {
    version,
    model: health.model,
    search: health.search,
    env_file: configured,
    services: ARAS_SERVICES.map(([service, label, port, role, route, env, prefix]) => {
      const need = missing[service] || [];
      const state = !configured ? 'not configured' : need.length ? 'incomplete' : 'ready';
      return {
        service, label, port, role, route, env,
        endpoint: configured ? `http://127.0.0.1:${port}${prefix}` : '',
        state, ready: state === 'ready', configured, required: service === 'router',
        missing_env: need,
      };
    }),
  };
}

function arasReport(version, configured = true) {
  const names = ARAS_SERVICES.map((row) => row[0]);
  return {
    configured: configured ? names : [],
    skipped: configured ? [] : names,
    checked: configured ? names.map((service) => ({ service, ok: true, configured: true, latency_ms: 12 })) : [],
    model: configured ? ARAS_MODEL : '',
    model_ready: configured,
    search_ready: configured,
    rag_ready: false,
    data_ready: false,
    tenant_set: false,
    missing: arasMissing(configured),
    version,
    endpoints: configured
      ? Object.fromEntries(ARAS_SERVICES.map(([service, , port, , , , prefix]) => [service, `http://127.0.0.1:${port}${prefix}`]))
      : {},
  };
}

function check(name, condition, detail) {
  if (condition) {
    console.log(`  ok   ${name}`);
  } else {
    console.log(`  FAIL ${name}${detail ? ` → ${detail}` : ''}`);
    failures.push(name);
  }
}

function mount(fetchImpl, pageUrl, seed) {
  const dom = new JSDOM(HTML, { url: pageUrl, pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window;
  w.fetch = fetchImpl;
  w.navigator.clipboard = { writeText: async () => {} };
  // A second window with the remembered session id = the page after a reload.
  Object.entries(seed || {}).forEach(([key, value]) => w.localStorage.setItem(key, value));
  w.eval(JS);
  return w;
}

const text = (w, id) => (w.document.getElementById(id) || {}).textContent || '';
const bannerShown = (w) => w.document.getElementById('conn-banner').classList.contains('show');
const modes = (w) => w.document.querySelectorAll('.mode-btn').length;
const MODE_COUNT = 4; // auto, agent, answer, search
const attachButton = (w) => w.document.getElementById('btn-attach');

async function scenarioStaticHost() {
  console.log('B  static host (the "buttons do nothing" case)');
  const notFound = async () => ({
    ok: false,
    status: 404,
    json: async () => ({ detail: 'Not Found' }),
  });
  const w = mount(notFound, 'http://localhost:8000/');
  await sleep(900);

  check('page still renders its modes', modes(w) === MODE_COUNT, `${modes(w)} mode buttons`);
  check('badges say offline, not "ok"', /offline|unknown|down/.test(text(w, 'badge-llm')), text(w, 'badge-llm'));
  check('red banner explains the failure', bannerShown(w), 'banner hidden');
  check(
    'banner lists the probed URLs',
    /health/.test(text(w, 'conn-banner-text')) && /404/.test(text(w, 'conn-banner-text')),
    text(w, 'conn-banner-text').slice(0, 80)
  );

  w.document.getElementById('prompt').value = 'hi';
  w.document.getElementById('btn-send').click();
  await sleep(500);
  const answer = w.document.querySelector('.message.assistant .msg-text');
  check('sending fails with a readable error, not a silent no-op', !!answer && /404/.test(answer.textContent || ''));
  check(
    'the error names the requested path and the hint',
    !!answer && /requested:/.test(answer.textContent) && /hint:/.test(answer.textContent),
    answer ? answer.textContent.slice(0, 100) : 'no answer bubble'
  );
}

async function scenarioApiPrefix() {
  console.log('C  backend reachable only under /api (proxy case)');
  const notFound = async () => ({
    ok: false,
    status: 404,
    json: async () => ({ detail: 'Not Found' }),
  });
  const apiOnly = (target, init) => {
    const raw = String(target);
    const asApi = raw.startsWith('/api') || raw.startsWith('api');
    return asApi ? fetch(new URL(raw, liveBase + '/').href, init) : notFound();
  };
  const w = mount(apiOnly, 'http://localhost:8000/');
  await sleep(1400);

  check('negotiation found the prefixed API', !/offline|backend down|checking/.test(text(w, 'badge-llm')) && !bannerShown(w), text(w, 'badge-llm'));
  check('the ARAS services loaded through the prefix', w.document.querySelectorAll('#search-list .provider-row').length === ARAS_SERVICES.length,
    `${w.document.querySelectorAll('#search-list .provider-row').length} rows`);
}

async function scenarioNormalHost() {
  console.log('A  normal host (UI + API from one process)');
  const w = mount((target, init) => fetch(new URL(String(target), liveBase + '/').href, init), liveBase + '/');
  await sleep(1200);

  check('model badge resolved', /^MODEL/.test(text(w, 'badge-llm')) && !/backend down|offline|checking/.test(text(w, 'badge-llm')), text(w, 'badge-llm'));
  check('no error banner', !bannerShown(w));
  check('modes rendered', modes(w) === MODE_COUNT, `${modes(w)} mode buttons`);
  check('auto mode is first and default', (w.document.querySelector('.mode-btn.active') || {}).dataset?.mode === 'auto');
  check('attachment control is present', !!attachButton(w));
  check('orchestrator panel is online', /planner/.test(text(w, 'agents-summary')));
  check('the seven ARAS services are listed', w.document.querySelectorAll('#search-list .provider-row').length === ARAS_SERVICES.length,
    `${w.document.querySelectorAll('#search-list .provider-row').length} rows`);
  check('the list names the router and the web service',
    !!w.document.querySelector('#search-list .provider-row[data-provider="router"]') &&
    !!w.document.querySelector('#search-list .provider-row[data-provider="web"]'));

  w.document.getElementById('prompt').value = 'hello';
  w.document.getElementById('btn-send').click();
  await sleep(1200);
  const answer = w.document.querySelector('.message.assistant .msg-text');
  const body = answer ? answer.textContent : '';
  check('a prompt produces an answer bubble', !!answer);
  check('the reply is a real answer or a configuration error — never Not Found', !!answer && !/Not Found/.test(body), body.slice(0, 100));
  const orchPanel = w.document.querySelector('.orch-panel');
  const hadOrch = !!orchPanel;
  const traceRows = w.document.querySelectorAll('.trace-row');
  check('auto mode renders the step-by-step trace', !!orchPanel && traceRows.length > 0, `${traceRows.length} rows`);
  check('the trace names the manager and the planner',
    /manager/i.test(orchPanel ? orchPanel.textContent : '') && /planner/i.test(orchPanel ? orchPanel.textContent : ''));
  const expandAll = w.document.querySelector('.orch-toolbar .ghost-btn');
  if (expandAll) {
    expandAll.click();
    check('expand all opens every step', w.document.querySelectorAll('.trace-row.open').length === traceRows.length);
  }
  check('the run log panel is populated', w.document.querySelectorAll('#runs-list .run-row').length > 0);
  check(
    'the turn was written to the harness memory',
    !!w.document.querySelector('#sessions-list .session-item'),
    text(w, 'sessions-summary')
  );
  const pill = text(w, 'session-pill');
  check('the composer names the active session', /memory: \d+ turn/.test(pill), pill);

  // ── sidebar: one Runs panel with two tabs, diagnostics only in the top bar
  check('the sidebar owns no diagnostics panel', !w.document.getElementById('btn-diag'));
  check('the top bar still has the diagnostics button', !!w.document.getElementById('btn-diag-top'));
  const savedTab = w.document.querySelector('#runs-tabs .seg-btn[data-runs="saved"]');
  const localTab = w.document.querySelector('#runs-tabs .seg-btn[data-runs="local"]');
  const runsList = w.document.getElementById('runs-list');
  const traceList = w.document.getElementById('trace-list');
  check('the runs panel has both tabs', !!savedTab && !!localTab);
  check('saved runs are shown by default', !runsList.hidden && traceList.hidden);
  localTab.click();
  await sleep(60);
  check('"This tab" shows the local history', runsList.hidden && !traceList.hidden);
  check('the local tab lists this tab\'s run', w.document.querySelectorAll('#trace-list li').length > 0);
  savedTab.click();
  await sleep(200);
  check('"Saved" switches back to the server log', !runsList.hidden && traceList.hidden);

  // diagnostics still work, from the top bar, into the inspector
  w.document.getElementById('btn-diag-top').click();
  await sleep(1200);
  check('diagnostics produce rows', w.document.querySelectorAll('#diag-summary .diag-row').length > 0);
  check('the diagnostics block is visible in the inspector', w.document.getElementById('diag-summary').hidden === false);
  check(
    'the diagnostics block sits inside the inspector',
    !!w.document.querySelector('#inspector #diag-summary')
  );

  // ── navbar dropdowns + ARAS wiring panel (v0.18.0)
  console.log('');
  console.log('C2  navbar dropdowns, ARAS wiring panel, copy buttons');
  const statusMenu = w.document.getElementById('menu-status');
  const toolsMenu = w.document.getElementById('menu-tools');
  check('the top bar has a Status dropdown', !!w.document.getElementById('btn-menu-status'));
  check('the top bar has a Tools dropdown', !!w.document.getElementById('btn-menu-tools'));
  w.document.getElementById('btn-menu-status').click();
  check('clicking Status opens its dropdown', statusMenu.classList.contains('open'));
  check(
    'the status pills live inside the Status dropdown',
    !!statusMenu.querySelector('#pill-llm') && !!statusMenu.querySelector('#pill-search') &&
    !!statusMenu.querySelector('#pill-aras') && !!statusMenu.querySelector('#pill-diag') &&
    !!statusMenu.querySelector('#pill-audit')
  );
  w.document.getElementById('btn-menu-tools').click();
  check('opening Tools closes Status', toolsMenu.classList.contains('open') && !statusMenu.classList.contains('open'));
  check(
    'Tools holds ARAS services / ARAS wiring / diagnostics / evaluate',
    ['btn-providers', 'btn-aras-top', 'btn-diag-top', 'btn-audit-top']
      .every((id) => !!toolsMenu.querySelector('#' + id))
  );

  w.document.getElementById('btn-aras-top').click();
  await sleep(900);
  check('choosing a tool closes the dropdown', !toolsMenu.classList.contains('open'));
  check(
    'the ARAS wiring modal opens',
    !w.document.getElementById('aras-modal').classList.contains('panel-hidden')
  );
  const arasBody = w.document.getElementById('aras-body').textContent;
  check('the wiring report rendered', /ARAS|router :8101|configured/i.test(arasBody), arasBody.slice(0, 70));
  check('the ARAS pill reports the check', !/not checked/.test(text(w, 'badge-aras')), text(w, 'badge-aras'));
  check('the ARAS panel has its copy button', !!w.document.getElementById('btn-aras-copy'));
  check('the ARAS panel has a proof-run button', !!w.document.getElementById('btn-aras-proof'));
  w.document.getElementById('btn-aras-close').click();
  check(
    'the ARAS modal closes',
    w.document.getElementById('aras-modal').classList.contains('panel-hidden')
  );
  check(
    'the diagnostics report carries a copy button',
    !!w.document.querySelector('#diag-summary .diag-copy-row button')
  );
  check('the ARAS services modal has a copy button', !!w.document.getElementById('btn-providers-copy'));

  // ── D: reload. A brand-new window with the same remembered session id
  // must redraw the conversation exactly as it was, with no model call.
  const match = /s-\d{8}-\d{6}-[0-9a-f]+/.exec(pill);
  if (!match) {
    check('a session id is available to reload', false, pill);
    return;
  }
  console.log('');
  console.log('D  reload: the same conversation, replayed from memory');
  const w2 = mount(
    (target, init) => fetch(new URL(String(target), liveBase + '/').href, init),
    liveBase + '/',
    { 'agetnic.session.v1': match[0] }
  );
  await sleep(1800);
  const userBubble = w2.document.querySelector('.message.user .msg-text');
  check('the user turn is back', !!userBubble && /hello/.test(userBubble.textContent || ''), userBubble ? userBubble.textContent : 'none');
  check('the assistant turn is back', !!w2.document.querySelector('.message.assistant .msg-text'));
  check(
    'the restored session is the active one',
    !!w2.document.querySelector('#sessions-list .session-item.selected')
  );
  if (hadOrch) {
    check('the step-by-step trace survives the reload', w2.document.querySelectorAll('.trace-row').length > 0);
    check(
      'the trace is the stored one, not a new run',
      !!w2.document.querySelector('.orch-runid')
    );
  }
  const w3 = mount(
    (target, init) => fetch(new URL(String(target), liveBase + '/').href, init),
    liveBase + '/'
  );
  await sleep(600);
  check('a fresh browser starts with an empty conversation', !w3.document.querySelector('.message'));
}

/**
 * E: the frame. No layout engine in jsdom, but the cascade is real, so this
 * proves each rule reaches the element it was written for — which is exactly how
 * the "inspector grows past the screen" bug came back if a selector is renamed.
 */
async function scenarioLayout() {
  console.log('E  frame: every panel scrolls inside the screen');
  const notFound = async () => ({ ok: false, status: 404, json: async () => ({ detail: 'Not Found' }) });
  const w = mount(notFound, 'http://localhost:8000/');
  const style = w.document.createElement('style');
  style.textContent = CSS;
  w.document.head.appendChild(style);
  await sleep(80);

  const css = (selector, prop) => {
    const node = w.document.querySelector(selector);
    return node ? w.getComputedStyle(node)[prop] : `(no ${selector})`;
  };

  check('the page itself never scrolls', css('body', 'overflow') === 'hidden', css('body', 'overflow'));
  const shell = css('.app-shell', 'height');
  check('the shell fills the viewport', shell === '100%', shell);
  check('the workspace clips its columns', css('.workspace', 'overflow') === 'hidden', css('.workspace', 'overflow'));
  check('the workspace keeps a flexible middle column', /minmax\(0/.test(w.getComputedStyle(w.document.querySelector('.workspace')).gridTemplateColumns || '') || /minmax\(0, 1fr\)/.test(CSS));
  check('the shell no longer guesses the header height', !/calc\(100vh/.test(CSS));
  check('the inspector may shrink below its content', css('.inspector', 'minHeight') === '0px', css('.inspector', 'minHeight'));
  check('the inspector clips and its JSON scrolls',
    css('.inspector', 'overflow') === 'hidden' && css('.json-view', 'overflow') === 'auto',
    `${css('.inspector', 'overflow')} / ${css('.json-view', 'overflow')}`);
  check('the JSON keeps a scroll area', css('.json-view', 'minHeight') === '0px', css('.json-view', 'minHeight'));
  check('the chat column scrolls on its own',
    css('.chat-scroll', 'overflow') === 'auto' && css('.chat-scroll', 'minHeight') === '0px',
    `${css('.chat-scroll', 'overflow')} / ${css('.chat-scroll', 'minHeight')}`);
  check('the sidebar scrolls on its own', css('.sidebar', 'overflowY') === 'auto', css('.sidebar', 'overflowY'));

  const panels = w.document.querySelectorAll('.sidebar section');
  check('the sidebar is five tidy panels', panels.length === 5, `${panels.length} panels`);
  check('the sidebar keeps runs, memory, agents and search', ['section-sessions', 'section-runs', 'section-agents', 'section-search']
    .every((id) => !!w.document.getElementById(id)));
  check('the sidebar carries no prose', w.document.querySelectorAll('.sidebar p').length === 0);
  check('no diagnostics panel in the sidebar', !w.document.getElementById('btn-diag'));
}


/* ── F: the rich answer surface (stub backend) ───────────────────────────
   The redesign turns the answer bubble into a reading surface: headings,
   tables, lists, callouts, code blocks and inline formatting, with the
   orchestration panel and the inspector timeline underneath. The live
   backend cannot answer without the ARAS router, so this scenario stubs
   /orchestrate. */
const MARKDOWN_ANSWER = [
  '## Findings',
  '',
  'The manager split this into **three** tasks and ran them in _parallel waves_.',
  '',
  '| Area | Result |',
  '| --- | --- |',
  '| Planning | 3 agents, 2 waves |',
  '| Search | ARAS web :8108 |',
  '',
  '- The planner wrote a brief per agent.',
  '- Agents ran with tools.',
  '',
  '> Guardrail: no router means no answer, never an invented one.',
  '',
  '```python',
  'result = await orchestrate(message="hi")',
  '```',
  '',
  'Inline `code` and a [source](https://example.com/doc).',
].join('\n');

function richPayload() {
  return {
    ok: true,
    answer: MARKDOWN_ANSWER,
    sources: ['https://example.com/doc'],
    latency_ms: 2450,
    provider: 'aras',
    model: ARAS_ANSWERS_AS,
    session: { session_id: 'sess-rich', turns: 1 },
    orchestration: {
      run_id: 'run-rich', provider: 'aras', model: ARAS_ANSWERS_AS, strategy: 'heuristic',
      model_calls: 5, parallel_ms: 1800, latency_ms: 2450, synthesized: true,
      log_path: 'logs/orchestrator/run-rich.json',
      plan: {
        intent: 'research', strategy: 'heuristic', detail_source: 'llm',
        summary: 'Manager picked two specialists.',
        agents: ['search', 'document'],
        steps: [
          { agent: 'search', label: 'Search agent', wave: 1, task: 'find sources' },
          { agent: 'document', label: 'Document agent', wave: 1, task: 'extract facts' },
        ],
      },
      waves: [{ wave: 1, agents: ['search', 'document'] }],
      evaluation: {
        requested: true, ran: true, method: 'model', rounds: 1,
        summary: 'the search report buries its answer', gaps: ['no publication date'],
        quality: 4.0, revised: 1, failed: 0, model_calls: 1, latency_ms: 700,
        items: [
          { agent: 'search', label: 'Search agent', verdict: 'revise', score: 4,
            issues: ['buries the answer'], revised: true, usable: true },
          { agent: 'document', label: 'Document agent', verdict: 'keep', score: 5,
            issues: [], revised: false, usable: true },
        ],
      },
      trace: [
        { seq: 1, stage: 'manager', actor: 'manager', title: 'Manager chose 2 specialists', offset_ms: 0, detail: { note: 'planner=llm' } },
        { seq: 2, stage: 'tool', actor: 'search', title: 'web_search', offset_ms: 300, detail: { query: 'orchestration', latency_ms: 620, output: { count: 5 } } },
        { seq: 3, stage: 'evaluate', actor: 'evaluator', offset_ms: 1900,
          title: 'Evaluator reviewed 2 report(s): quality 4/5, 1 rewritten',
          detail: { round: 1, method: 'model', quality: 4, summary: 'the search report buries its answer',
                    gaps: ['no publication date'], revised: 1,
                    items: [{ agent: 'search', label: 'Search agent', verdict: 'revise', score: 4,
                              issues: ['buries the answer'], revised: true }] } },
        { seq: 4, stage: 'synthesis', actor: 'manager', title: 'Synthesized final answer', offset_ms: 2400, detail: { latency_ms: 1500 } },
      ],
      review: { asked: true, note: 'enough evidence', follow_up: [], rounds: 1 },
    },
  };
}

async function scenarioRichAnswer() {
  console.log('F  rich answer rendering (stub backend)');
  const routes = {
    '/health': arasHealth('0.19.0'),
    '/providers': arasProviders('0.19.0'),
    '/agents': { planner: 'llm', synthesize: true, parallel: 4, roster: ['search'], agents: [] },
    '/runs': { runs: [] },
    '/sessions': { sessions: [] },
    '/orchestrate': richPayload(),
    '/chat': {
      ok: true,
      answer: 'Two steps: one model turn and one tool call.',
      provider: 'aras', model: ARAS_ANSWERS_AS, search_provider: 'aras', latency_ms: 900, steps: 2,
      sources: ['https://example.com/a'],
      trace: [
        { type: 'model', provider: 'aras', model: ARAS_ANSWERS_AS, latency_ms: 400,
          content: 'I should search for this.',
          tool_calls: [{ name: 'web_search', arguments: { query: 'orchestration' } }] },
        { type: 'tool', name: 'web_search', latency_ms: 500, ok: true,
          arguments: { query: 'orchestration' }, preview: { count: 3 } },
      ],
    },
  };
  const stub = async (target) => {
    let clean = String(target).replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    if (!clean.startsWith('/')) clean = '/' + clean;
    if (clean === '/orchestrate/stream') {
      // the same turn, as frames — and without a readable body, so the page
      // takes its "no streaming reader" path and reads the whole text at once
      const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
      const body =
        frame('open', { run_id: 'run-rich' }) +
        frame('thought', liveThought(1, 'manager', 'manager', 'Manager', 'status', 'Request received', 'Request received')) +
        frame('result', richPayload());
      return { ok: true, status: 200, json: async () => ({}), text: async () => body };
    }
    const body = routes[clean] || {};
    return { ok: true, status: 200, json: async () => body };
  };
  const w = mount(stub, 'http://localhost:8000/');
  await sleep(900);
  w.document.getElementById('prompt').value = 'research orchestration';
  w.document.getElementById('btn-send').click();
  await sleep(600);

  const bubble = w.document.querySelector('.message.assistant .msg-text');
  const q = (sel) => (bubble ? !!bubble.querySelector(sel) : false);
  check('the answer bubble is a markdown surface', !!bubble && bubble.classList.contains('markdown-body'));
  check('headings render', q('h2'));
  check('bold and italic render', q('strong') && q('em'));
  check('tables render as a scrollable table', q('.table-wrap table.markdown-table'));
  check('lists render', (bubble ? bubble.querySelectorAll('ul li').length : 0) === 2);
  check('callouts render', q('.callout'));
  check('fenced code keeps its language', q('.code-block pre code'));
  check('inline code and links render', q('p code') && q('a[href]'));
  check('sources become chips', w.document.querySelectorAll('.sources .source-link').length === 1);
  check('the orchestration panel is attached', !!w.document.querySelector('.orch-panel'));
  check('the plan lists one chip per agent', w.document.querySelectorAll('.plan-agents .agent-chip').length === 2);
  check('the trace rows keep every event', w.document.querySelectorAll('.orch-panel .trace-row').length === 4);

  // the evaluator gate is visible: a head chip, a note, a per-agent verdict
  const head = w.document.querySelector('.orch-panel .orch-head');
  check('the panel reports the evaluator', /evaluator 4\/5/.test(head ? head.textContent : ''));
  check('...and how many reports it rewrote', /1 revised/.test(head ? head.textContent : ''));
  const evalNote = w.document.querySelector('.orch-panel .eval-note');
  check('the evaluator note names the problem', !!evalNote && /buries its answer/.test(evalNote.textContent));
  check('the evaluator note lists the gaps', !!evalNote && /no publication date/.test(evalNote.textContent));
  check('the evaluator note names the rewritten report', !!evalNote && /Search agent/.test(evalNote.textContent));
  check('the agent chip carries its verdict',
    !!w.document.querySelector('.plan-agents .agent-chip .verdict'));
  check('a revised report is marked on its chip',
    /revised/.test((w.document.querySelector('.plan-agents .agent-chip .verdict') || {}).textContent || ''));
  const evalRow = [...w.document.querySelectorAll('.orch-panel .trace-row')]
    .find((row) => /evaluator/.test(row.textContent));
  check('the evaluator has its own trace row', !!evalRow);
  check('...with the per-report verdict inside',
    !!(evalRow && evalRow.querySelector('.eval-card')));
  check('...and the gaps it found',
    !!(evalRow && /no publication date/.test(evalRow.textContent)));
  check('the answer carries a meta line and actions',
    !!w.document.querySelector('.answer-meta') && w.document.querySelectorAll('.message-actions button').length >= 2);

  check('the inspector shows a run summary', !!w.document.querySelector('#inspector-trace .run-summary'));
  check('the inspector shows a numbered timeline', w.document.querySelectorAll('#inspector-trace .trace-item').length === 4);
  check('the inspector shows an evaluation section',
    /evaluator \/5/.test((w.document.querySelector('#inspector-trace .run-summary') || {}).textContent || ''));
  check('the evaluation section lists both reports',
    w.document.querySelectorAll('#inspector-trace .eval-card').length === 2);
  check('the inspector shows tool activity',
    w.document.querySelectorAll('#inspector-trace .trace-card').length >= 1);
  check('the context tab lists the run context', w.document.querySelectorAll('#inspector-context .context-item').length > 5);

  w.document.querySelector('.inspector-tab[data-tab="json"]').click();
  const json = w.document.getElementById('trace-json');
  check('the JSON tab shows the payload', !json.hidden && /run-rich/.test(json.textContent));
  check('opening a tab hides the others', w.document.getElementById('inspector-trace').hidden);
  w.document.querySelector('.inspector-tab[data-tab="context"]').click();
  check('the context tab becomes visible', !w.document.getElementById('inspector-context').hidden);

  w.document.getElementById('btn-close-inspector').click();
  check('closing the inspector collapses the column',
    w.document.querySelector('.workspace').classList.contains('inspector-collapsed'));
  w.document.getElementById('btn-toggle-inspector').click();
  check('the toggle brings it back',
    !w.document.querySelector('.workspace').classList.contains('inspector-collapsed'));

  // Agent mode: the single-model trace keeps its steps and tool payloads.
  w.document.querySelector('.mode-btn[data-mode="agent"]').click();
  w.document.getElementById('prompt').value = 'search for orchestration';
  w.document.getElementById('btn-send').click();
  await sleep(600);
  const lastPanel = [...w.document.querySelectorAll('.orch-panel')].pop();
  check('agent mode draws its own trace panel', !!lastPanel);
  check('agent mode keeps one row per step', lastPanel ? lastPanel.querySelectorAll('.trace-row').length === 2 : false);
  check('a tool step can be expanded to its result',
    !!(lastPanel && lastPanel.querySelector('.trace-json-block pre')));
  check('the tool step lists its arguments',
    !!(lastPanel && /orchestration/.test(lastPanel.textContent)));
}


/* ── G: light/dark switch and the resizable columns ─────────────────────
   Both are page-level features with no backend: a stored preference, an
   inline style on .workspace and a value in localStorage. jsdom cannot
   paint, so this checks the state machine and the CSS contract (theme.css
   must only ever target html[data-theme="light"]). */
async function scenarioThemeAndColumns() {
  console.log('G  theme switch + resizable columns');
  const stub = async (target) => {
    let clean = String(target).replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    if (!clean.startsWith('/')) clean = '/' + clean;
    const routes = {
      '/health': arasHealth('0.19.0'),
      '/providers': arasProviders('0.19.0'),
      '/agents': { planner: 'llm', synthesize: true, parallel: 4, roster: [], agents: [] },
      '/runs': { runs: [] },
      '/sessions': { sessions: [] },
    };
    const body = routes[clean] || {};
    return { ok: true, status: 200, json: async () => body };
  };
  const w = mount(stub, 'http://localhost:8000/');
  await sleep(700);

  // ── theme
  const root = w.document.documentElement;
  const toggle = w.document.getElementById('btn-theme');
  check('the theme switch is in the top bar', !!toggle);
  check('dark is the default', root.dataset.theme === 'dark', root.dataset.theme);
  check('the switch reports the current mode', toggle.getAttribute('aria-pressed') === 'false');
  toggle.click();
  check('clicking it turns the page light', root.dataset.theme === 'light', root.dataset.theme);
  check('the switch is now pressed', toggle.getAttribute('aria-pressed') === 'true');
  check('the choice is remembered', w.localStorage.getItem('agetnic.theme.v1') === 'light');
  check('the theme-color meta follows', w.document.querySelector('meta[name="theme-color"]').content === '#f2f5fb');
  toggle.click();
  check('clicking again returns to dark', root.dataset.theme === 'dark');

  const reloaded = mount(stub, 'http://localhost:8000/', { 'agetnic.theme.v1': 'light' });
  await sleep(600);
  check('a stored light theme is applied on load',
    reloaded.document.documentElement.dataset.theme === 'light');
  check('...and the switch reflects it',
    reloaded.document.getElementById('btn-theme').getAttribute('aria-pressed') === 'true');

  check('the theme stylesheet is linked', /assets\/css\/theme\.css/.test(HTML));
  const lightSelectors = THEME_CSS.split('}')
    .map((chunk) => chunk.split('{')[0].trim())
    .filter((sel) => sel && !sel.startsWith('/*') && !sel.startsWith('*'));
  const unprefixed = lightSelectors.filter((sel) => !sel.startsWith('html[data-theme="light"]'));
  check('every light rule is scoped to the light theme', unprefixed.length === 0, unprefixed.slice(0, 3).join(' | '));
  check('the light palette redefines the tokens', /html\[data-theme="light"\][^{]*\{[^}]*--bg:\s*#f2f5fb/.test(THEME_CSS));

  // ── columns
  const ws = w.document.getElementById('workspace');
  const gutters = [...w.document.querySelectorAll('.gutter')];
  check('both column edges have a handle', gutters.length === 2);
  check('the handles are separators with a range',
    gutters.every((g) => g.getAttribute('role') === 'separator'
      && g.getAttribute('aria-valuemin') && g.getAttribute('aria-valuemax')));
  check('the grid reserves a gutter column',
    /--gutter-width/.test(CSS) && /var\(--sidebar-width\) var\(--gutter-width\)/.test(CSS));

  const left = w.document.getElementById('gutter-left');
  const before = Number(left.getAttribute('aria-valuenow'));
  left.dispatchEvent(new w.MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 200, clientY: 300 }));
  const move = new w.Event('pointermove', { bubbles: true });
  move.clientX = 360;
  move.clientY = 300;
  w.document.dispatchEvent(move);
  w.document.dispatchEvent(new w.Event('pointerup', { bubbles: true }));
  check('dragging the edge sets a width', /\d+px/.test(ws.style.getPropertyValue('--sidebar-width')),
    ws.style.getPropertyValue('--sidebar-width'));
  check('the width is remembered',
    /sidebar/.test(w.localStorage.getItem('agetnic.layout.v1') || ''));
  check('the handle reports the new width',
    Number(left.getAttribute('aria-valuenow')) === parseInt(ws.style.getPropertyValue('--sidebar-width'), 10));

  left.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
  const afterKey = parseInt(ws.style.getPropertyValue('--sidebar-width'), 10);
  check('the arrow keys nudge the column', afterKey < Number(left.getAttribute('aria-valuenow')) + 1 && afterKey >= 248,
    `${afterKey}px`);

  left.dispatchEvent(new w.MouseEvent('dblclick', { bubbles: true }));
  check('a double click resets to the design width',
    ws.style.getPropertyValue('--sidebar-width') === '');

  const right = w.document.getElementById('gutter-right');
  right.dispatchEvent(new w.MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 900, clientY: 300 }));
  const move2 = new w.Event('pointermove', { bubbles: true });
  move2.clientX = 700;
  move2.clientY = 300;
  w.document.dispatchEvent(move2);
  w.document.dispatchEvent(new w.Event('pointerup', { bubbles: true }));
  check('the inspector edge resizes too',
    /\d+px/.test(ws.style.getPropertyValue('--inspector-width')),
    ws.style.getPropertyValue('--inspector-width'));

  const saved = mount(stub, 'http://localhost:8000/', {
    'agetnic.layout.v1': JSON.stringify({ sidebar: 350, inspector: 430 }),
  });
  await sleep(600);
  const ws2 = saved.document.getElementById('workspace');
  check('saved widths are restored on load',
    ws2.style.getPropertyValue('--sidebar-width') === '350px'
      && ws2.style.getPropertyValue('--inspector-width') === '430px',
    `${ws2.style.getPropertyValue('--sidebar-width')} / ${ws2.style.getPropertyValue('--inspector-width')}`);
}

/* ── H: live thoughts ────────────────────────────────────────────────────
   Auto mode no longer waits for one JSON body: it reads
   POST /orchestrate/stream as server-sent events and draws the run while it
   happens — a group per actor, a spinner on whoever is working, every line
   opening to its real text. The stream here is stubbed, but the frames are
   the ones app/orchestrator/live.py produces, and they are delivered in
   pieces (one frame split down the middle) so the page has to reassemble. */
function liveThought(seq, category, group, label, kind, title, text, detail) {
  return {
    seq, stage: category, actor: group.replace('agent:', ''), category, group, label, kind, title, text,
    offset_ms: seq * 700, at: '2026-11-04T09:00:00.000Z', detail: detail || {},
  };
}

function liveStream() {
  const frames = [];
  const push = (event, data) => frames.push(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  push('open', { run_id: 'run-live-1', threshold_ms: 0 });
  push('thought', liveThought(1, 'manager', 'manager', 'Manager', 'status', 'Request received', 'Request received'));
  push('thought', liveThought(2, 'planner', 'planner', 'Planner', 'note', 'Selected the agents by rule', 'Selected the agents by rule', { agents: ['search', 'data'] }));
  push('thought', liveThought(3, 'agent', 'agent:search', 'Web researcher', 'prompt', 'Prompt sent to the model (step 1)',
    'Find current, verifiable information for: what changed in the latest release.'));
  push('thought', liveThought(4, 'agent', 'agent:search', 'Web researcher', 'tool', 'web_search', 'web_search · latest python release', { tool: 'web_search', query: 'latest python release', latency_ms: 812 }));
  push('thought', liveThought(5, 'agent', 'agent:search', 'Web researcher', 'reply', 'Model reply (step 1)',
    'The release notes put the answer at 3.14: nothing breaking, but the REPL got faster.'));
  push('thought', liveThought(6, 'agent', 'agent:data', 'Data analyst', 'status', 'Data analyst finished: ok', 'Data analyst finished: ok', { status: 'ok', latency_ms: 640 }));
  push('thought', liveThought(7, 'evaluator', 'evaluator', 'Evaluator', 'verdict', 'Evaluator reviewed 2 report(s)',
    'one report was thin — Web researcher 4/5, Data analyst 3/5 · gaps: the exact date', { quality: 0.8, round: 1, items: [
      { agent: 'search', label: 'Web researcher', score: 4, verdict: 'keep' },
      { agent: 'data', label: 'Data analyst', score: 3, verdict: 'revise' },
    ], gaps: ['the exact date'] }));
  push('thought', liveThought(8, 'synthesis', 'manager', 'Manager · writing', 'status', 'Final answer', 'Wrote the answer (812 chars)'));
  push('result', {
    answer: 'Python 3.14 is the current release — nothing breaking, and the REPL got faster.',
    mode: 'auto', run_id: 'run-live-1', message: 'what changed in the latest release?',
    orchestration: { run_id: 'run-live-1', plan: { agents: ['search', 'data'], summary: 'two agents' }, agents: [], trace: [], evaluation: {} },
    latency_ms: 4200,
    session: { session_id: 's-live-0001', turns: 1, stored: true },
  });
  const text = frames.join('');
  // deliver it in three reads, splitting frame 7 down the middle
  return [text.slice(0, 400), text.slice(400, 1400), text.slice(1400)].filter(Boolean);
}

async function scenarioLiveThoughts() {
  console.log('H  live thoughts (the run, drawn while it happens)');
  const pieces = liveStream();
  let askedPath = '';
  let askedBody = null;
  const stub = async (target, init) => {
    let clean = String(target).replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    if (!clean.startsWith('/')) clean = '/' + clean;
    if (clean === '/orchestrate/stream') {
      askedPath = clean;
      askedBody = init && init.body ? JSON.parse(init.body) : null;
      let index = 0;
      return {
        ok: true,
        status: 200,
        headers: { get: () => 'text/event-stream' },
        body: {
          getReader: () => ({
            // the first two reads land at once, the last one a beat later
            read: async () => {
              if (index >= pieces.length) return { done: true, value: undefined };
              if (index === pieces.length - 1) await sleep(90);
              return { done: false, value: new TextEncoder().encode(pieces[index++]) };
            },
          }),
        },
        text: async () => pieces.join(''),
      };
    }
    const routes = {
      '/health': arasHealth('0.19.0'),
      '/providers': arasProviders('0.19.0'),
      '/agents': { planner: 'llm', synthesize: true, parallel: 4, roster: ['search'], agents: [] },
      '/runs': { runs: [] },
      '/sessions': { sessions: [] },
    };
    return { ok: true, status: 200, json: async () => routes[clean] || {} };
  };

  const w = mount(stub, 'http://localhost:8000/');
  await sleep(600);
  w.document.getElementById('prompt').value = 'what changed in the latest release?';
  w.document.getElementById('btn-send').click();
  await sleep(40);

  // ── while the run is still going
  const live = w.document.querySelector('.thoughts');
  check('auto mode streams the run', askedPath === '/orchestrate/stream', askedPath || 'no request');
  check('the panel is inside the assistant message', !!live);
  check('the bubble is still empty while it thinks',
    !!live && !(w.document.querySelector('.message.assistant .msg-text').textContent || '').trim());
  check('the panel says it is working', !!live && live.dataset.state === 'live', live ? live.dataset.state : '-');
  check('a waiting spinner is visible', !!w.document.querySelector('.thoughts .thoughts-spinner:not(.off)'));
  const groupsLive = [...w.document.querySelectorAll('.thoughts .thought-group')];
  check('thoughts are grouped by who is thinking', groupsLive.length >= 3, `${groupsLive.length} groups`);
  check('the group that is working shows a live dot',
    groupsLive.some((g) => g.querySelector('.tg-live.on')));

  await sleep(240);

  // ── after the run
  const done = w.document.querySelector('.thoughts');
  check('the panel survives to the end', !!done);
  check('...and is marked done', !!done && done.dataset.state === 'done', done ? done.dataset.state : '-');
  check('the spinner is gone', !!done && !!done.querySelector('.thoughts-spinner.off'));
  check('the header counts the steps', !!done && /Thought through \d+ steps?/.test(done.textContent), done ? done.querySelector('.thoughts-title').textContent : '-');
  check('the finished groups fold away',
    [...done.querySelectorAll('.thought-group')].every((g) => !g.open));
  const labels = [...done.querySelectorAll('.thought-group')].map((g) => (g.querySelector('.tg-label') || {}).textContent);
  check('the planner, the agents, the evaluator and the manager each get a group',
    ['Planner', 'Web researcher', 'Data analyst', 'Evaluator', 'Manager'].every((label) => labels.includes(label)),
    labels.join(' | '));
  const counts = [...done.querySelectorAll('.thought-group .tg-meta')].map((n) => Number(n.textContent));
  check('each folded group keeps its count', counts.every((n) => n >= 1) && counts.length === labels.length, counts.join(','));
  check('the answer is in the bubble, not in the thoughts',
    /Python 3\.14 is the current release/.test(w.document.querySelector('.message.assistant .msg-text').textContent || ''));
  check('the synthesis only says it wrote the answer',
    /Wrote the answer \(812 chars\)/.test(done.textContent)
      && !/Python 3\.14 is the current release[^]*Wrote/.test(done.textContent));
  check('the answer text appears exactly once in the message',
    ((w.document.querySelector('.message.assistant').textContent || '').match(/Python 3\.14 is the current release/g) || []).length === 1);
  check('the assistant message adopts the streamed session', /memory: 1 turn/.test(text(w, 'session-pill')), text(w, 'session-pill'));

  // ── opening a thought: the real text is there
  const agent = [...done.querySelectorAll('.thought-group')].find(
    (g) => (g.querySelector('.tg-label') || {}).textContent === 'Web researcher'
  );
  agent.querySelector('summary').click();
  check('a group opens on click', agent.open);
  const items = [...agent.querySelectorAll('.thought-item')];
  check('the agent kept its prompt, its tool call and its reply',
    items.length === 3, `${items.length} items`);
  const reply = items[items.length - 1];
  check('a thought opens to what was actually said', !!(reply.querySelector('summary') || reply.classList.contains('flat')));
  reply.open = true;
  check('...the model’s own words are inside',
    /the REPL got faster/.test(reply.textContent), reply.textContent.slice(0, 80));
  const tool = items.find((item) => item.dataset.kind === 'tool');
  check('the tool call names the query', !!tool && /latest python release/.test(tool.textContent));
  const evaluator = [...done.querySelectorAll('.thought-group')].find(
    (g) => (g.querySelector('.tg-label') || {}).textContent === 'Evaluator'
  );
  evaluator.querySelector('summary').click();
  const verdict = evaluator.querySelector('.thought-item');
  verdict.open = true;
  check('the evaluator shows a score per report',
    evaluator.querySelectorAll('.ti-score').length === 2,
    `${evaluator.querySelectorAll('.ti-score').length} scores`);
  check('...and the gaps it found', /the exact date/.test(evaluator.textContent));

  // a group the user opened stays open when the next run finishes
  check('the request carried the conversation', !!askedBody && typeof askedBody.message === 'string');
  check('the run is in the tab history', w.document.querySelectorAll('#trace-list li').length > 0);

  // ── the panel's own contract (jsdom paints nothing, so only literals)
  check('the panel is a bounded card, not a full-width log',
    /\.thoughts \{[^}]*max-width: 760px/.test(CSS));
  check('the waiting state is a real animation',
    /@keyframes thought-spin/.test(CSS) && /\.thoughts-spinner \{[^}]*animation: thought-spin/.test(CSS));
  check('the light theme carries the panel too', /html\[data-theme="light"\] \.thoughts\b/.test(THEME_CSS));
  check('reduced motion is respected', /prefers-reduced-motion: reduce[^}]*\}[^]*thoughts-spinner/.test(CSS));
  check('the inspector shows the finished run', !!w.document.querySelector('#inspector-trace'));
}

/* ── I: the ARAS services ────────────────────────────────────────────────
   The page has one model (the ARAS router) and one search (ARAS web). This
   scenario stubs the backend twice — a filled ARAS block and an empty one —
   and checks that the pills, the sidebar list, the services panel, the wiring
   report and the diagnostics all say the same thing, and that nothing on the
   page offers another provider to pick. */
function arasBackend(configured, calls) {
  const version = '0.19.0';
  const unset = {
    error: 'configuration',
    message: '[aras:router] not configured — missing ARAS_BASE_URL, ARAS_ROUTER_TOKEN, ARAS_MODEL',
    detail: {
      provider: 'aras', service: 'router', status: null,
      message: '[aras:router] not configured — missing ARAS_BASE_URL, ARAS_ROUTER_TOKEN, ARAS_MODEL',
      hint: 'set ARAS_BASE_URL (or ARAS_ROUTER_URL), ARAS_ROUTER_TOKEN (or the shared ARAS_TOKEN), ARAS_MODEL in .env, then restart',
      missing: ['ARAS_BASE_URL', 'ARAS_ROUTER_TOKEN', 'ARAS_MODEL'],
    },
  };
  const routes = {
    '/health': arasHealth(version, configured),
    '/providers': arasProviders(version, configured),
    '/aras': arasReport(version, configured),
    '/agents': { planner: 'auto', synthesize: true, parallel: 4, roster: ['search'], agents: [] },
    '/runs': { runs: [] },
    '/sessions': { sessions: [] },
    '/diagnose': configured
      ? {
          version,
          model: { ok: true, provider: 'aras', model: ARAS_ANSWERS_AS, reply: 'pong', latency_ms: 420 },
          search: {
            checked: [{ provider: 'aras', ok: true, count: 5, latency_ms: 310, empty: false }],
            skipped: [], ok: ['aras'], failed: [],
          },
          aras: arasReport(version, true),
          hints: [],
        }
      : {
          version,
          model: { ok: false, provider: 'aras', model: '', latency_ms: 0, error: unset.detail },
          search: {
            checked: [],
            skipped: [{ provider: 'aras', reason: 'not configured — missing ARAS_BASE_URL, ARAS_WEB_TOKEN' }],
            ok: [], failed: [],
          },
          aras: null,
          hints: ['The model is not configured: set ARAS_BASE_URL, ARAS_ROUTER_TOKEN, ARAS_MODEL in .env.'],
        },
    '/search': {
      query: 'aras', provider: 'aras', count: 1, latency_ms: 210,
      results: [{ title: 'ARAS', url: 'https://example.com/aras', snippet: 'one hit', source: 'aras:bing' }],
    },
    // What the server really sends for the proof run: it asks for no
    // synthesis, so the run names no model — only the agent that answered does.
    '/orchestrate': {
      answer: 'ok', provider: '', model: '', run_id: 'run-proof',
      orchestration: {
        run_id: 'run-proof', plan: { agents: ['general'], steps: [] }, waves: [], trace: [],
        agents: [{ agent: 'general', status: 'ok', provider: 'aras', model: ARAS_ANSWERS_AS, latency_ms: 380 }],
      },
    },
  };
  return async (target, init) => {
    let clean = String(target).replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    if (!clean.startsWith('/')) clean = '/' + clean;
    calls.push({ path: clean, body: init && init.body ? JSON.parse(init.body) : null });
    if (!configured && (clean === '/chat' || clean === '/search')) {
      return { ok: false, status: 400, json: async () => unset };
    }
    return { ok: true, status: 200, json: async () => routes[clean] || {} };
  };
}

async function scenarioArasServices() {
  console.log('I  ARAS services: one model, one search, seven services');
  const rowsOf = (w) => [...w.document.querySelectorAll('#search-list .provider-row')];
  const row = (w, name) => w.document.querySelector(`#search-list .provider-row[data-provider="${name}"]`);
  const pillHas = (w, id, cls) => w.document.getElementById(id).classList.contains(cls);

  // ── a filled ARAS block
  const calls = [];
  const w = mount(arasBackend(true, calls), 'http://localhost:8000/');
  await sleep(700);

  check('the model pill names the router model', text(w, 'badge-llm').includes(ARAS_MODEL), text(w, 'badge-llm'));
  check('...and is green', pillHas(w, 'pill-llm', 'success'));
  check('the search pill names the ARAS web service', /ARAS web/.test(text(w, 'badge-search')), text(w, 'badge-search'));
  check('the health card shows the model', text(w, 'health-model').includes(ARAS_MODEL), text(w, 'health-model'));
  check('the sidebar section is the ARAS services',
    /ARAS services/.test(w.document.querySelector('#section-search .section-label').textContent));
  check('all seven services are listed', rowsOf(w).length === 7, `${rowsOf(w).length} rows`);
  check('the list is in the backend order',
    rowsOf(w).map((node) => node.dataset.provider).join(',') === 'router,web,ocr,retrieval,ingestion,data,vector',
    rowsOf(w).map((node) => node.dataset.provider).join(','));
  check('each row carries its port', /:8101/.test(row(w, 'router').textContent) && /:8108/.test(row(w, 'web').textContent));
  check('a ready service has a filled dot', row(w, 'router').classList.contains('selected') && row(w, 'web').classList.contains('selected'));
  check('a ready service says what it does', /every planner, agent, evaluator and manager call/.test(row(w, 'router').textContent));
  check('an incomplete service is not marked ready',
    !row(w, 'retrieval').classList.contains('selected') && /incomplete/.test(row(w, 'retrieval').textContent));
  check('...and names the variable it still needs', /set ARAS_COLLECTION/.test(row(w, 'retrieval').textContent),
    row(w, 'retrieval').textContent);
  check('port 8106 is never shown', !/8106/.test(w.document.getElementById('search-list').textContent));
  check('there is no "chain" or provider picker left',
    !/failover|Chain \(|no key|free key/i.test(w.document.getElementById('search-list').textContent));

  // the services panel: opened from a row and from Tools ▾
  row(w, 'router').click();
  await sleep(150);
  const modal = w.document.getElementById('providers-modal');
  check('clicking a service opens the services panel', !modal.classList.contains('panel-hidden'));
  const cards = [...w.document.querySelectorAll('#providers-body .provider-card')];
  check('the panel has one card per service', cards.length === 7, `${cards.length} cards`);
  const groups = [...w.document.querySelectorAll('#providers-body .provider-group-title')].map((n) => n.textContent);
  check('the cards are grouped by what they are for',
    groups.join(' | ') === 'The model | Web search | Documents & data', groups.join(' | '));
  const routerCard = w.document.querySelector('#providers-body .provider-card[data-service="router"]');
  check('the router card shows its endpoint and route',
    /127\.0\.0\.1:8101\/api\/v1/.test(routerCard.textContent) && /chat\/completions/.test(routerCard.textContent));
  check('the router card lists its env vars', /ARAS_ROUTER_TOKEN/.test(routerCard.textContent) && /ARAS_MODEL/.test(routerCard.textContent));
  check('the router card links to the model list',
    [...routerCard.querySelectorAll('.pc-links a')].some((a) => /aras\/models$/.test(a.getAttribute('href'))));
  check('the panel says how many services are ready', /4 of 7 ARAS services ready/.test(w.document.getElementById('providers-body').textContent),
    w.document.getElementById('providers-body').textContent.slice(0, 80));
  check('no card offers an API key or another vendor',
    !/Get API key|free tier|needs key|LLM_PROVIDER/i.test(w.document.getElementById('providers-body').textContent));
  check('the panel title names ARAS', /ARAS services/.test(text(w, 'providers-title')), text(w, 'providers-title'));
  let copied = '';
  w.navigator.clipboard.writeText = async (value) => { copied = value; };
  w.document.getElementById('btn-providers-copy').click();
  await sleep(80);
  check('the copy button carries the services report', /"services"/.test(copied) && /"router"/.test(copied), copied.slice(0, 60));
  w.document.getElementById('btn-providers-close').click();
  check('the panel closes', modal.classList.contains('panel-hidden'));
  w.document.getElementById('btn-providers').click();
  await sleep(120);
  check('Tools ▾ opens the same panel', !modal.classList.contains('panel-hidden') &&
    w.document.querySelectorAll('#providers-body .provider-card').length === 7);
  w.document.getElementById('btn-providers-close').click();

  // the wiring report: config, health, proof
  w.document.getElementById('btn-aras-top').click();
  await sleep(300);
  const arasBody = () => w.document.getElementById('aras-body').textContent;
  check('the wiring report names the router as the model', /router :8101 → the model/.test(arasBody()), arasBody().slice(0, 90));
  check('...and shows the configured model id', arasBody().includes(ARAS_MODEL));
  check('...and what the optional services still need', /set ARAS_COLLECTION/.test(arasBody()) && /set ARAS_TENANT/.test(arasBody()));
  check('the health rows cover all seven services', w.document.querySelectorAll('#aras-body .diag-row').length === 4 + 7,
    `${w.document.querySelectorAll('#aras-body .diag-row').length} rows`);
  check('the ARAS pill counts the services that answered', /7\/7 up/.test(text(w, 'badge-aras')), text(w, 'badge-aras'));
  w.document.getElementById('btn-aras-proof').click();
  await sleep(300);
  check('the proof run names the router and its model',
    arasBody().includes(`answered by the ARAS router (${ARAS_ANSWERS_AS})`), arasBody().slice(-160));
  check('the proof run never mentions a fallback provider', !/fallback provider/i.test(arasBody()));
  const proof = calls.find((call) => call.path === '/orchestrate');
  check('the proof run is one tiny request to the general agent',
    !!proof && proof.body.agents.join() === 'general' && proof.body.remember === false);
  w.document.getElementById('btn-aras-close').click();

  // diagnostics: a model row, a search row, seven health rows
  w.document.getElementById('btn-diag-top').click();
  await sleep(300);
  const diag = w.document.getElementById('diag-summary').textContent;
  check('diagnostics show the router call', /model · ARAS router/.test(diag) && diag.includes(ARAS_ANSWERS_AS), diag.slice(0, 120));
  check('diagnostics show the web search', /search · ARAS web/.test(diag) && /5 hits/.test(diag));
  check('diagnostics show every health probe', (diag.match(/aras · /g) || []).length === 7);
  check('the diagnostics pill is green', /model ok · search ok/.test(text(w, 'badge-diag')) && pillHas(w, 'pill-diag', 'success'), text(w, 'badge-diag'));

  // search mode: one backend, so the request names none
  w.document.querySelector('.mode-btn[data-mode="search"]').click();
  check('search mode says where it searches', /ARAS web search/.test(text(w, 'composer-mode-label')), text(w, 'composer-mode-label'));
  w.document.getElementById('prompt').value = 'aras';
  w.document.getElementById('btn-send').click();
  await sleep(400);
  const searchCall = calls.find((call) => call.path === '/search');
  check('search mode posts /search', !!searchCall);
  check('...with no provider to choose', !!searchCall && !('provider' in searchCall.body), JSON.stringify(searchCall && searchCall.body));
  const hit = [...w.document.querySelectorAll('.message.assistant .msg-text')].pop();
  check('the hits are listed', !!hit && /example\.com\/aras/.test(hit.textContent), hit ? hit.textContent.slice(0, 80) : 'none');

  // ── an empty ARAS block: everything says what to set, nothing pretends
  console.log('');
  console.log('I2 ARAS not configured: every surface names the missing variables');
  const calls2 = [];
  const w2 = mount(arasBackend(false, calls2), 'http://localhost:8000/');
  await sleep(700);
  check('the model pill says not configured', /MODEL\s+not configured/.test(text(w2, 'badge-llm')), text(w2, 'badge-llm'));
  check('...in red', pillHas(w2, 'pill-llm', 'bad'));
  check('...and its tooltip names the variables', /ARAS_ROUTER_TOKEN/.test(w2.document.getElementById('pill-llm').title) &&
    /ARAS_MODEL/.test(w2.document.getElementById('pill-llm').title), w2.document.getElementById('pill-llm').title);
  check('the search pill says not configured', /SEARCH\s+not configured/.test(text(w2, 'badge-search')), text(w2, 'badge-search'));
  check('the Status dot turns red', w2.document.getElementById('menu-status-dot').classList.contains('bad'));
  check('the services are still listed', rowsOf(w2).length === 7);
  check('no service is marked ready', rowsOf(w2).every((node) => !node.classList.contains('selected')));
  check('every row says not configured', rowsOf(w2).every((node) => /not configured/.test(node.textContent)));
  check('the router row names its three variables',
    /set ARAS_BASE_URL, ARAS_ROUTER_TOKEN, ARAS_MODEL/.test(row(w2, 'router').textContent), row(w2, 'router').textContent);

  w2.document.getElementById('btn-aras-top').click();
  await sleep(300);
  const body2 = w2.document.getElementById('aras-body').textContent;
  check('the wiring report says nothing is configured', /No ARAS service is configured/.test(body2), body2.slice(0, 80));
  check('...and that there is no other model', /only model this app has/.test(body2) && !/fallback provider/i.test(body2));
  check('the ARAS pill is red', /not configured/.test(text(w2, 'badge-aras')) && pillHas(w2, 'pill-aras', 'bad'), text(w2, 'badge-aras'));
  w2.document.getElementById('btn-aras-close').click();

  w2.document.getElementById('btn-diag-top').click();
  await sleep(300);
  const diag2 = w2.document.getElementById('diag-summary').textContent;
  check('diagnostics name the missing variables', /ARAS_ROUTER_TOKEN/.test(diag2) && /ARAS_WEB_TOKEN/.test(diag2), diag2.slice(0, 140));
  check('diagnostics carry the hint', /→ The model is not configured/.test(diag2));
  check('the diagnostics pill is red', /model failed · search failed/.test(text(w2, 'badge-diag')) && pillHas(w2, 'pill-diag', 'bad'), text(w2, 'badge-diag'));
  check('the health card says not configured', /not configured/.test(text(w2, 'health-model')) && /not configured/.test(text(w2, 'health-search')),
    `${text(w2, 'health-model')} / ${text(w2, 'health-search')}`);

  w2.document.querySelector('.mode-btn[data-mode="agent"]').click();
  w2.document.getElementById('prompt').value = 'hello';
  w2.document.getElementById('btn-send').click();
  await sleep(400);
  const answer = [...w2.document.querySelectorAll('.message.assistant .msg-text')].pop();
  const said = answer ? answer.textContent : '';
  check('a prompt answers with the configuration error', /not configured — missing ARAS_BASE_URL, ARAS_ROUTER_TOKEN, ARAS_MODEL/.test(said), said.slice(0, 120));
  check('...and the hint that fixes it', /hint: set ARAS_BASE_URL/.test(said));
  check('...said once, not repeated', (said.match(/not configured — missing/g) || []).length === 1,
    `${(said.match(/not configured — missing/g) || []).length} times`);
  check('...and never an invented answer', !/pong|stub answer/.test(said));

  // ── the page itself: nothing left to pick, no other vendor named
  const page = (HTML + JS).toLowerCase();
  const vendors = ['groq', 'gemini', 'ollama', 'mistral', 'cerebras', 'tavily', 'serper', 'duckduckgo', 'wikipedia', 'searxng'];
  check('the page names no other model or search vendor', vendors.every((name) => !page.includes(name)),
    vendors.filter((name) => page.includes(name)).join(', '));
  check('the page never asks for LLM_PROVIDER', !/llm_provider|search_provider=|search_providers/.test(page));
}

(async () => {
  await scenarioStaticHost();
  console.log('');
  await scenarioArasServices();
  console.log('');
  await scenarioThemeAndColumns();
  console.log('');
  await scenarioLiveThoughts();
  console.log('');
  await scenarioRichAnswer();
  console.log('');
  await scenarioLayout();
  if (liveBase) {
    console.log('');
    await scenarioApiPrefix();
    console.log('');
    await scenarioNormalHost();
  } else {
    console.log('\n(skipping A and C: pass a base URL, e.g. `node tests/ui_smoke.js http://localhost:8000`)');
  }
  console.log('');
  if (failures.length) {
    console.log(`FAILED ${failures.length} check(s): ${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('UI smoke test: all checks passed');
})();
