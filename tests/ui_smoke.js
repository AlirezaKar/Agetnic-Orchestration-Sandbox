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
      trace: [
        { seq: 1, stage: 'manager', actor: 'manager', title: 'Manager chose 2 specialists', offset_ms: 0, detail: { note: 'planner=llm' } },
        { seq: 2, stage: 'tool', actor: 'search', title: 'web_search', offset_ms: 300, detail: { query: 'orchestration', latency_ms: 620, output: { count: 5 } } },
        { seq: 3, stage: 'synthesis', actor: 'manager', title: 'Synthesized final answer', offset_ms: 2400, detail: { latency_ms: 1500 } },
      ],
      review: { asked: true, note: 'enough evidence', follow_up: [] },
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
  check('the trace rows keep every event', w.document.querySelectorAll('.orch-panel .trace-row').length === 3);
  check('the answer carries a meta line and actions',
    !!w.document.querySelector('.answer-meta') && w.document.querySelectorAll('.message-actions button').length >= 2);

  check('the inspector shows a run summary', !!w.document.querySelector('#inspector-trace .run-summary'));
  check('the inspector shows a numbered timeline', w.document.querySelectorAll('#inspector-trace .trace-item').length === 3);
  check('the inspector shows tool activity', w.document.querySelectorAll('#inspector-trace .trace-card').length === 1);
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

(async () => {
  await scenarioStaticHost();
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
