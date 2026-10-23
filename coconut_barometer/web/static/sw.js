// The service worker, served at /sw.js so it covers the whole site. For now it shows Web Push notifications
// (Settings → Alerts) and opens the page an alert is about when one is tapped; it caches nothing yet.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

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
