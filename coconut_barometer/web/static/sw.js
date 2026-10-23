// The service worker, served at /sw.js so it covers the whole site. It keeps the app for offline use, shows Web
// Push notifications (Settings → Alerts) and opens the page an alert is about when one is tapped.
//
// What it keeps (one cache):
//   the page      from the network when there is one (and kept), else the kept copy: the app opens offline.
//                 Every address serves the same page (only its link-preview tags differ), so one copy does.
//   /static/v/    the JS and CSS at a version: never change (assets.py), so kept for good and served from here.
//                 When the page names a new version, all of its files are fetched (/sw-precache.json) so every
//                 page opens offline, not just the ones visited, and the old version's are dropped.
//   /static/      images, the font, the manifest: the kept copy at once, refreshed behind it.
// Never: /api/ (data, the login), /sw.js itself, or anything from another site (Flickr's photos, map tiles).
const CACHE = "coconut-barometer";
const PAGE = "/";
const VERSION = /\/static\/v\/([0-9a-f]+)\//;
const DONE = "/sw-precache.json";        // kept once every file of its version is: which version that is

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim().then(() => refreshPage().catch(() => {}))));

self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (req.mode === "navigate") return e.respondWith(page(req, e));
  if (url.pathname.startsWith("/static/v/")) return e.respondWith(forGood(req));
  if (url.pathname.startsWith("/static/")) return e.respondWith(keptThenFresh(req, e));
});

async function page(req, e) {
  try {
    const res = await fetch(req);
    if (res.ok) e.waitUntil(keepPage(res.clone()));
    return res;
  } catch {
    return (await caches.match(PAGE)) || new Response("You're offline, and the app hasn't been kept on this device yet.",
      { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }
}

async function refreshPage() {
  const res = await fetch(PAGE, { cache: "no-cache" });
  if (res.ok) await keepPage(res);
}

// Keep the page; if it names a version the cache doesn't have yet, fetch all of that version and drop the others.
async function keepPage(res) {
  const cache = await caches.open(CACHE), text = await res.clone().text();
  await cache.put(PAGE, res);
  const v = text.match(VERSION)?.[1], done = await cache.match(DONE);
  if (!v || (done && (await done.json()).version === v)) return;
  const list = await (await fetch(DONE, { cache: "no-cache" })).json();
  if (list.version !== v) return;          // the server moved on again since: the next page will do it
  await cache.addAll(list.files);          // all or nothing: a failure leaves DONE as it was, to try again
  await cache.put(DONE, new Response(JSON.stringify(list), { headers: { "Content-Type": "application/json" } }));
  for (const key of await cache.keys()) {
    const kept = new URL(key.url).pathname.match(VERSION)?.[1];
    if (kept && kept !== v) await cache.delete(key);
  }
}

async function forGood(req) {
  const kept = await caches.match(req);
  if (kept) return kept;
  const res = await fetch(req);
  if (res.ok) (await caches.open(CACHE)).put(req, res.clone());
  return res;
}

async function keptThenFresh(req, e) {
  const kept = await caches.match(req, { ignoreSearch: true });
  const fresh = fetch(req).then(async (res) => {
    if (res.ok) await (await caches.open(CACHE)).put(req, res.clone());
    return res;
  });
  if (kept) { e.waitUntil(fresh.catch(() => {})); return kept; }
  return fresh;
}

// ---- notifications ------------------------------------------------------------------------------------------
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data?.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "Coconut Barometer", {
    body: d.body || "", tag: d.tag || undefined, data: { url: d.url || "/" },
    icon: "/static/img/icon-192.png", badge: "/static/img/favicon-32.png",
  }));
});

// Tapping one: the app's open window, moved to the alert's page, or a new one there.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const open = (await self.clients.matchAll({ type: "window", includeUncontrolled: true }))
      .find((c) => new URL(c.url).origin === self.location.origin);
    if (!open) return self.clients.openWindow(url);
    await open.focus();
    return open.navigate(url).catch(() => open.postMessage({ open: url }));
  })());
});
