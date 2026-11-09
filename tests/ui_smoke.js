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

  check('negotiation found the prefixed API', text(w, 'badge-llm') !== 'LLM: backend down' && !bannerShown(w), text(w, 'badge-llm'));
  check('search backends loaded through the prefix', w.document.querySelectorAll('#search-list .provider-row').length > 0);
}

async function scenarioNormalHost() {
  console.log('A  normal host (UI + API from one process)');
  const w = mount((target, init) => fetch(new URL(String(target), liveBase + '/').href, init), liveBase + '/');
  await sleep(1200);

  check('llm badge resolved', !/backend down|offline/.test(text(w, 'badge-llm')), text(w, 'badge-llm'));
  check('no error banner', !bannerShown(w));
  check('modes rendered', modes(w) === MODE_COUNT, `${modes(w)} mode buttons`);
  check('auto mode is first and default', (w.document.querySelector('.mode-btn.active') || {}).dataset?.mode === 'auto');
  check('attachment control is present', !!attachButton(w));
  check('orchestrator panel is online', /planner/.test(text(w, 'agents-summary')));
  check('search backends listed', w.document.querySelectorAll('#search-list .provider-row').length > 0);

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
    'Tools holds providers / ARAS / diagnostics / evaluate',
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
  check('the wiring report rendered', /ARAS|model chain|configured/i.test(arasBody), arasBody.slice(0, 70));
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
  check('the providers modal has a copy button', !!w.document.getElementById('btn-providers-copy'));

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
   backend cannot answer without a key, so this scenario stubs /orchestrate. */
const MARKDOWN_ANSWER = [
  '## Findings',
  '',
  'The manager split this into **three** tasks and ran them in _parallel waves_.',
  '',
  '| Area | Result |',
  '| --- | --- |',
  '| Planning | 3 agents, 2 waves |',
  '| Search | ddg → wikipedia |',
  '',
  '- The planner wrote a brief per agent.',
  '- Agents ran with tools.',
  '',
  '> Guardrail: no provider key means no answer, never an invented one.',
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
    provider: 'groq',
    model: 'llama-3.3-70b',
    session: { session_id: 'sess-rich', turns: 1 },
    orchestration: {
      run_id: 'run-rich', provider: 'groq', model: 'llama-3.3-70b', strategy: 'heuristic',
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
    '/health': { status: 'ok', version: '0.12.0', model_chain: ['groq'], search_chain: ['ddg'] },
    '/providers': { providers: [], search: { providers: [] } },
    '/agents': { planner: 'llm', synthesize: true, parallel: 4, roster: ['search'], agents: [] },
    '/runs': { runs: [] },
    '/sessions': { sessions: [] },
    '/orchestrate': richPayload(),
    '/chat': {
      ok: true,
      answer: 'Two steps: one model turn and one tool call.',
      provider: 'groq', model: 'llama-3.3-70b', latency_ms: 900, steps: 2,
      sources: ['https://example.com/a'],
      trace: [
        { type: 'model', provider: 'groq', model: 'llama-3.3-70b', latency_ms: 400,
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
      '/health': { status: 'ok', version: '0.13.0', model_chain: ['groq'], search_chain: ['ddg'] },
      '/providers': { providers: [], search: { providers: [] } },
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
      '/health': { status: 'ok', version: '0.15.0', model_chain: ['groq'], search_chain: ['ddg'] },
      '/providers': { providers: [], search: { providers: [] } },
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

(async () => {
  await scenarioStaticHost();
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
