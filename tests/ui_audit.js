#!/usr/bin/env node
/**
 * UI check for the self-audit panel (the "Evaluate" button).
 *
 * The Python suite proves the audit and its routes; this proves the *page*:
 * that the checklist is visible before a run (so the panel never opens as a bare
 * spinner), that each check fills in as its verdict arrives, that the verdicts
 * and the scorecard agree, and that the JSON view, copy and download all carry
 * the same object.
 *
 *   npm i jsdom                    # once, anywhere on NODE_PATH
 *   node tests/ui_audit.js         # stubbed API — no server needed
 *   node tests/ui_audit.js http://localhost:8000   # against the real backend
 *
 * The stub mode answers /audit and /audit/stream and 404s everything else, which
 * is exactly the "panel must work on its own" case: the connection banner comes
 * up, and the audit still opens, runs and reports.
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
  console.log('SKIP tests/ui_audit.js — jsdom is not installed (`npm i jsdom`)');
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

// ── the scorecard the stub streams: 16 pass, 2 skipped live checks ─────────
const LEVELS = [
  { id: 'plan', title: 'Planning — who runs, with what brief, in what order' },
  { id: 'execute', title: 'Execution — waves, failure isolation, timeouts' },
  { id: 'gate', title: 'Evaluator gate — scoring, rewriting, keeping evidence' },
  { id: 'manager', title: 'Manager — review loop and synthesis' },
  { id: 'record', title: 'Trace, run record and live stream' },
  { id: 'outcome', title: 'Outcome proxies — grounded, stable answers' },
  { id: 'robust', title: 'Robustness — odd input, injected text' },
  { id: 'live', title: 'Live model checks (opt-in, needs a configured model)' },
];
const CHECKS = [
  ['plan.selection', 'plan', 'Agent selection', 'the rules pick the specialists a request cannot be answered without', 'nothing', false, 'pass'],
  ['plan.waves', 'plan', 'Wave scheduling', 'independent agents share a wave, dependencies wait, cycles cannot hang', 'nothing', false, 'pass'],
  ['exec.isolation', 'execute', 'Failure isolation', 'one agent failing does not take the run down', 'nothing', false, 'pass'],
  ['exec.timeout', 'execute', 'Timeout fence', 'a slow agent is fenced by the timeout', 'nothing', false, 'pass'],
  ['gate.rules', 'gate', 'Evaluator rules', 'the no-model gate still scores and flags', 'nothing', false, 'pass'],
  ['manager.review', 'manager', 'Review loop', 'one agent means no review; a real review runs once', 'nothing', false, 'pass'],
  ['manager.fallback', 'manager', 'Synthesis fallback', 'with no model the manager merges the reports', 'nothing', false, 'pass'],
  ['record.roundtrip', 'record', 'Run record', 'every run serialises to JSON', 'nothing', false, 'pass'],
  ['record.stream', 'record', 'Live stream', 'a watcher sees every event once, in order', 'nothing', false, 'pass'],
  ['outcome.grounding', 'outcome', 'Grounding', 'the answer cites what the run found', 'nothing', false, 'pass'],
  ['outcome.stability', 'outcome', 'Repeatability', 'the same request plans the same way', 'nothing', false, 'pass'],
  ['robust.inputs', 'robust', 'Odd input', 'huge files do not break the pipeline', 'nothing', false, 'pass'],
  ['robust.injection', 'robust', 'Injected document text', 'instructions in an attachment cannot re-plan the run', 'nothing', false, 'pass'],
  ['live.planner', 'live', 'LLM planner vs rules', 'the model planner agrees with the rules', 'a configured model (the ARAS router block in .env)', true, 'skip'],
  ['live.latency', 'live', 'Stage latency', 'a real run gives the per-stage cost', 'a configured model (the ARAS router block in .env)', true, 'skip'],
];

function scorecard() {
  const checks = CHECKS.map(([id, level, title, proves, needs, live, status]) => ({
    id,
    level,
    title,
    proves,
    needs,
    live,
    status,
    ms: status === 'skip' ? 0 : 4,
    detail: status === 'skip' ? 'live check — enable live model checks (needs a configured model)' : 'as intended',
    metrics: status === 'skip' ? {} : { prompts: 10, coverage: 1 },
    findings: [],
  }));
  const summary = { total: checks.length, pass: 0, warn: 0, fail: 0, skip: 0, status: 'pass' };
  checks.forEach((row) => {
    summary[row.status] += 1;
  });
  return {
    audit_id: 'audit-test-0001',
    version: '0.16.0',
    status: 'pass',
    ms: 312,
    started_at: '2026-11-07T10:00:00+00:00',
    finished_at: '2026-11-07T10:00:00+00:00',
    live_requested: false,
    summary,
    by_level: [],
    environment: { planner: 'auto', evaluate: true, review: true, live_models: false },
    checks,
    files: { json: '/tmp/logs/audit/audit-test-0001.json', markdown: '/tmp/logs/audit/audit-test-0001.md' },
  };
}

const CATALOGUE = {
  version: '0.16.0',
  total: CHECKS.length,
  checks: CHECKS.map(([id, level, title, proves, needs, live]) => ({ id, level, title, proves, needs, live })),
  levels: LEVELS,
  last: null,
  live_available: true,
  environment: { model_ready: true, search_ready: true, planner: 'auto', evaluate: true, review: true },
};

function sseFrames(card) {
  const frames = [];
  frames.push(`event: open\ndata: ${JSON.stringify({ total: card.checks.length, live: false })}\n\n`);
  card.checks.forEach((row, index) => {
    frames.push(
      `event: start\ndata: ${JSON.stringify({
        id: row.id,
        index: index + 1,
        total: card.checks.length,
        level: row.level,
        title: row.title,
      })}\n\n`
    );
    frames.push(`event: check\ndata: ${JSON.stringify({ ...row, index: index + 1, total: card.checks.length })}\n\n`);
  });
  frames.push(`event: result\ndata: ${JSON.stringify(card)}\n\n`);
  return frames;
}

function streamResponse(frames) {
  let index = 0;
  const encoder = new TextEncoder();
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          async read() {
            if (index >= frames.length) return { done: true, value: undefined };
            await sleep(2);
            return { done: false, value: encoder.encode(frames[index++]) };
          },
        };
      },
    },
    text: async () => frames.join(''),
  };
}

function stubFetch(card) {
  const frames = sseFrames(card);
  // The page joins base + path like a URL, so the request may arrive as
  // "audit", "/audit" or "api/audit" depending on what negotiation found.
  const route = (target) => String(target).split('?')[0].replace(/^[./]+/, '');
  return async (target) => {
    const path = route(target);
    if (path === 'audit/stream') return streamResponse(frames);
    if (path === 'audit') return { ok: true, status: 200, json: async () => CATALOGUE };
    return { ok: false, status: 404, json: async () => ({ detail: 'Not Found' }) };
  };
}

function mount(fetchImpl) {
  const dom = new JSDOM(HTML, {
    url: 'http://localhost:8000/',
    pretendToBeVisual: true,
    runScripts: 'outside-only',
  });
  const w = dom.window;
  w.fetch = fetchImpl;
  w.navigator.clipboard = { writeText: async () => {} };
  w.TextDecoder = TextDecoder;
  w.TextEncoder = TextEncoder;
  if (!w.URL.createObjectURL) w.URL.createObjectURL = () => 'blob:stub';
  if (!w.URL.revokeObjectURL) w.URL.revokeObjectURL = () => {};
  w.eval(JS);
  return w;
}

const text = (w, id) => (w.document.getElementById(id) || {}).textContent || '';
const rows = (w) => Array.from(w.document.querySelectorAll('#audit-results .audit-row'));
const statuses = (w) => rows(w).map((row) => row.dataset.status);
const modalOpen = (w) => !w.document.getElementById('audit-modal').classList.contains('panel-hidden');

async function main() {
  const card = scorecard();
  const live = Boolean(liveBase);
  console.log(live ? `live backend ${liveBase}` : 'stub API (no server)');
  const w = mount(
    live
      ? (target, init) => fetch(new URL(String(target), liveBase + '/').href, init)
      : stubFetch(card)
  );
  await sleep(1200);

  console.log('1  the control exists and says what it is');
  check('the top bar has an Evaluate button', !!w.document.getElementById('btn-audit-top'));
  check('the button is labelled Evaluate', /evaluate/i.test(text(w, 'btn-audit-top')));
  check('a status pill reports the audit', /audit/i.test(text(w, 'badge-audit')), text(w, 'badge-audit'));

  console.log('2  opening the panel shows the checklist before anything runs');
  w.document.getElementById('btn-audit-top').click();
  await sleep(400);
  check('the panel opens', modalOpen(w));
  const expected = live ? w.document.querySelectorAll('#audit-results .audit-row').length : CHECKS.length;
  check('every check is listed up front', rows(w).length === expected, `${rows(w).length} rows`);
  check('unrun checks read as queued', live || statuses(w).every((s) => s === 'queued'), statuses(w).join(','));
  check('each row states what it proves', /pick the specialists|plans the same way|fenced/.test(w.document.getElementById('audit-results').textContent));
  check('the level headings group the rows', w.document.querySelectorAll('.audit-level').length >= 4);
  check('the counter says how many will run', /ready · \d+ checks|\d+\/\d+/.test(text(w, 'audit-count')), text(w, 'audit-count'));
  // Before the first run the JSON tab holds a hint; against a backend that has
  // already been audited it holds that scorecard. Either way it is valid JSON.
  let initialJson = null;
  try {
    initialJson = JSON.parse(text(w, 'audit-json'));
  } catch (_) {
    initialJson = null;
  }
  check('the JSON tab starts from a valid document', !!initialJson, text(w, 'audit-json').slice(0, 40));
  check('the live toggle is offered', !!w.document.getElementById('audit-live'));
  check(
    'the panel says what this host is wired to',
    /planner/.test(text(w, 'audit-sub')) && /model/.test(text(w, 'audit-sub')),
    text(w, 'audit-sub')
  );

  console.log('3  running fills the list in, one check at a time');
  w.document.getElementById('btn-audit-run').click();
  await sleep(300);
  const midway = statuses(w);
  check('some checks are still pending mid-run', midway.some((s) => s === 'queued' || s === 'running'), midway.join(','));
  check('the badge says it is running', /running/i.test(text(w, 'badge-audit')), text(w, 'badge-audit'));

  await sleep(live ? 4000 : 4000);
  const final = statuses(w);
  check('no row is left queued', final.every((s) => s !== 'queued'), final.join(','));
  check('every row carries a verdict', final.every((s) => ['pass', 'warn', 'fail', 'skip'].includes(s)), final.join(','));
  check('passes and skips are both shown', final.includes('pass') && final.includes('skip'), final.join(','));
  check('rows show their measured detail', /as intended|needs a configured model|coverage/.test(w.document.getElementById('audit-results').textContent));
  check('a metrics table is available per check', w.document.querySelectorAll('#audit-results .audit-metrics').length > 0);
  check('the progress bar is full', (w.document.getElementById('audit-bar-fill').style.width || '').startsWith('100'));
  check('the counter reports the total time', /\d+\/\d+ · \d+ ms/.test(text(w, 'audit-count')), text(w, 'audit-count'));

  console.log('4  the summary and the pill agree with the rows');
  const summaryText = text(w, 'audit-summary');
  check('the summary strip is visible', w.document.getElementById('audit-summary').hidden === false);
  check('the summary counts passes', /pass \d+/.test(summaryText), summaryText);
  check('the summary counts skips', /skip \d+/.test(summaryText), summaryText);
  check('the summary names the mode', /offline|live/.test(summaryText), summaryText);
  check('the pill reports the outcome', /Audit: \d+\/\d+ pass/.test(text(w, 'badge-audit')), text(w, 'badge-audit'));

  console.log('5  the JSON view is the same object as the visual one');
  w.document.querySelector('.audit-tab[data-audit-tab="json"]').click();
  await sleep(120);
  const raw = text(w, 'audit-json');
  let parsed = null;
  try {
    parsed = JSON.parse(raw);
  } catch (_) {
    parsed = null;
  }
  check('the JSON tab renders valid JSON', !!parsed, raw.slice(0, 60));
  check('the JSON carries an audit id', !!(parsed && parsed.audit_id), raw.slice(0, 60));
  check('the JSON lists every check', !!parsed && parsed.checks.length === rows(w).length);
  check('the JSON keeps metrics and findings per check', !!parsed && parsed.checks.every((row) => 'metrics' in row && 'findings' in row));
  w.document.querySelector('.audit-tab[data-audit-tab="results"]').click();
  await sleep(60);
  check('switching back shows the results again', !w.document.getElementById('audit-results').classList.contains('panel-hidden'));

  console.log('6  copy and download carry the scorecard');
  let copied = '';
  w.navigator.clipboard.writeText = async (value) => {
    copied = value;
  };
  w.document.getElementById('btn-audit-copy').click();
  await sleep(120);
  check('copy sends the scorecard to the clipboard', copied.indexOf('audit_id') >= 0, copied.slice(0, 40));
  let clickedDownload = false;
  const realClick = w.HTMLAnchorElement.prototype.click;
  w.HTMLAnchorElement.prototype.click = function patched() {
    clickedDownload = true;
    if (this.download) copied = this.download;
  };
  w.document.getElementById('btn-audit-download').click();
  w.HTMLAnchorElement.prototype.click = realClick;
  check('download names the file after the audit', clickedDownload && /\.json$/.test(copied), copied);

  console.log('7  closing and reopening keeps the result');
  w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
  await sleep(120);
  check('Escape closes the panel', !modalOpen(w));
  w.document.getElementById('btn-audit-top').click();
  await sleep(200);
  check('reopening shows the last verdicts, not an empty list', modalOpen(w) && statuses(w).every((s) => s !== 'queued'), statuses(w).join(','));

  if (failures.length) {
    console.log(`\n${failures.length} FAILED`);
    process.exit(1);
  }
  console.log('\nall audit UI checks passed');
}

main().catch((error) => {
  console.log('FAIL the harness threw: ' + (error && error.stack ? error.stack : error));
  process.exit(1);
});
