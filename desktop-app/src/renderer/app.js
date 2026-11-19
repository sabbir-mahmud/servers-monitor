'use strict';
// The log window. Everything from the log goes in with textContent: a log line is data.

const api = window.nexguard;
const $ = (id) => document.getElementById(id);

const MAX_KEEP = 20000; // entries kept in memory
const MAX_SHOW = 3000; // rows drawn at once; filter to see older matches

const state = {
  sources: [],
  id: null,
  entries: [],
  level: 'all',
  query: '',
  follow: true,
  source: null,
};

document.body.classList.add(api.platform);

// ---------- formatting ----------

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });

function dayLabel(d) {
  const today = new Date();
  const y = new Date(today);
  y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return dayFmt.format(d);
}

function ago(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return '—';
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 45) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

function every(seconds) {
  if (!seconds) return '—';
  if (seconds % 3600 === 0) return `every ${seconds / 3600} h`;
  if (seconds % 60 === 0) return `every ${seconds / 60} min`;
  return `every ${seconds} s`;
}

// ---------- status ----------

function setPill(tone, text) {
  $('pill').dataset.tone = tone;
  $('pill-text').textContent = text;
}

function renderStatus(src) {
  state.source = src;
  const s = src.status;
  $('summary').hidden = false;
  $('f-dir').textContent = src.dir;
  $('f-dir').title = src.dir;
  $('f-interval').textContent = s ? every(s.interval) : '—';
  $('f-checkin').textContent = s && s.at ? ago(s.at) : 'not yet';
  const err = $('f-error');
  err.hidden = true;
  err.removeAttribute('data-tone');
  if (!s) {
    setPill('neutral', 'No check-in yet');
    return;
  }
  const stale = s.ok && s.interval && Date.now() - new Date(s.at).getTime() > 3 * s.interval * 1000 + 60000;
  if (!s.ok) {
    setPill('critical', 'Last check-in failed');
    err.textContent = s.error || 'The last check-in failed.';
    err.hidden = false;
  } else if (stale) {
    setPill('warning', `Quiet since ${ago(s.at)}`);
    err.textContent = 'The agent hasn’t checked in for a while. The computer may have been asleep, or the agent isn’t running.';
    err.dataset.tone = 'warning';
    err.hidden = false;
  } else {
    setPill('good', 'Connected');
  }
}

// ---------- log ----------

function matches(e) {
  if (state.level !== 'all' && e.level !== state.level) return false;
  if (!state.query) return true;
  const q = state.query;
  if (e.msg.toLowerCase().includes(q)) return true;
  return Object.entries(e.attrs).some(([k, v]) => k.toLowerCase().includes(q) || v.toLowerCase().includes(q));
}

function highlight(parent, text) {
  const q = state.query;
  if (!q) {
    parent.append(text);
    return;
  }
  const lower = text.toLowerCase();
  let i = 0;
  for (let j = lower.indexOf(q); j !== -1; j = lower.indexOf(q, i)) {
    parent.append(text.slice(i, j));
    const m = document.createElement('mark');
    m.textContent = text.slice(j, j + q.length);
    parent.append(m);
    i = j + q.length;
  }
  parent.append(text.slice(i));
}

let lastDay = null;

function rowFor(e) {
  const frag = document.createDocumentFragment();
  const d = e.time ? new Date(e.time) : null;
  const valid = d && !Number.isNaN(d.getTime());
  if (valid && d.toDateString() !== lastDay) {
    lastDay = d.toDateString();
    const day = document.createElement('div');
    day.className = 'day';
    day.textContent = dayLabel(d);
    frag.append(day);
  }
  const row = document.createElement('div');
  row.className = `row ${e.level}`;
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = valid ? timeFmt.format(d) : '';
  if (valid) t.title = d.toString();
  const lvl = document.createElement('span');
  lvl.className = `lvl ${e.level}`;
  lvl.textContent = e.level === 'WARN' ? 'WARN' : e.level;
  const msg = document.createElement('span');
  msg.className = 'msg';
  highlight(msg, e.msg);
  for (const [k, v] of Object.entries(e.attrs)) {
    const a = document.createElement('span');
    a.className = 'attr';
    const b = document.createElement('b');
    b.textContent = `${k}=`;
    a.append(b);
    highlight(a, v);
    msg.append(a);
  }
  row.append(t, lvl, msg);
  frag.append(row);
  return frag;
}

function updateCounts() {
  const c = { all: state.entries.length, INFO: 0, WARN: 0, ERROR: 0 };
  for (const e of state.entries) if (e.level in c) c[e.level]++;
  $('c-all').textContent = c.all;
  $('c-info').textContent = c.INFO;
  $('c-warn').textContent = c.WARN;
  $('c-error').textContent = c.ERROR;
}

function shown() {
  return state.entries.filter(matches);
}

function render() {
  const log = $('log');
  const list = shown();
  const visible = list.slice(-MAX_SHOW);
  lastDay = null;
  log.replaceChildren();
  if (list.length > visible.length) {
    const note = document.createElement('div');
    note.className = 'note';
    note.textContent = `${list.length - visible.length} older lines not shown. Filter to find them, or Save to get them all.`;
    log.append(note);
  }
  const frag = document.createDocumentFragment();
  for (const e of visible) frag.append(rowFor(e));
  log.append(frag);
  if (state.entries.length && !list.length) {
    const note = document.createElement('div');
    note.className = 'note';
    note.textContent = 'No lines match.';
    log.append(note);
  }
  updateCounts();
  updateFooter(list.length);
  if (state.follow) log.scrollTop = log.scrollHeight;
}

function append(entries) {
  state.entries.push(...entries);
  const over = state.entries.length - MAX_KEEP;
  if (over > 0) state.entries.splice(0, over);
  const fresh = entries.filter(matches);
  const log = $('log');
  if (fresh.length && log.querySelectorAll('.row').length + fresh.length <= MAX_SHOW) {
    log.querySelector('.note:last-child')?.remove();
    const frag = document.createDocumentFragment();
    for (const e of fresh) frag.append(rowFor(e));
    log.append(frag);
    updateCounts();
    updateFooter(shown().length);
    if (state.follow) log.scrollTop = log.scrollHeight;
  } else if (fresh.length) {
    render();
  } else {
    updateCounts();
  }
  showEmptyIfNeeded();
}

function updateFooter(n) {
  const total = state.entries.length;
  $('footer-count').textContent = n === total ? `${total} lines` : `${n} of ${total} lines`;
  $('footer-live').textContent = state.follow ? 'Following new lines' : 'Paused: scroll to the bottom to follow';
}

function plainText(list) {
  return list.map((e) => {
    const attrs = Object.entries(e.attrs).map(([k, v]) => ` ${k}=${/\s/.test(v) ? JSON.stringify(v) : v}`).join('');
    return `${e.time || ''} ${e.level.padEnd(5)} ${e.msg}${attrs}`.trim();
  }).join('\n') + '\n';
}

// ---------- empty states ----------

function showEmpty(title, text) {
  $('empty-title').textContent = title;
  $('empty-text').textContent = text;
  $('empty').hidden = false;
}

function showEmptyIfNeeded() {
  const src = state.source;
  if (state.entries.length || !src) {
    $('empty').hidden = true;
    return;
  }
  showEmpty('No log yet',
    src.hasLog
      ? 'The log is empty. New lines appear here as the agent writes them.'
      : 'The agent hasn’t written a log here yet. It starts one the next time it runs (agent 0.2.0 or newer). This window will fill in by itself.');
}

function notInstalled() {
  $('summary').hidden = true;
  $('toolbar').hidden = true;
  $('empty').hidden = true;
  $('log').replaceChildren();
  setPill('neutral', 'Not connected');
  showPair({ canCancel: false });
}

// ---------- pairing ----------

const pair = { input: null, preview: null, busy: false };

function pairStep(step) {
  $('pair-form').hidden = step !== 'form';
  $('pair-confirm').hidden = step !== 'confirm';
  $('pair-working').hidden = step !== 'working';
}

function pairError(id, message) {
  const el = $(id);
  el.textContent = message || '';
  el.hidden = !message;
}

function cleanError(err) {
  return String((err && err.message) || err).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
}

function showPair({ server = '', code = '', canCancel = true, autoContinue = false } = {}) {
  const visible = !$('pair').hidden;
  $('pair').hidden = false;
  $('pair-cancel').hidden = !canCancel;
  if (!visible || server || code) {
    if (server) $('pair-server').value = server;
    if (code) $('pair-code').value = code.length === 8 ? `${code.slice(0, 4)}-${code.slice(4)}` : code;
    pairError('pair-error', '');
    pairStep('form');
  }
  if (autoContinue && server && code) previewPair();
  else (server ? $('pair-code') : $('pair-server')).focus();
}

function hidePair() {
  $('pair').hidden = true;
  pairStep('form');
}

async function previewPair() {
  if (pair.busy) return;
  pair.busy = true;
  $('pair-next').disabled = true;
  pairError('pair-error', '');
  pair.input = {
    server: $('pair-server').value,
    code: $('pair-code').value,
    protectedInstall: $('pair-protected').checked,
  };
  try {
    pair.preview = await api.preview(pair.input);
    const d = pair.preview.device;
    $('confirm-device').textContent = d.name;
    $('confirm-account').textContent = d.person ? `${d.account} · ${d.person}` : d.account;
    $('confirm-disclosure').textContent = pair.preview.disclosure;
    const already = state.sources.some((x) => x.installed && x.status);
    $('confirm-mode').textContent = (pair.input.protectedInstall
      ? 'Installs as a protected service: your computer asks for an administrator password next.'
      : 'Installs for you only: it runs while you’re signed in, with no website blocking.') +
      (already ? ' This computer is connected already: this pairing replaces that one.' : '');
    pairError('confirm-error', '');
    pairStep('confirm');
    $('confirm-allow').focus();
  } catch (err) {
    pairError('pair-error', cleanError(err));
  } finally {
    pair.busy = false;
    $('pair-next').disabled = false;
  }
}

async function connectPair() {
  if (pair.busy || !pair.input) return;
  pair.busy = true;
  $('working-text').textContent = pair.input.protectedInstall
    ? 'Waiting for the administrator password, then installing the service.'
    : 'Pairing and starting the agent.';
  pairStep('working');
  try {
    const result = await api.connect(pair.input);
    if (!result.ok) {
      pairError('confirm-error', result.error);
      pairStep('confirm');
      return;
    }
    hidePair();
    $('pair-code').value = '';
    await init();
  } catch (err) {
    pairError('confirm-error', cleanError(err));
    pairStep('confirm');
  } finally {
    pair.busy = false;
  }
}

$('pair-form').addEventListener('submit', (e) => {
  e.preventDefault();
  previewPair();
});
$('pair-code').addEventListener('input', (e) => {
  // Shown as the dashboard shows it: K7PQ-3MXA.
  const raw = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8);
  e.target.value = raw.length > 4 ? `${raw.slice(0, 4)}-${raw.slice(4)}` : raw;
});
$('pair-cancel').addEventListener('click', hidePair);
$('confirm-back').addEventListener('click', () => pairStep('form'));
$('confirm-allow').addEventListener('click', connectPair);
$('pair-open').addEventListener('click', () => showPair({ canCancel: true }));
api.onShowPair(() => showPair({ canCancel: state.sources.some((x) => x.installed) }));
api.onPairLink((link) => showPair({ ...link, canCancel: state.sources.some((x) => x.installed), autoContinue: true }));

// ---------- wiring ----------

async function openSource(id) {
  state.id = id;
  state.entries = [];
  $('log').replaceChildren();
  try {
    const { source, entries } = await api.open(id);
    state.entries = entries;
    $('toolbar').hidden = false;
    renderStatus(source);
    render();
    showEmptyIfNeeded();
  } catch (err) {
    $('toolbar').hidden = true;
    setPill('critical', 'Can’t read the log');
    showEmpty('Can’t read the log', String(err.message || err).replace(/^Error invoking remote method '[^']+': (Error: )?/, ''));
  }
}

function pickDefault(sources) {
  return sources.find((s) => s.hasLog) || sources.find((s) => s.installed);
}

async function init() {
  state.sources = await api.listSources();
  const select = $('source');
  const found = state.sources.filter((s) => s.installed);
  select.replaceChildren(...found.map((s) => {
    const o = document.createElement('option');
    o.value = s.id;
    o.textContent = s.label;
    return o;
  }));
  select.hidden = found.length < 2;
  const first = pickDefault(state.sources);
  $('pair-open').hidden = !first;
  if (!first) {
    if (!$('pair').hidden && pair.busy) return;
    notInstalled();
    setTimeout(() => { if (!state.sources.some((x) => x.installed)) init(); }, 15000); // set up some other way
    return;
  }
  select.value = first.id;
  await openSource(first.id);
}

async function start() {
  await init();
  const link = await api.pendingLink(); // opened from a nexguard://pair link
  if (link) showPair({ ...link, canCancel: state.sources.some((x) => x.installed), autoContinue: true });
}

$('source').addEventListener('change', (e) => openSource(e.target.value));

let searchTimer;
$('search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = e.target.value.trim().toLowerCase();
    render();
  }, 120);
});
$('search').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    e.target.value = '';
    state.query = '';
    render();
  }
});

for (const b of document.querySelectorAll('.segments button')) {
  b.addEventListener('click', () => {
    for (const x of document.querySelectorAll('.segments button')) x.setAttribute('aria-checked', String(x === b));
    state.level = b.dataset.level;
    render();
  });
}

function setFollow(on) {
  state.follow = on;
  $('follow').setAttribute('aria-pressed', String(on));
  updateFooter(shown().length);
  if (on) $('log').scrollTop = $('log').scrollHeight;
}
$('follow').addEventListener('click', () => setFollow(!state.follow));
$('log').addEventListener('scroll', () => {
  const log = $('log');
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
  if (atBottom !== state.follow) setFollow(atBottom);
});

$('reveal').addEventListener('click', () => api.reveal());
$('export').addEventListener('click', () => api.exportText(plainText(shown())));

api.onEntries(({ id, entries }) => {
  if (id === state.id) append(entries);
});
api.onStatus((src) => {
  if (src.id === state.id) {
    renderStatus(src);
    showEmptyIfNeeded();
  }
});
api.onFocusSearch(() => $('search').focus());

// Keep "3 min ago" honest between status updates.
setInterval(() => state.source && renderStatus(state.source), 30000);

start();
