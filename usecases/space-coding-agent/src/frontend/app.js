// Copyright (C) 2025-2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
'use strict';

// Everything on screen is rebuilt from the session's SSE events, so a reload or a
// second tab shows the same chat, timeline and board. All text goes in via textContent.

const EXAMPLES = [
  ['Sort benchmark · 4 microVMs',
   'Benchmark four sorting algorithms in parallel, one microVM each: quicksort, mergesort, heapsort (with heapq) ' +
   "and Python's built-in sorted (timsort). Every task generates the same 1,000,000 random ints with seed 42, " +
   'checks that its output is sorted, and reports the best of 3 runs in seconds. Compare them in a table.'],
  ['Monte Carlo π · 8 microVMs',
   'Estimate pi with a Monte Carlo simulation in 8 parallel sandboxes, seeds 1 to 8, 4,000,000 samples each. ' +
   'Report each estimate, then the mean, the standard deviation and the error against math.pi, in a table.'],
  ['Queue sweep · 8 microVMs',
   'Simulate an M/M/1 queue in 8 parallel sandboxes, one per utilisation rho = 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, ' +
   '0.9, 0.95, with service rate 1, 200,000 customers and seed 7. Compare the simulated mean wait in the queue ' +
   'with the theory Wq = rho / (1 - rho) in a table.'],
  ['Session persistence · 1 microVM',
   'In your sandbox, write data.csv with 1,000 rows of synthetic sales data (date, region, amount) using seed 3. ' +
   'Then, in a separate step, read it back and report the total and average amount per region.'],
  ['Isolation check',
   'Show what this sandbox is: run uname -a, nproc, free -m and df -h. Then try to reach ' +
   'http://litellm.litellm.svc.cluster.local:4000 from inside it and tell me what happens.'],
];

const LIVE_STATES = new Set(['creating', 'ready', 'running', 'done', 'failed']);
const OUTPUT_LIMIT = 20000;
const HISTORY_LIMIT = 60;

const $ = (id) => document.getElementById(id);
const state = {
  sid: null, es: null, lastSeq: 0, running: false,
  cards: new Map(), tools: new Map(), bubbles: new Map(), thinking: null,
  podKernel: '', guestKernel: '', statusTimer: null,
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

const secs = (ms) => (ms === null || ms === undefined) ? '' : (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + ' s';

// ── API ───────────────────────────────────────────────────────────────────
class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== '/api/login') showLogin();
  if (!res.ok) throw new HttpError(res.status, data.error || res.statusText);
  return data;
}

// ── login ─────────────────────────────────────────────────────────────────
function showLogin() {
  if (state.es) { state.es.close(); state.es = null; }
  $('login').hidden = false;
  $('login-token').focus();
}

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('login-error').textContent = '';
  try {
    await api('POST', '/api/login', { token: $('login-token').value });
    $('login-token').value = '';
    $('login').hidden = true;
    await boot();
    if (!$('login').hidden) {  // the token was right, but the next request had no cookie
      $('login-error').textContent = 'Signed in, but the browser did not keep the session cookie. ' +
        'Open the app over https (or http://localhost).';
    }
  } catch (err) {
    $('login-error').textContent = err.message;
  }
});

// ── status bar ────────────────────────────────────────────────────────────
async function refreshStatus() {
  try {
    const s = await api('GET', '/api/status');
    $('st-llm').className = 'dot ' + (s.llm ? 'ok' : 'bad');
    $('st-space').className = 'dot ' + (s.space ? 'ok' : 'bad');
    $('st-live').textContent = s.live_sandboxes + ' live microVM' + (s.live_sandboxes === 1 ? '' : 's');
    $('st-model').textContent = s.model + ' · template ' + s.template;
    state.podKernel = s.pod_kernel;
    renderKernels();
  } catch (err) { /* login overlay or a transient error */ }
}

let statusSoon = null;
function refreshStatusSoon() {  // live count right after a VM starts or dies
  clearTimeout(statusSoon);
  statusSoon = setTimeout(refreshStatus, 300);
}

function renderKernels() {
  const parts = [];
  if (state.podKernel) parts.push('pod kernel ' + state.podKernel);
  if (state.guestKernel) parts.push('microVM kernel ' + state.guestKernel);
  $('st-kernels').textContent = parts.join('  vs  ') || 'kernels';
}

// ── session and event stream ──────────────────────────────────────────────
async function boot() {
  await refreshStatus();
  if (!$('login').hidden) return;
  clearInterval(state.statusTimer);
  state.statusTimer = setInterval(refreshStatus, 10000);
  const saved = sessionStorage.getItem('sca_sid');
  if (saved) {
    try {
      await api('GET', '/api/sessions/' + saved);
      return connect(saved);
    } catch (err) {
      if (err.status === 401) return;
    }
  }
  await newSession();
}

async function newSession() {
  const s = await api('POST', '/api/sessions');
  sessionStorage.setItem('sca_sid', s.session_id);
  connect(s.session_id);
}

function connect(sid, after) {
  if (state.es) state.es.close();
  if (!after) clearUI(true);
  state.sid = sid;
  state.lastSeq = after || 0;
  const es = new EventSource('/api/sessions/' + sid + '/events' + (after ? '?after=' + after : ''));
  state.es = es;
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (err) { return; }
    if (ev.type !== 'session.snapshot' && ev.seq <= state.lastSeq) return;  // replay overlap
    state.lastSeq = ev.seq;
    const handler = HANDLERS[ev.type];
    if (handler) handler(ev.data || {}, ev);
  };
  es.onerror = async () => {
    if (es.readyState !== EventSource.CLOSED || state.es !== es) return;  // the browser retries by itself
    try {
      await api('GET', '/api/sessions/' + sid);
      setTimeout(() => { if (state.es === es) connect(sid, state.lastSeq); }, 2000);
    } catch (err) {
      if (err.status === 404) { notice('This chat expired on the server; a new one was started.'); newSession(); }
      else if (err.status !== 401) setTimeout(() => { if (state.es === es) connect(sid, state.lastSeq); }, 5000);
    }
  };
}

function clearUI(all) {
  $('messages').replaceChildren(welcome);
  state.bubbles.clear();
  state.thinking = null;
  $('timeline').replaceChildren();
  state.tools.clear();
  setRunning(false);
  if (all) {
    $('cards').replaceChildren();
    $('history-cards').replaceChildren();
    state.cards.clear();
    $('fanout-banner').hidden = true;
    updateBoardCount();
  }
}

// ── chat ──────────────────────────────────────────────────────────────────
const welcome = document.querySelector('.welcome');

function scrollChat(force) {
  const box = $('messages');
  if (force || box.scrollHeight - box.scrollTop - box.clientHeight < 120) box.scrollTop = box.scrollHeight;
}

function addMessage(cls, node) {
  welcome.remove();
  const box = $('messages');
  if (state.thinking && state.thinking.parentNode === box) box.insertBefore(node, state.thinking);
  else box.append(node);
  scrollChat(cls === 'user');
  return node;
}

function userMessage(text) { addMessage('user', el('div', 'msg user', text)); }

function notice(text, isError) { addMessage('notice', el('div', 'msg' + (isError ? ' error' : ' muted'), text)); }

function assistantBubble(id) {
  let b = state.bubbles.get(id);
  if (!b) {
    b = { node: addMessage('assistant', el('div', 'msg assistant')), text: '', pending: false };
    state.bubbles.set(id, b);
  }
  return b;
}

function renderBubble(b) {
  b.pending = false;
  b.node.replaceChildren(renderMarkdown(b.text));
  scrollChat(false);
}

function setThinking(text) {
  if (!state.running) return;
  if (!state.thinking) state.thinking = el('div', 'thinking');
  state.thinking.textContent = text;
  if (!state.thinking.parentNode) { $('messages').append(state.thinking); scrollChat(false); }
}

function setRunning(running) {
  state.running = running;
  $('send').disabled = running;
  $('btn-cancel').disabled = !running;
  if (running) setThinking('Thinking…');
  else if (state.thinking) { state.thinking.remove(); state.thinking = null; }
}

// Minimal markdown: fenced code, tables, headings, lists, paragraphs, `code` and **bold**.
function renderMarkdown(src) {
  const frag = document.createDocumentFragment();
  // Some models wrap tool calls in an xml fence; the server parses the call out and
  // leaves the fence behind, which would turn the rest of the reply into code.
  const lines = src.replace(/\r\n/g, '\n').replace(/^[ \t]*```xml[ \t]*\n([ \t]*```[ \t]*(\n|$))?/gm, '')
    .replace(/^[ \t]*```xml[ \t]*$/gm, '').split('\n');
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isList = (l) => /^\s*([-*]|\d+\.)\s+/.test(l);
  const isBlock = (l) => /^\s*(```|\||#{1,4}\s)/.test(l) || isList(l);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      const buf = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) buf.push(lines[i]);
      i++;
      const pre = el('pre');
      pre.append(el('code', null, buf.join('\n')));
      frag.append(pre);
    } else if (isRow(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const table = el('table');
      const head = el('tr');
      cells(line).forEach((c) => head.append(inline(el('th'), c)));
      table.append(head);
      for (i += 2; i < lines.length && isRow(lines[i]); i++) {
        const tr = el('tr');
        cells(lines[i]).forEach((c) => tr.append(inline(el('td'), c)));
        table.append(tr);
      }
      frag.append(table);
    } else if (/^#{1,4}\s/.test(line)) {
      const level = line.match(/^#+/)[0].length;
      frag.append(inline(el('h' + Math.min(level + 1, 4)), line.replace(/^#+\s+/, '')));
      i++;
    } else if (isList(line)) {
      const list = el(/^\s*\d+\./.test(line) ? 'ol' : 'ul');
      for (; i < lines.length && isList(lines[i]); i++) {
        list.append(inline(el('li'), lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '')));
      }
      frag.append(list);
    } else if (!line.trim()) {
      i++;
    } else {
      const buf = [line];
      for (i++; i < lines.length && lines[i].trim() && !isBlock(lines[i]); i++) buf.push(lines[i]);
      frag.append(inline(el('p'), buf.join('\n')));
    }
  }
  return frag;
}

function cells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

function inline(node, text) {
  for (const part of text.split(/(`[^`]+`|\*\*[^*]+\*\*)/)) {
    if (!part) continue;
    if (part.startsWith('`') && part.endsWith('`') && part.length > 1) node.append(el('code', null, part.slice(1, -1)));
    else if (part.startsWith('**') && part.endsWith('**') && part.length > 4) node.append(el('strong', null, part.slice(2, -2)));
    else node.append(document.createTextNode(part));
  }
  return node;
}

// ── tool timeline ─────────────────────────────────────────────────────────
function parsed(value) {
  try { return JSON.parse(value); } catch (err) { return null; }  // a truncated preview is not valid JSON
}

function argsText(name, args) {
  const params = name === 'run_sweep' && parsed(args.params);
  if (params) return 'PARAM in ' + JSON.stringify(params) + '\n\n' + (args.code || '');
  return Object.entries(args).map(([k, v]) => (String(v).includes('\n') ? k + ':\n' + v : k + ' = ' + v)).join('\n');
}

function argsSummary(name, args) {
  const params = name === 'run_sweep' && parsed(args.params);
  if (params) return params.length + ' microVMs: ' + params.join(', ').slice(0, 70);
  if (name === 'run_parallel' && args.scripts) {
    const labels = [...args.scripts.matchAll(/^#\s*task:\s*(.+)$/gm)].map((m) => m[1].trim());
    return labels.length + ' microVMs: ' + labels.join(', ').slice(0, 70);
  }
  const first = Object.values(args)[0];
  return first ? String(first).split('\n')[0].slice(0, 70) : '';
}

function details(summary, body) {
  const d = el('details');
  d.append(el('summary', null, summary), el('pre', null, body));
  return d;
}

// A chip per microVM in the tool row that used it; hovering one points at its card.
function linkCall(c, callId) {
  const tool = callId && state.tools.get(callId);
  if (!tool || c.calls.has(callId)) return;
  c.calls.add(callId);
  const chip = el('span', 'vm s-' + (c.state || 'queued'), c.label);
  chip.title = c.kind + ' microVM ' + c.label;
  chip.addEventListener('mouseenter', () => { c.root.classList.add('hl'); c.root.scrollIntoView({ block: 'nearest' }); });
  chip.addEventListener('mouseleave', () => c.root.classList.remove('hl'));
  tool.vms.append(chip);
  c.chips.push(chip);
}

function toolStart(d, ev) {
  const node = el('li', 'tool');
  const head = el('div', 'head');
  const dur = el('span', 'muted', 'running…');
  head.append(el('span', 'name', d.name), dur);
  node.append(head, el('div', 'muted', argsSummary(d.name, d.args || {})));
  const vms = el('div', 'vms');
  node.append(vms);
  if (d.args && Object.keys(d.args).length) node.append(details('arguments', argsText(d.name, d.args)));
  $('timeline').append(node);
  node.scrollIntoView({ block: 'nearest' });
  state.tools.set(ev.call_id, { node, dur, vms });
  setThinking('Running ' + d.name + '…');
}

function toolEnd(d, ev) {
  const t = state.tools.get(ev.call_id);
  if (!t) return;
  t.node.classList.add(d.status === 'ok' ? 'ok' : 'error');
  t.dur.textContent = d.status === 'cancelled' ? 'cancelled' : secs(d.duration_ms);
  t.node.append(details('result (' + d.result_chars + ' chars)', d.result_preview || ''));
  setThinking('Thinking…');
}

// ── sandbox board ─────────────────────────────────────────────────────────
function newCard(key, d, ev) {
  const root = el('div', 'card');
  const top = el('div', 'top');
  const name = el('div');
  name.append(el('span', 'label', d.label || key), el('span', 'kind', d.kind || 'fanout'));
  const pill = el('span', 'pill');
  top.append(name, pill);
  const cmd = el('div', 'meta');
  const meta = el('div', 'meta');
  const out = el('pre');
  const result = el('div', 'result');
  root.append(top, meta, cmd, out, result);
  const c = { key, root, pill, meta, cmd, out, result, label: d.label || key, kind: d.kind || 'fanout',
              calls: new Set(), chips: [], fields: {}, since: Date.now(), state: null, outLen: 0 };
  if (c.kind === 'session') $('cards').prepend(root); else $('cards').append(root);
  state.cards.set(key, c);
  linkCall(c, ev.call_id);
  return c;
}

function setCardState(c, s) {
  c.state = s;
  c.since = Date.now();
  c.root.className = 'card s-' + s + (c.root.classList.contains('hl') ? ' hl' : '');
  for (const chip of c.chips) chip.className = 'vm s-' + s;
  tickCard(c);
  updateBoardCount();
}

function tickCard(c) {
  const live = c.state === 'creating' || c.state === 'running' || c.state === 'queued';
  c.pill.textContent = c.state + (live ? ' ' + ((Date.now() - c.since) / 1000).toFixed(1) + ' s' : '');
}

function setFields(c, patch) {
  Object.assign(c.fields, patch);
  c.meta.replaceChildren(...Object.entries(c.fields)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => el('span', null, k + ' ' + v)));
}

function updateBoardCount() {
  let live = 0;
  for (const c of state.cards.values()) if (c.fields.id && LIVE_STATES.has(c.state)) live++;
  $('board-count').textContent = live ? '· ' + live + ' live' : '';
  $('history-count').textContent = '(' + $('history-cards').children.length + ')';
}

function retire(c) {  // killed cards move to History after a moment
  setTimeout(() => {
    if (state.cards.get(c.key) !== c) return;
    state.cards.delete(c.key);
    const hist = $('history-cards');
    hist.prepend(c.root);
    while (hist.children.length > HISTORY_LIMIT) hist.lastChild.remove();
    updateBoardCount();
  }, 4000);
}

function onQueued(d, ev) {
  setCardState(newCard(d.key, { label: d.label, kind: 'fanout' }, ev), 'queued');
}

function onCreating(d, ev) {
  let c = d.queued_key && state.cards.get(d.queued_key);
  if (c) {
    state.cards.delete(d.queued_key);
    c.key = d.key;
    state.cards.set(d.key, c);
  } else {
    c = newCard(d.key, d, ev);
  }
  setFields(c, { template: d.template });
  setCardState(c, 'creating');
}

function onReady(d) {
  const c = state.cards.get(d.key);
  if (!c) return;
  setFields(c, { template: undefined, id: d.sandbox_id, boot: secs(d.create_ms) + (d.attempts > 1 ? ' (' + d.attempts + ' tries)' : '') });
  c.root.title = 'sandbox ' + d.sandbox_id;
  setCardState(c, 'ready');
  refreshStatusSoon();
}

function onInfo(d) {
  const c = state.cards.get(d.key);
  if (c) setFields(c, { kernel: d.kernel, python: d.python, vcpu: d.nproc });
  if (d.kernel) { state.guestKernel = d.kernel; renderKernels(); }
}

function onExecStart(d, ev) {
  const c = state.cards.get(d.key);
  if (!c) return;
  linkCall(c, ev.call_id);  // the session VM serves many tool calls
  c.cmd.textContent = (d.kind === 'python' ? '» python  ' : '$ ') + d.preview.split('\n')[0].slice(0, 120);
  c.out.textContent = '';
  c.outLen = 0;
  c.result.textContent = '';
  setFields(c, { run: undefined, exit: undefined });
  setCardState(c, 'running');
}

function onExecOutput(d) {
  const c = state.cards.get(d.key);
  if (!c || c.outLen > OUTPUT_LIMIT) return;
  const chunk = d.stream === 'stderr' ? d.chunk.replace(/^(?=.)/gm, '! ') : d.chunk;
  c.out.append(document.createTextNode(chunk));
  c.outLen += chunk.length;
  if (c.outLen > OUTPUT_LIMIT) c.out.append(document.createTextNode('\n… output truncated in the UI'));
  c.out.scrollTop = c.out.scrollHeight;
}

function onExecEnd(d) {
  const c = state.cards.get(d.key);
  if (!c) return;
  setFields(c, { run: secs(d.duration_ms), exit: d.timed_out ? 'timeout' : d.exit_code });
  if (d.result_json) c.result.textContent = 'RESULT ' + JSON.stringify(d.result_json);
  else if (d.error) c.result.textContent = d.error;
  const ok = d.exit_code === 0 && !d.timed_out && !d.error;
  // The session VM stays up between calls; a fan-out VM is done after its one task.
  setCardState(c, c.kind === 'session' ? 'ready' : (ok ? 'done' : 'failed'));
}

function onKilled(d) {
  const c = state.cards.get(d.key);
  if (!c) return;
  setFields(c, { killed: d.reason, lifetime: secs(d.lifetime_ms) });
  setCardState(c, 'killed');
  retire(c);
  refreshStatusSoon();
}

function onSandboxError(d) {
  const c = state.cards.get(d.key);
  if (!c) return;
  if (d.phase === 'kill') { setFields(c, { kill: 'failed, will retry' }); return; }
  c.result.textContent = d.error + (d.retryable ? ' (retried)' : '');
  setCardState(c, 'failed');
  retire(c);
}

let fanoutTimer = null;
function onFanoutStart(d) {
  const banner = $('fanout-banner');
  const started = Date.now();
  banner.hidden = false;
  clearInterval(fanoutTimer);
  const tick = () => {
    banner.textContent = 'Parallel run: ' + d.n + ' microVMs, up to ' + d.concurrency + ' at once · ' +
      ((Date.now() - started) / 1000).toFixed(1) + ' s';
  };
  tick();
  fanoutTimer = setInterval(tick, 200);
}

function onFanoutEnd(d) {
  clearInterval(fanoutTimer);
  $('fanout-banner').textContent = 'Parallel run: ' + d.ok + ' ok, ' + d.failed + ' failed · wall ' + secs(d.wall_ms) +
    ' · boot ' + secs(d.boot_min_ms) + '–' + secs(d.boot_max_ms) + ' · run ' + secs(d.run_wall_ms) +
    ' side by side vs ' + secs(d.sum_run_ms) + ' in turn' + (d.speedup ? ' (' + d.speedup + '×)' : '');
}

setInterval(() => {
  for (const c of state.cards.values()) if (c.state === 'creating' || c.state === 'running' || c.state === 'queued') tickCard(c);
}, 200);

// ── event handlers ────────────────────────────────────────────────────────
const HANDLERS = {
  'session.snapshot': (d) => {
    clearUI(true);
    (d.messages || []).forEach((m, n) => {
      if (m.role === 'user') { userMessage(m.text); return; }
      const b = assistantBubble('snap_' + n);
      b.text = m.text;
      renderBubble(b);
    });
    for (const s of d.sandboxes || []) {
      const c = newCard(s.key, s, {});
      setFields(c, { id: s.sandbox_id, kernel: s.kernel, python: s.python, vcpu: s.nproc });
      setCardState(c, 'ready');
    }
    setRunning(!!d.running);
  },
  'session.reset': () => {
    clearUI(false);
    $('fanout-banner').hidden = true;
  },
  'run.start': (d) => {
    userMessage(d.user_text);
    setRunning(true);
    const sep = el('li', 'run-sep', d.user_text.split('\n')[0].slice(0, 80));
    $('timeline').append(sep);
  },
  'message.delta': (d) => {
    const b = assistantBubble(d.message_id);
    b.text += d.text;
    if (!b.pending) { b.pending = true; requestAnimationFrame(() => renderBubble(b)); }
  },
  'message.end': (d) => {
    const b = state.bubbles.get(d.message_id);
    if (b) renderBubble(b);
  },
  'run.end': (d) => {
    setRunning(false);
    if (d.status === 'cancelled') notice('Cancelled.');
    else if (d.status !== 'ok') notice((d.status === 'timeout' ? 'Timed out: ' : 'Error: ') + (d.error || d.status), true);
    const tokens = (d.usage && (d.usage.input_token_count || 0) + (d.usage.output_token_count || 0)) || 0;
    addMessage('meta', el('div', 'run-meta', secs(d.duration_ms) + (tokens ? ' · ' + tokens.toLocaleString() + ' tokens' : '')));
  },
  'tool.start': toolStart,
  'tool.end': toolEnd,
  'fanout.start': onFanoutStart,
  'fanout.end': onFanoutEnd,
  'sandbox.queued': onQueued,
  'sandbox.creating': onCreating,
  'sandbox.ready': onReady,
  'sandbox.info': onInfo,
  'sandbox.exec.start': onExecStart,
  'sandbox.exec.output': onExecOutput,
  'sandbox.exec.end': onExecEnd,
  'sandbox.killed': onKilled,
  'sandbox.error': onSandboxError,
};

// ── controls ──────────────────────────────────────────────────────────────
async function send(text) {
  text = text.trim();
  if (!text || state.running || !state.sid) return;
  try {
    await api('POST', '/api/sessions/' + state.sid + '/messages', { text });
    $('input').value = '';
  } catch (err) {
    if (err.status !== 401) notice(err.message, true);
  }
}

$('composer').addEventListener('submit', (e) => { e.preventDefault(); send($('input').value); });
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send($('input').value); }
});

$('btn-cancel').addEventListener('click', () => {
  if (state.sid) api('POST', '/api/sessions/' + state.sid + '/cancel').catch((err) => notice(err.message, true));
});

$('btn-reset').addEventListener('click', () => {
  if (state.sid) api('POST', '/api/sessions/' + state.sid + '/reset').catch((err) => notice(err.message, true));
});

$('btn-new').addEventListener('click', async () => {
  const old = state.sid;
  try {
    await newSession();
    if (old) api('DELETE', '/api/sessions/' + old).catch(() => {});
  } catch (err) {
    if (err.status !== 401) notice(err.message, true);
  }
});

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('sca_theme', theme); } catch (err) { /* private mode: not remembered */ }
  $('btn-theme').textContent = theme === 'dark' ? '☀ Light' : '☾ Dark';
}
$('btn-theme').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
setTheme(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');

for (const [title, prompt] of EXAMPLES) {
  const b = el('button', null, title);
  b.type = 'button';
  b.title = prompt;
  b.addEventListener('click', () => { $('input').value = prompt; $('input').focus(); });
  $('examples').append(b);
}

boot();
