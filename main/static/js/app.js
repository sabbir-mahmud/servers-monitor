// Theme: "system" (default), "light" or "dark", remembered per browser. Runs in <head>
// so the right palette is applied before first paint. Bootstrap reads data-bs-theme.
(function () {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const read = () => { try { return localStorage.getItem("sm-theme") || "system"; } catch (e) { return "system"; } };
  const apply = () => {
    const pref = read();
    const mode = pref === "system" ? (mq.matches ? "dark" : "light") : pref;
    const root = document.documentElement;
    root.setAttribute("data-theme", mode);
    root.setAttribute("data-bs-theme", mode);
    document.querySelectorAll("[data-theme-set]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.themeSet === pref)));
  };
  apply();
  mq.addEventListener("change", apply);
  document.addEventListener("DOMContentLoaded", apply);
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-theme-set]");
    if (!btn) return;
    try { localStorage.setItem("sm-theme", btn.dataset.themeSet); } catch (err) { /* private mode */ }
    apply();
  });
})();

// Sparkline hover: crosshair + tooltip at the nearest sample. Re-bound after HTMX swaps.
function bindSparks(root) {
  root.querySelectorAll(".spark:not([data-bound])").forEach((fig) => {
    fig.dataset.bound = "1";
    const points = JSON.parse(fig.dataset.points || "[]");
    if (!points.length) return;
    const start = +fig.dataset.start, end = +fig.dataset.end;
    const plot = fig.querySelector(".spark-plot");
    const cursor = fig.querySelector(".spark-cursor");
    const tip = fig.querySelector(".spark-tip");
    plot.addEventListener("mousemove", (e) => {
      const rect = plot.getBoundingClientRect();
      const t = start + ((e.clientX - rect.left) / rect.width) * (end - start);
      let best = points[0];
      for (const p of points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      const x = ((best[0] - start) / (end - start)) * rect.width;
      cursor.style.left = x + "px";
      tip.style.left = Math.min(Math.max(x, 50), rect.width - 50) + "px";
      const unit = fig.dataset.unit || "%";
      const value = unit === "%" ? best[1].toFixed(1) + "%" : Math.round(best[1]).toLocaleString() + " " + unit;
      tip.textContent = new Date(best[0] * 1000).toLocaleString() + " · " + value;
      cursor.hidden = tip.hidden = false;
    });
    plot.addEventListener("mouseleave", () => { cursor.hidden = tip.hidden = true; });
  });
}
document.addEventListener("DOMContentLoaded", () => bindSparks(document));
document.addEventListener("htmx:afterSwap", (e) => bindSparks(e.target));

// Bulk-run form: show only the inputs the chosen action uses; select-all helpers.
document.addEventListener("change", (e) => {
  if (e.target.matches("#id_action")) toggleArgs(e.target.value);
  if (e.target.matches("[data-select-group]")) {
    const g = e.target.dataset.selectGroup;
    document.querySelectorAll(`.server-check input[data-groups~="${g}"]`).forEach((cb) => (cb.checked = e.target.checked));
  }
});
function toggleArgs(action) {
  const show = { tail_log: ["name", "lines"], restart_service: ["name"], shell: ["command"] }[action] || [];
  ["name", "lines", "command"].forEach((f) => {
    const row = document.querySelector(`[data-arg="${f}"]`);
    if (row) row.hidden = !show.includes(f);
  });
}
document.addEventListener("DOMContentLoaded", () => {
  const a = document.querySelector("#id_action");
  if (a) toggleArgs(a.value);
  document.querySelectorAll("[data-select-all]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const on = btn.dataset.selectAll === "1";
      document.querySelectorAll(".server-check input[type=checkbox]").forEach((cb) => (cb.checked = on));
    })
  );
});

// Copy buttons on the enrolment page.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.querySelector(btn.dataset.copy);
  const text = el.textContent.trim();
  const done = () => {
    const label = btn.querySelector("[data-copy-label]") || btn;
    const old = label.textContent; label.textContent = "Copied";
    setTimeout(() => (label.textContent = old), 1500);
  };
  // The Clipboard API only exists on https:// and localhost; a dashboard opened at a LAN
  // address over http falls back to selecting the text and copying it the old way.
  const fallback = () => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(range);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    if (ok) { sel.removeAllRanges(); done(); }  // else leave it selected for Ctrl/⌘ C
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
  else fallback();
});

// Connect a computer: show the steps for this visitor's system, with a switch for others.
(function () {
  const guess = () => {
    const p = ((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || navigator.userAgent).toLowerCase();
    if (p.includes("win")) return "windows";
    if (p.includes("mac")) return "macos";
    if (p.includes("linux") || p.includes("x11")) return "linux";
    return null;
  };
  const choose = (box, os) => {
    if (!box.querySelector(`[data-os-panel="${os}"]`)) return;
    box.querySelectorAll("[data-os]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.os === os)));
    box.querySelectorAll("[data-os-panel]").forEach((p) => { p.hidden = p.dataset.osPanel !== os; });
  };
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-os]");
    const box = btn && btn.closest("[data-os-tabs]");
    if (box) choose(box, btn.dataset.os);
  });
  document.addEventListener("DOMContentLoaded", () => {
    const os = guess();
    if (os) document.querySelectorAll("[data-os-tabs]").forEach((box) => { if (box.querySelector("[data-os]")) choose(box, os); });
  });
})();

// Sign-in: show/hide password, Caps Lock hint, and a busy state so a slow submit isn't double-clicked.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-toggle-password]");
  if (!btn) return;
  const input = document.querySelector(btn.dataset.togglePassword);
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  btn.setAttribute("aria-pressed", String(show));
  btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
  btn.querySelector('[data-when="hidden"]').hidden = show;
  btn.querySelector('[data-when="shown"]').hidden = !show;
  input.focus();
});
["keydown", "keyup"].forEach((type) =>
  document.addEventListener(type, (e) => {
    const hint = document.getElementById("caps-hint");
    if (hint && e.getModifierState) hint.hidden = !e.getModifierState("CapsLock");
  })
);
document.addEventListener("submit", (e) => {
  const form = e.target.closest("[data-loading-form]");
  if (!form) return;
  const btn = form.querySelector("button:not([type=button])");
  btn.classList.add("is-loading");
  btn.setAttribute("aria-busy", "true");
  const label = btn.querySelector(".login-submit-label");
  if (label) label.textContent = "Signing in…";
});

// Forms that change something irreversible ask first.
document.addEventListener("submit", (e) => {
  const form = e.target.closest("form[data-confirm]");
  if (form && !window.confirm(form.dataset.confirm)) e.preventDefault();
});

// Server page: sticky jump links. The active link is the last section whose top has passed
// under the nav, so it's always the section you're reading (and the last one at page end).
document.addEventListener("DOMContentLoaded", () => {
  const nav = document.querySelector("[data-jump-nav]");
  if (!nav) return;
  const links = [...nav.querySelectorAll("a[href^='#']")];
  let ticking = false;
  const update = () => {
    ticking = false;
    if (pinned) return;
    // Past scroll-margin-top (4.75rem), so a section you jumped to counts as current.
    const line = nav.getBoundingClientRect().bottom + 40;
    let current = links[0];
    for (const a of links) {
      const el = document.querySelector(a.getAttribute("href"));
      if (el && el.getBoundingClientRect().top <= line) current = a;
    }
    links.forEach((a) => a.classList.toggle("active", a === current));
    // Keep the active link visible in the nav on narrow screens (sideways only).
    const l = current.offsetLeft, r = l + current.offsetWidth;
    if (l < nav.scrollLeft || r > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = l - 16;
  };
  const queue = () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } };
  // Room after the last section so even a short one can scroll up under the nav; then
  // every link (About included) lands its section at the top and lights up correctly.
  const stack = document.querySelector(".stack");
  const last = document.querySelector(links[links.length - 1].getAttribute("href"));
  const pad = () => {
    if (!stack || !last) return;
    const margin = parseFloat(getComputedStyle(last).scrollMarginTop) || 0;
    stack.style.paddingBottom = Math.max(0, window.innerHeight - margin - last.offsetHeight - 24) + "px";
  };
  pad();
  window.addEventListener("resize", pad);
  document.addEventListener("htmx:afterSettle", pad);
  // A clicked link is current right away, not after the smooth scroll catches up.
  let pinned = null;
  nav.addEventListener("click", (e) => {
    const a = e.target.closest("a[href^='#']");
    if (!a) return;
    pinned = a;
    links.forEach((l) => l.classList.toggle("active", l === a));
    clearTimeout(nav._unpin);
    nav._unpin = setTimeout(() => { pinned = null; queue(); }, 1000);
  });
  window.addEventListener("scroll", queue, { passive: true });
  window.addEventListener("resize", queue);
  document.addEventListener("htmx:afterSettle", queue);
  update();
  const sentinel = document.createElement("div");
  nav.before(sentinel);
  new IntersectionObserver(([en]) => nav.classList.toggle("is-stuck", !en.isIntersecting)).observe(sentinel);
});

// Process list: CPU / Memory order. The choice lives on the panel (outside the part that
// refreshes every 10 s), so it survives each refresh.
(function () {
  const sync = (panel) => panel.querySelectorAll("[data-sort-set]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.sortSet === panel.dataset.sort)));
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-sort-set]");
    const panel = btn && btn.closest("[data-sort]");
    if (!panel) return;
    panel.dataset.sort = btn.dataset.sortSet;
    sync(panel);
  });
  document.addEventListener("htmx:afterSettle", () => document.querySelectorAll("[data-sort]").forEach(sync));
})();

// "Use this chat" buttons fill a form field (Telegram chat discovery).
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-fill]");
  if (!btn) return;
  const input = document.querySelector(btn.dataset.fill);
  if (!input) return;
  input.value = btn.dataset.value;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  input.focus();
  btn.closest("[data-chat-list]")?.querySelectorAll("[data-fill]").forEach((b) => b.classList.toggle("active", b === btn));
});

// Monitor form: show only the fields for the chosen type, and relabel the target field.
(function () {
  function apply(form) {
    const select = form.querySelector("[data-kind-select]");
    if (!select) return;
    const kind = select.value;
    form.querySelectorAll("[data-kinds]").forEach((el) => {
      const show = el.dataset.kinds.split(" ").includes(kind);
      const box = el.matches("input, select, textarea") ? el.closest(".form-field") : el;
      if (box) box.hidden = !show;
    });
    form.querySelectorAll("[data-kind-card]").forEach((card) => {
      card.hidden = !card.querySelector(".form-field:not([hidden])");
    });
    const target = form.querySelector("[name=target]");
    const label = form.querySelector("[data-target-label]");
    if (target && label) {
      label.textContent = target.dataset["label" + kind[0].toUpperCase() + kind.slice(1)] || "Host";
      target.placeholder = target.dataset["placeholder" + kind[0].toUpperCase() + kind.slice(1)] || "";
    }
    const echo = (target && target.value) || "nightly-backup";
    form.querySelectorAll("[data-target-echo]").forEach((el) => { el.textContent = echo; });
  }
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-kind-form]").forEach((form) => {
      apply(form);
      form.addEventListener("change", () => apply(form));
      form.addEventListener("input", (e) => { if (e.target.name === "target") apply(form); });
    });
  });
})();

// Rollout options only matter for rolling jobs.
(function () {
  const sync = () => document.querySelectorAll("[data-rollout]").forEach((box) => {
    const on = box.querySelector("[data-rolling-toggle]")?.checked;
    box.querySelectorAll("[data-rolling-only]").forEach((el) => { el.hidden = !on; });
  });
  document.addEventListener("DOMContentLoaded", sync);
  document.addEventListener("change", (e) => { if (e.target.matches("[data-rolling-toggle]")) sync(); });
})();

// Installable app: register the service worker (offline page + push alerts) and offer
// "Install app" where the browser supports it. Service workers need HTTPS (or localhost).
(function () {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  window.addEventListener("load", () => {
    const url = document.body.dataset.sw;
    if (url) navigator.serviceWorker.register(url, { scope: "/" }).catch(() => { /* optional */ });
  });
  let deferred = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferred = e;
    document.querySelectorAll("[data-install-app]").forEach((b) => { b.hidden = false; });
  });
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-install-app]");
    if (!btn || !deferred) return;
    deferred.prompt();
    await deferred.userChoice;
    deferred = null;
    btn.hidden = true;
  });
})();

// Browser push channel: subscribe this browser and fill the form's hidden fields.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-push-subscribe]");
  if (!btn) return;
  const status = document.querySelector("[data-push-status]");
  const say = (text, ok) => { if (status) { status.textContent = text; status.className = ok ? "text-good small" : "text-critical small"; } };
  if (!window.isSecureContext || !("serviceWorker" in navigator) || !("PushManager" in window)) {
    say("This browser can't receive push here. Push needs HTTPS (or localhost) and a supporting browser.", false);
    return;
  }
  if ((await Notification.requestPermission()) !== "granted") {
    say("Notifications are blocked for this site. Allow them in the browser's site settings.", false);
    return;
  }
  try {
    const reg = await navigator.serviceWorker.register(document.body.dataset.sw, { scope: "/" });
    await navigator.serviceWorker.ready;
    const raw = atob(btn.dataset.vapid.replace(/-/g, "+").replace(/_/g, "/"));
    const key = Uint8Array.from(raw, (c) => c.charCodeAt(0));
    const sub = (await reg.pushManager.getSubscription())
      || (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }));
    const json = sub.toJSON();
    document.getElementById("id_cfg_endpoint").value = json.endpoint;
    document.getElementById("id_cfg_p256dh").value = json.keys.p256dh;
    document.getElementById("id_cfg_auth").value = json.keys.auth;
    say("This browser is subscribed. Save the channel, then send a test.", true);
  } catch (err) {
    say("Couldn't subscribe: " + err.message, false);
  }
});

// Add/edit forms: a live avatar while typing a name, and a suggested device name
// ("Sara's iPhone") that follows the choices until the user types their own.
document.addEventListener("DOMContentLoaded", () => {
  const initials = (s) => s.trim().split(/\s+/).slice(0, 2).map((w) => w[0] || "").join("").toUpperCase() || "?";
  const preview = document.querySelector("[data-avatar-preview]");
  const nameInput = preview && preview.closest("form").querySelector("input[name=name]");
  if (preview && nameInput) nameInput.addEventListener("input", () => { preview.textContent = initials(nameInput.value); });

  const form = document.querySelector("form[data-name-suggest]");
  if (!form) return;
  const name = form.querySelector("input[name=name]");
  let nouns = {};
  try { nouns = JSON.parse(form.dataset.nouns || "{}"); } catch (e) { /* keep empty */ }
  let suggested = "";
  let touched = name.value.trim() !== "";
  const suggest = () => {
    const platform = form.querySelector("input[name=platform]:checked");
    const person = form.querySelector("input[name=person]:checked");
    const select = form.querySelector("select[name=person]");
    let who = "";
    if (person && person.value) who = person.closest("label").querySelector("[data-person-name]").textContent.trim();
    else if (select && select.value) who = select.options[select.selectedIndex].text.trim();
    const noun = platform ? nouns[platform.value] || "device" : "device";
    const owner = who ? (who.split(/\s+/)[0] + (/s$/i.test(who.split(/\s+/)[0]) ? "'" : "'s")) : form.dataset.sharedPrefix;
    const next = `${owner} ${noun}`;
    name.placeholder = next;
    if (!touched || name.value === suggested) { name.value = next; touched = false; }
    suggested = next;
  };
  name.addEventListener("input", () => { touched = name.value !== suggested; });
  form.addEventListener("change", (e) => { if (e.target.name === "platform" || e.target.name === "person") suggest(); });
  if (!touched) suggest(); else suggested = "";
});

// Server form: interval presets fill the number box; the matching preset stays highlighted.
document.addEventListener("DOMContentLoaded", () => {
  const presets = document.querySelectorAll("[data-interval]");
  if (!presets.length) return;
  const input = document.getElementById(presets[0].dataset.target);
  const sync = () => presets.forEach((b) => b.classList.toggle("is-active", b.dataset.interval === String(input.value)));
  presets.forEach((b) => b.addEventListener("click", () => { input.value = b.dataset.interval; sync(); }));
  input.addEventListener("input", sync);
  sync();
});

// Device controls: the header tiles and list counts follow the switches as they change, the
// save bar says how many changes aren't saved yet (Save stays off until there are some),
// Undo puts everything back, ⌘S/Ctrl+S saves, and leaving with unsaved changes asks first.
(function () {
  const valueOf = (el) => (el.type === "checkbox" ? el.checked : el.value.trim());
  const lines = (text) => text.split(/\n/).map((l) => l.trim()).filter(Boolean).length;

  function bind(form) {
    const fields = [...form.querySelectorAll("input[type=checkbox][name], textarea[name]")];
    const start = new Map(fields.map((f) => [f, valueOf(f)]));
    const alwaysDirty = form.hasAttribute("data-dirty"); // re-shown after a failed save
    const note = form.querySelector("[data-dirty-note]");
    const noteText = note && note.querySelector("span");
    const save = form.querySelector("[data-save]");
    const undo = form.querySelector(".ctl-undo");
    const on = (names) => names.split(" ").some((n) => {
      const el = form.elements[n];
      return el && (el.type === "checkbox" ? el.checked : lines(el.value) > 0);
    });
    let dirty = 0;

    const update = () => {
      dirty = fields.filter((f) => valueOf(f) !== start.get(f)).length;
      const isDirty = dirty > 0 || alwaysDirty;
      form.classList.toggle("is-dirty", isDirty);
      if (save) save.disabled = !isDirty;
      if (undo) undo.hidden = dirty === 0;
      if (noteText) noteText.textContent = dirty
        ? `${dirty} unsaved change${dirty === 1 ? "" : "s"}`
        : note.dataset.clean;
      document.querySelectorAll("[data-mirror]").forEach((el) => {
        el.textContent = on(el.dataset.mirror) ? "On" : "Off";
      });
      document.querySelectorAll("[data-mirror-icon]").forEach((el) =>
        el.classList.toggle("is-good", on(el.dataset.mirrorIcon)));
      document.querySelectorAll("[data-line-count]").forEach((el) => {
        const t = form.elements[el.dataset.lineCount];
        const n = t ? lines(t.value) : 0;
        el.textContent = n ? String(n) : "";
      });
    };

    form.addEventListener("input", update);
    form.addEventListener("change", update);
    form.addEventListener("reset", () => setTimeout(update)); // values change after the event
    let submitting = false;
    form.addEventListener("submit", () => { submitting = true; });
    window.addEventListener("beforeunload", (e) => {
      if (dirty && !submitting) { e.preventDefault(); e.returnValue = ""; }
    });
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s" && save && !save.disabled) {
        e.preventDefault();
        form.requestSubmit(save);
      }
    });
    update();
  }

  document.addEventListener("DOMContentLoaded", () =>
    document.querySelectorAll("form[data-controls-form]").forEach(bind));
})();
