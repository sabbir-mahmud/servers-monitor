{% load static %}// Server Monitor service worker {{ version }}: offline page, cached static files, push alerts.
// Dashboard pages and data are never cached: they need you signed in and must be fresh.
const CACHE = "sm-{{ version }}";
const OFFLINE = "{% url 'offline' %}";
const ASSETS = [OFFLINE, "{% static 'css/app.css' %}", "{% static 'vendor/bootstrap.min.css' %}", "{% static 'js/app.js' %}",
  "{% static 'img/icon.svg' %}", "{% static 'img/icon-192.png' %}"];
const ICON = "{% static 'img/icon-192.png' %}";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  if (new URL(req.url).pathname.startsWith("{% get_static_prefix %}")) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "{{ SITE_NAME|escapejs }}", {
    body: data.body || "",
    tag: data.tag || undefined,
    renotify: Boolean(data.tag),
    requireInteraction: data.severity === "critical",
    icon: ICON,
    badge: ICON,
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data.url || "/", self.location.origin);
  if (target.origin !== self.location.origin) return;  // only ever open this dashboard
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    for (const w of wins) if (w.url === target.href && "focus" in w) return w.focus();
    return self.clients.openWindow(target.href);
  }));
});
