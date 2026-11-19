// The browser terminal: xterm.js on the page, SSH on the dashboard (apps/operations/ssh.py).
// Output: long-poll /read (the server answers as soon as there's something, or after 20 s).
// Input: keystrokes batched for a few milliseconds and posted to /write.
(function () {
  const app = document.getElementById('ssh-app');
  if (!app) return;
  const $ = (id) => document.getElementById(id);
  const form = $('ssh-form'), errorBox = $('ssh-error'), connectBtn = $('ssh-connect');
  const sessionBox = $('ssh-session'), stateEl = $('ssh-state');
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

  const post = (url, body) => fetch(url, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
    body: JSON.stringify(body),
  });

  function showError(text) {
    errorBox.textContent = text;
    errorBox.hidden = !text;
  }

  // The terminal, sized to its box.
  const term = new Terminal({
    cursorBlink: true, fontSize: 13, scrollback: 5000,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    theme: { background: '#0d0f12', foreground: '#e6e6e6', cursor: '#7aa2ff' },
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);

  let sid = null, offset = 0, closed = false, pending = '', flushTimer = null;
  const urls = {};

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('');
    connectBtn.disabled = true;
    connectBtn.textContent = 'Connecting…';
    sessionBox.hidden = false;  // measure the box so the remote shell starts at the right size
    if (!term.element) term.open($('ssh-term'));
    fit.fit();
    sessionBox.hidden = true;
    const body = {
      server: pick ? pick.value : '', host: form.host.value.trim(), port: form.port.value,
      username: form.username.value.trim(), cols: term.cols, rows: term.rows,
    };
    if (auth === 'password') body.password = form.password.value;
    else { body.private_key = form.private_key.value; body.passphrase = form.passphrase.value; }
    let reply;
    try {
      const resp = await post(app.dataset.open, body);
      reply = await resp.json().catch(() => ({ error: `The dashboard answered HTTP ${resp.status}.` }));
    } catch {
      reply = { error: "Couldn't reach the dashboard." };
    }
    // Secrets leave the page as soon as they've been used.
    form.password.value = ''; form.passphrase.value = '';
    connectBtn.disabled = false;
    connectBtn.innerHTML = connectLabel;
    if (reply.error || !reply.id) {
      showError(reply.error || 'The connection failed.');
      return;
    }
    form.private_key.value = '';
    start(reply);
  });

  function start(reply) {
    sid = reply.id; offset = 0; closed = false;
    const base = app.dataset.base + 's/' + encodeURIComponent(sid) + '/';
    urls.read = base + 'read/'; urls.write = base + 'write/'; urls.close = base + 'close/';
    $('ssh-label').textContent = reply.label;
    form.hidden = true;
    sessionBox.hidden = false;
    sessionBox.classList.remove('is-closed');
    stateEl.textContent = 'Connected';
    term.reset();
    fit.fit();
    term.focus();
    poll();
  }

  async function poll() {
    while (sid && !closed) {
      let r;
      try {
        const resp = await fetch(`${urls.read}?offset=${offset}`, { credentials: 'same-origin' });
        if (resp.status === 404) { end('The session ended.'); return; }
        r = await resp.json();
      } catch {
        stateEl.textContent = 'Reconnecting…';
        await new Promise((ok) => setTimeout(ok, 1500));
        continue;
      }
      stateEl.textContent = 'Connected';
      if (r.data) term.write(Uint8Array.from(atob(r.data), (c) => c.charCodeAt(0)));
      offset = r.offset;
      if (r.closed) { end(r.reason || 'The connection closed.'); return; }
    }
  }

  function end(reason) {
    closed = true;
    sessionBox.classList.add('is-closed');
    stateEl.textContent = reason;
    term.write(`\r\n\x1b[90m[${reason}]\x1b[0m\r\n`);
    $('ssh-close').textContent = 'New connection';
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

  $('ssh-close').addEventListener('click', async () => {
    if (sid && !closed) {
      await post(urls.close, {}).catch(() => {});
      end('You disconnected.');
    }
    // Back to the form for another connection.
    sid = null;
    sessionBox.hidden = true;
    sessionBox.classList.remove('is-full');
    document.body.classList.remove('ssh-fullscreen');
    form.hidden = false;
    $('ssh-close').textContent = 'Disconnect';
    form.password.focus();
  });

  // Leaving the page closes the session (the server would also close it after a minute).
  window.addEventListener('pagehide', () => {
    if (!sid || closed) return;
    fetch(urls.close, {
      method: 'POST', keepalive: true, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: '{}',
    }).catch(() => {});
  });
})();
