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
  navigator.clipboard.writeText(el.textContent.trim()).then(() => {
    const label = btn.querySelector("[data-copy-label]") || btn;
    const old = label.textContent; label.textContent = "Copied";
    setTimeout(() => (label.textContent = old), 1500);
  });
});

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

// Server page: highlight the section in view and add a hairline once the nav sticks.
document.addEventListener("DOMContentLoaded", () => {
  const nav = document.querySelector(".section-nav");
  if (!nav) return;
  const links = [...nav.querySelectorAll("a[href^='#']")];
  const setActive = (id) => links.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + id));
  const visible = new Map();
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => visible.set(en.target.id, en.isIntersecting ? en.intersectionRatio : 0));
    const best = links.map((a) => a.getAttribute("href").slice(1)).find((id) => visible.get(id) > 0);
    if (best) setActive(best);
  }, { rootMargin: "-80px 0px -55% 0px", threshold: [0, 0.01] });
  // Sections inside HTMX partials are replaced on refresh, so re-observe by id after swaps.
  const observe = () => { io.disconnect(); links.forEach((a) => { const el = document.querySelector(a.getAttribute("href")); if (el) io.observe(el); }); };
  observe();
  document.addEventListener("htmx:afterSettle", observe);
  const sentinel = document.createElement("div");
  nav.before(sentinel);
  new IntersectionObserver(([en]) => nav.classList.toggle("is-stuck", !en.isIntersecting)).observe(sentinel);
});

// Sidebar categories: remember which ones were collapsed, but never hide the current page.
(function () {
  const KEY = "sm-nav-collapsed";
  const read = () => { try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; } };
  const write = (list) => { try { localStorage.setItem(KEY, JSON.stringify(list)); } catch (e) { /* private mode */ } };
  document.addEventListener("DOMContentLoaded", () => {
    const collapsed = new Set(read());
    document.querySelectorAll("details[data-nav-group]").forEach((group) => {
      if (collapsed.has(group.dataset.navGroup) && !group.hasAttribute("data-active")) group.open = false;
      group.addEventListener("toggle", () => {
        const now = new Set(read());
        group.open ? now.delete(group.dataset.navGroup) : now.add(group.dataset.navGroup);
        write([...now]);
      });
    });
  });
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
