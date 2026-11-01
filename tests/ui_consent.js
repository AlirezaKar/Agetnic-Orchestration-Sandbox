#!/usr/bin/env node
/**
 * UI test for the file permission prompt (v0.11.0).
 *
 * The Python suite proves the API refuses a file nobody allowed; this proves the
 * *page* asks first, and that with FILES_PERMISSION_PROMPT=false nothing changes.
 *
 *   npm i jsdom                      # once, anywhere on NODE_PATH
 *   node tests/ui_consent.js         # offline, no server needed
 *
 * Checks:
 *   1. prompt on  — attaching a file shows "Permission needed", the chip is
 *      locked, and Send refuses with a banner instead of uploading.
 *   2. "Allow"    — the panel clears for that file and Send uploads it with
 *      consent: true.
 *   3. "Don't allow" — the file is dropped; nothing is uploaded.
 *   4. prompt off — no panel at all, and the upload still carries consent.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML = fs.readFileSync(path.join(ROOT, 'app/static/index.html'), 'utf8');
const JS = fs.readFileSync(path.join(ROOT, 'app/static/assets/js/app.js'), 'utf8');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  console.log('SKIP tests/ui_consent.js — jsdom is not installed (`npm i jsdom`)');
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

/** A page with a stubbed backend. `calls` records every request it makes. */
function mount(permissionPrompt) {
  const calls = [];
  const json = (data, status = 200) => ({
    ok: status < 400,
    status,
    json: async () => data,
    text: async () => JSON.stringify(data),
  });
  const fetchImpl = async (url, init) => {
    const body = init && init.body ? JSON.parse(init.body) : {};
    // the page asks "health" (relative) or "/health" depending on negotiation
    const route = String(url).split('?')[0].replace(/^\/+/, '');
    calls.push({ url: String(url), route, body });
    if (route === 'health') {
      return json({
        status: 'ok',
        version: '0.11.0',
        model_chain: ['custom'],
        search_chain: [],
        files: { count: 0, pending: 0, permission_prompt: permissionPrompt, writable: true },
        memory: { sessions: 0, turns: 0 },
        ui: {},
        skipped: { models: [], search: [] },
      });
    }
    if (route === 'files') {
      return json({ id: 'file-1', name: body.name, kind: 'text', chars: 5, consent: true });
    }
    if (route === 'orchestrate') {
      return json({
        answer: 'stub answer',
        mode: 'auto',
        run_id: 'run-1',
        orchestration: {
          run_id: 'run-1',
          plan: { intent: 'test', summary: 'stub', strategy: 'heuristic', agents: [], steps: [] },
          agents: [],
          waves: [],
          trace: [],
          trace_summary: {},
          review: {},
          files: [],
        },
      });
    }
    return json({});
  };

  const dom = new JSDOM(HTML, { url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only' });
  const w = dom.window;
  w.fetch = fetchImpl;
  w.navigator.clipboard = { writeText: async () => {} };
  w.eval(JS);
  return { w, calls };
}

function attach(w, name) {
  const input = w.document.getElementById('file-input');
  const file = new w.File([new w.Blob(['hello policy'], { type: 'text/plain' })], name, {
    type: 'text/plain',
  });
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  input.dispatchEvent(new w.Event('change'));
}

const panelShown = (w) => w.document.getElementById('consent').hidden === false;
const chips = (w) => Array.from(w.document.querySelectorAll('.att-chip'));
const lockedChips = (w) => Array.from(w.document.querySelectorAll('.att-chip.att-locked'));
const bannerShown = (w) => w.document.getElementById('conn-banner').classList.contains('show');
const fileUploads = (calls) => calls.filter((call) => call.route === 'files');

async function promptOn() {
  console.log('A  FILES_PERMISSION_PROMPT=true');
  const { w, calls } = mount(true);
  await sleep(120);
  w.document.getElementById('prompt').value = 'summarise the attached policy';
  attach(w, 'policy.md');
  attach(w, 'notes.txt');

  check('the prompt card appears when a file is attached', panelShown(w));
  check('both files are listed as waiting', w.document.querySelectorAll('.consent-row').length === 2);
  check('the chips are marked as locked', lockedChips(w).length === 2);
  check('nothing was uploaded yet', fileUploads(calls).length === 0);

  w.document.getElementById('btn-send').click();
  await sleep(120);
  check('Send is refused with a banner', bannerShown(w));
  check('still nothing uploaded', fileUploads(calls).length === 0);

  const allow = w.document.querySelector('.consent-row .consent-allow');
  allow.click();
  await sleep(60);
  check('Allow clears that file from the prompt', w.document.querySelectorAll('.consent-row').length === 1);
  check('the allowed chip is no longer locked', lockedChips(w).length === 1);

  const deny = w.document.querySelector('.consent-row .consent-deny');
  deny.click();
  await sleep(60);
  check("Don't allow removes the file", chips(w).length === 1);
  check('the prompt card is gone', panelShown(w) === false);

  w.document.getElementById('btn-send').click();
  await sleep(200);
  const uploads = fileUploads(calls);
  check('now the allowed file uploads', uploads.length === 1, JSON.stringify(uploads));
  check('the upload carries consent: true', uploads[0] && uploads[0].body.consent === true);
}

async function promptOff() {
  console.log('B  FILES_PERMISSION_PROMPT=false (the default)');
  const { w, calls } = mount(false);
  await sleep(120);
  w.document.getElementById('prompt').value = 'summarise the attached policy';
  attach(w, 'policy.md');

  check('no prompt card at all', panelShown(w) === false);
  check('the chip is not locked', lockedChips(w).length === 0);
  check('the fixture still holds one file', chips(w).length === 1);

  w.document.getElementById('btn-send').click();
  await sleep(200);
  const uploads = fileUploads(calls);
  check('the upload happens immediately', uploads.length === 1);
  check('the upload is allowed', uploads[0] && uploads[0].body.consent === true);
  check('no permission banner', bannerShown(w) === false);
}

(async () => {
  await promptOn();
  await promptOff();
  console.log('');
  if (failures.length) {
    console.log(`UI consent test: ${failures.length} check(s) failed: ${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('UI consent test: all checks passed');
})();
