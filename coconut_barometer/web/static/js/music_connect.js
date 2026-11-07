// /api/music/connect: Apple's sign-in (web/api/music.py). A plain script, so it needs none of the app's modules on a
// page whose headers are looser than the rest of the site's.
(() => {
  const meta = (name) => document.querySelector(`meta[name="${name}"]`).content;
  const $ = (id) => document.getElementById(id);
  const say = (text) => { $("note").textContent = text; };
  let music = null;

  // Set up ahead of the click: a popup opened after waiting on something else gets blocked.
  async function ready() {
    try {
      music = await window.MusicKit.configure({ developerToken: meta("cb-developer-token"), app: { name: "Coconut Barometer", build: "1" } });
      $("go").disabled = false;
      say("");
    } catch (e) { say(`Apple's sign-in wouldn't start: ${e.message || e}. Check the Team ID, Key ID and private key in Settings.`); }
  }

  async function connect(e) {
    e.preventDefault();
    $("go").disabled = true;
    say("Waiting for Apple…");
    try {
      const token = await music.authorize();
      const r = await fetch("/api/music/connect", { method: "POST", body: JSON.stringify({ token, password: $("password").value }),
        headers: { "Content-Type": "application/json", "X-CSRF-Token": meta("cb-csrf") } });
      if (r.ok) { location.href = "/#/music"; return; }
      say((await r.json().catch(() => ({}))).detail || `Couldn't connect (${r.status}).`);
    } catch (err) { say(`Apple didn't finish the sign-in: ${err.message || err}`); }
    $("go").disabled = false;
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("connect").addEventListener("submit", connect);
    if (window.MusicKit) ready();
    else document.addEventListener("musickitloaded", ready, { once: true });
  });
})();
