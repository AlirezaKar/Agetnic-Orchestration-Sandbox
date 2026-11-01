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
  check('search backends loaded through the prefix', w.document.querySelectorAll('#search-list .provider-item').length > 0);
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
  check('search backends listed', w.document.querySelectorAll('#search-list .provider-item').length > 0);

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

  w.document.getElementById('btn-diag').click();
  await sleep(1200);
  check('diagnostics produce rows', w.document.querySelectorAll('#diag-summary .diag-row').length > 0);

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
    !!w2.document.querySelector('#sessions-list .session-item.active')
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

(async () => {
  await scenarioStaticHost();
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
