// The browser terminal: xterm.js on the page, SSH on the dashboard (apps/operations/ssh.py).
// Output: long-poll /read (the server answers as soon as there's something, or after 20 s).
// Input: keystrokes batched for a few milliseconds and posted to /write.
// Saved logins: the page only ever holds their id; the secret never comes back here.
(function () {
  const app = document.getElementById('ssh-app');
  if (!app) return;
  const $ = (id) => document.getElementById(id);
  const form = $('ssh-form'), errorBox = $('ssh-error'), connectBtn = $('ssh-connect');
  const setup = $('ssh-setup'), sessionBox = $('ssh-session'), stateEl = $('ssh-state');
  const noteEl = $('ssh-note'), timerEl = $('ssh-timer'), reconnectBtn = $('ssh-reconnect');
  const closeText = $('ssh-close-text');
  const csrf = (JSON.parse(document.body.getAttribute('hx-headers') || '{}'))['X-CSRFToken'];
  let auth = 'password';
  const connectLabel = connectBtn.innerHTML;

  // Pick a server: fill in what was used last time.
  const pick = form.querySelector('[data-pick]');
  if (pick) pick.addEventListener('change', () => {
    const o = pick.selectedOptions[0];
    if (!o || !o.value) return;
    form.host.value = o.dataset.host || '';
    form.port.value = o.dataset.port || 22;
    form.username.value = o.dataset.user || form.username.value;
  });

  form.querySelectorAll('[data-auth]').forEach((btn) => btn.addEventListener('click', () => {
    auth = btn.dataset.auth;
    form.querySelectorAll('[data-auth]').forEach((b) => {
      const on = b === btn;
      b.classList.toggle('active', on);
      b.setAttribute('aria-pressed', on);
    });
    form.querySelectorAll('[data-auth-panel]').forEach((p) => { p.hidden = p.dataset.authPanel !== auth; });
  }));
  $('ssh-key-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (file && file.size < 64 * 1024) form.private_key.value = await file.text();
  });
  form.save.addEventListener('change', () => { $('ssh-save-name').hidden = !form.save.checked; });

  const post = (url, body) => fetch(url, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
    body: JSON.stringify(body),
  });

  function showError(text) {
    errorBox.textContent = text;
    errorBox.hidden = !text;
    if (text) errorBox.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  function note(text, warn) {
    noteEl.textContent = text || '';
    noteEl.classList.toggle('is-warn', !!warn);
  }

  // The terminal, sized to its box.
  const term = new Terminal({
    cursorBlink: true, fontSize: 13, scrollback: 5000,
    fontFamily: 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace',
    theme: { background: getComputedStyle(sessionBox).backgroundColor || '#15171c', foreground: '#e6e6e6', cursor: '#7aa2ff' },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);

  let sid = null, offset = 0, closed = false, pending = '', flushTimer = null;
  let login = null;  // {id, name} of the saved login in use, for Reconnect
  let startedAt = 0, ticker = null;
  const urls = {};

  // Measure the terminal box so the remote shell starts at the right size.
  function measure() {
    const wasHidden = sessionBox.hidden;
    sessionBox.hidden = false;
    if (!term.element) term.open($('ssh-term'));
    fit.fit();
    sessionBox.hidden = wasHidden;
    return { cols: term.cols, rows: term.rows };
  }

  async function connect(body, busy) {
    showError('');
    busy(true);
    let reply;
    try {
      const resp = await post(app.dataset.open, { ...body, ...measure() });
      reply = await resp.json().catch(() => ({ error: `The dashboard answered HTTP ${resp.status}.` }));
    } catch {
      reply = { error: "Couldn't reach the dashboard." };
    }
    busy(false);
    if (reply.error || !reply.id) {
      showError(reply.error || 'The connection failed.');
      return null;
    }
    start(reply);
    return reply;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      server: pick ? pick.value : '', host: form.host.value.trim(), port: form.port.value,
      username: form.username.value.trim(),
      save: form.save.checked, name: form.elements.login_name.value.trim(),
    };
    if (auth === 'password') body.password = form.password.value;
    else { body.private_key = form.private_key.value; body.passphrase = form.passphrase.value; }
    const reply = await connect(body, (on) => {
      connectBtn.disabled = on;
      if (on) connectBtn.textContent = 'Connecting…'; else connectBtn.innerHTML = connectLabel;
    });
    // Secrets leave the page as soon as they've been used.
    form.password.value = ''; form.passphrase.value = '';
    if (!reply) return;
    form.private_key.value = '';
    if (reply.saved) {
      note(`Saved as “${reply.saved.name}”`);
      addSaved(reply.saved, reply.label);
    } else if (reply.save_error) {
      note(`Not saved: ${reply.save_error}`, true);
    }
  });

  // One click on a saved login.
  function useSaved(btn) {
    return connect({ login: btn.dataset.login }, (on) => {
      app.querySelectorAll('.ssh-login-go').forEach((b) => { b.disabled = on; });
      btn.setAttribute('aria-busy', on);
    });
  }
  app.addEventListener('click', (e) => {
    const btn = e.target.closest('.ssh-login-go');
    if (btn) useSaved(btn);
  });

  // A just-saved login appears in the list without a reload (removing it needs one).
  function addSaved(saved, label) {
    if (app.querySelector(`.ssh-login-go[data-login="${saved.id}"]`)) return;
    let list = app.querySelector('.ssh-login-list');
    if (!list) {
      list = document.createElement('ul');
      list.className = 'ssh-login-list';
      list.setAttribute('role', 'list');
      const empty = app.querySelector('.ssh-saved-empty');
      empty.replaceWith(list);
    }
    const li = document.createElement('li');
    li.className = 'ssh-login';
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'ssh-login-go'; btn.dataset.login = saved.id;
    btn.innerHTML = '<span class="ssh-login-mark" aria-hidden="true">✓</span><span class="ssh-login-text">'
      + '<span class="ssh-login-name"></span><span class="ssh-login-target mono"></span>'
      + '<span class="ssh-login-meta">Saved just now</span></span>';
    btn.querySelector('.ssh-login-name').textContent = saved.name;
    btn.querySelector('.ssh-login-target').textContent = label;
    li.appendChild(btn);
    list.prepend(li);
  }

  function start(reply) {
    sid = reply.id; offset = 0; closed = false;
    login = reply.login || null;
    const base = app.dataset.base + 's/' + encodeURIComponent(sid) + '/';
    urls.read = base + 'read/'; urls.write = base + 'write/'; urls.close = base + 'close/';
    $('ssh-label').textContent = reply.label;
    note(login && !reply.saved ? `Saved login “${login.name}”` : '');
    setup.hidden = true;
    sessionBox.hidden = false;
    sessionBox.classList.remove('is-closed');
    reconnectBtn.hidden = true;
    closeText.textContent = 'Disconnect';
    stateEl.textContent = 'Connected';
    startedAt = Date.now();
    clearInterval(ticker);
    ticker = setInterval(tick, 1000);
    tick();
    term.reset();
    fit.fit();
    term.focus();
    poll();
  }

  function tick() {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    timerEl.textContent = (h ? `${h}:${String(m).padStart(2, '0')}` : m) + ':' + String(sec).padStart(2, '0');
  }

  async function poll() {
    const mine = sid;
    while (sid === mine && !closed) {
      let r;
      try {
        const resp = await fetch(`${urls.read}?offset=${offset}`, { credentials: 'same-origin' });
        if (resp.status === 404) { end('The session ended.'); return; }
        r = await resp.json();
      } catch {
        stateEl.textContent = 'Reconnecting…';
        sessionBox.classList.add('is-connecting');
        await new Promise((ok) => setTimeout(ok, 1500));
        continue;
      }
      if (sid !== mine) return;
      sessionBox.classList.remove('is-connecting');
      stateEl.textContent = 'Connected';
      if (r.data) term.write(Uint8Array.from(atob(r.data), (c) => c.charCodeAt(0)));
      offset = r.offset;
      if (r.closed) { end(r.reason || 'The connection closed.'); return; }
    }
  }

  function end(reason) {
    closed = true;
    clearInterval(ticker);
    sessionBox.classList.add('is-closed');
    sessionBox.classList.remove('is-connecting');
    stateEl.textContent = reason;
    term.write(`\r\n\x1b[90m[${reason}]\x1b[0m\r\n`);
    closeText.textContent = 'New connection';
    reconnectBtn.hidden = !login;  // typed-in secrets are gone: only a saved login can redial
  }

  term.onData((data) => {
    if (!sid || closed) return;
    pending += data;
    if (!flushTimer) flushTimer = setTimeout(flush, 8);
  });
  function flush() {
    flushTimer = null;
    if (!pending || !sid) return;
    const data = pending; pending = '';
    post(urls.write, { data }).catch(() => {});
  }

  let resizeTimer = null;
  function refit() {
    if (sessionBox.hidden) return;
    fit.fit();
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (sid && !closed) post(urls.write, { cols: term.cols, rows: term.rows }); }, 150);
  }
  window.addEventListener('resize', refit);

  $('ssh-full').addEventListener('click', () => {
    sessionBox.classList.toggle('is-full');
    document.body.classList.toggle('ssh-fullscreen', sessionBox.classList.contains('is-full'));
    refit();
    term.focus();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && e.shiftKey && sessionBox.classList.contains('is-full')) $('ssh-full').click();
  });

  reconnectBtn.addEventListener('click', () => {
    if (!login) return;
    connect({ login: login.id }, (on) => {
      reconnectBtn.disabled = on;
      stateEl.textContent = on ? 'Connecting…' : stateEl.textContent;
      sessionBox.classList.toggle('is-connecting', on);
    }).then((reply) => {
      if (!reply) {  // show why, on the setup screen
        backToSetup();
      }
    });
  });

  function backToSetup() {
    sid = null;
    clearInterval(ticker);
    sessionBox.hidden = true;
    sessionBox.classList.remove('is-full', 'is-closed', 'is-connecting');
    document.body.classList.remove('ssh-fullscreen');
    setup.hidden = false;
  }

  $('ssh-close').addEventListener('click', async () => {
    if (sid && !closed) {
      await post(urls.close, {}).catch(() => {});
      end('You disconnected.');
      return;  // stay on the closed terminal: Reconnect, or New connection
    }
    backToSetup();
    (login ? app.querySelector('.ssh-login-go') : form.password)?.focus();
  });

  // Leaving the page closes the session (the server would also close it after a minute).
  window.addEventListener('pagehide', () => {
    if (!sid || closed) return;
    fetch(urls.close, {
      method: 'POST', keepalive: true, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: '{}',
    }).catch(() => {});
  });

  // Arrived from a server's SSH menu with a saved login: connect straight away, and drop
  // it from the address so a reload doesn't silently redial.
  if (app.dataset.auto) {
    const url = new URL(location.href);
    url.searchParams.delete('login');
    history.replaceState(null, '', url);
    const btn = app.querySelector(`.ssh-login-go[data-login="${app.dataset.auto}"]`);
    if (btn) useSaved(btn);
  }
})();
