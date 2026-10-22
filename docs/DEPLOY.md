# Deploying to Render

The repository includes a Blueprint (`render.yaml`) and a `Dockerfile`. Render builds the image from GitHub on every push to `main`, serves it over HTTPS, and keeps everything that must survive a redeploy on a persistent disk at `/var/data`: the database, the Flickr login, the rate-limit file and the backups.

## Moving from the flickr-organizer service

The app was called flickr-organizer. A Blueprint from this repository now makes a separate service, `coconut-barometer`, with its own empty disk; bring everything across with a backup:

1. **On the old service:** Settings → Backups → **Back up now**, then **Download** it (`flickr_organizer-….db.gz`), and **Download encryption key** (`secret.key`). Keep the two apart.
2. **Create the new service:** New → Blueprint → this repository. Open it with the setup code from its Logs and **Restore a backup** with both files (see "What it needs" below).
3. **Reconnect Flickr** (Settings → Connections → **Connect Flickr**): the Flickr login is a file on the old disk, not in the backup.
4. Check it, then suspend or delete the old service. Anything the old one did after the backup (APRS positions heard, reviews) stays there; Flickr and WeatherFlow data fill back in by themselves.

Backups from before the move to DuckDB (SQLite) can't be restored; converting one needs the import from git history (`flickr_organizer/store/migration.py`).

DuckDB lets one process use a database at a time, so run command-line commands (`sync`, `backup`, ...) only while the web app is stopped; the app makes its own backups on the schedule in Settings.

## What it needs

- **A paid Render instance with a disk.** Free instances have no disk and sleep after 15 minutes without visitors, which would lose the database and stop the hourly sync. The Blueprint asks for `1c-2g` (1 CPU, 2 GB): planning the whole library peaks around 400 MB, so the 512 MB size is too tight. Check render.com/pricing for current prices; the disk starts at 5 GB (the database is ~140 MB, plus compressed backups).
- **This repository on GitHub, private.** It has no secrets in it, but there's no reason to publish your categories and handles.
- One instance only: a disk can't be shared, and each deploy has a short gap while the old instance stops.

## 1. Prepare locally (to keep your history)

Bringing your current database keeps everything: the review decisions (so nothing you rejected comes back), the paid-for vision results, the stats history, comments, faves, groups, your login and your settings. Skip to step 2 to start fresh instead.

```bash
uv run coconut-barometer settings import-file config.yaml
uv run coconut-barometer backup
ls -lh backups/
```

`import-file` copies every setting, and the API keys (encrypted), into the database, so the server needs no config.yaml. The settings that protect the server itself (host, port, allowed hosts and so on) aren't copied; the server works them out. Keep two files at hand: the newest `backups/coconut_barometer-….db.gz`, and `~/.config/coconut-barometer/secret.key` (the key the API keys are encrypted with).

## 2. Create the service

1. Push to GitHub (`git push`).
2. In Render: **New → Blueprint**, pick the repository. Render reads `render.yaml`. No environment variables are needed.
3. Create it. The first deploy builds the image (a few minutes) and starts the app. `/healthz` tells Render it's up.

## 3. First-run setup

With no login yet, the app opens on a setup page instead of the sign-in page.

1. In Render, open the service's **Logs** and find `finish setup with this code: XXXX-XXXX-XXXX`. The page asks for it, so a stranger who finds the new address first can't claim the server. A new code is made at every start until setup is done.
2. Open `https://<service>.onrender.com`, enter the code, and either:
   - **Restore a backup:** drag `coconut_barometer_….db.gz` (or `flickr_organizer-….db.gz` from the old service) and `secret.key` onto the drop zone (or paste the key), then **Restore**. The backup is checked as a valid Coconut Barometer library database, and the key is checked against its encrypted API keys. The app restarts into it, keeping the empty database beside it. The page reloads by itself; sign in with your usual login.
   - **Create a login:** a username and a password of 12+ characters. You're signed in straight away.
3. **Settings → Connections**: set the keys if you started fresh, then **Connect Flickr**. That opens flickr.com to approve access and brings you back; the login is saved on the disk. (If Flickr refuses the return address, set your Flickr app's callback URL to `https://<your host>/api/flickr/callback` at flickr.com/services/apps.)
4. Fresh start only: **Settings** for categories, briefs and the rest (or bring them later: `settings import-file` locally, back up, and restore it in **Settings → Backups**).

The encryption key lives at `/var/data/secret.key` on the disk. It's made when you first set an API key, or comes with a restore.

### Optional environment variables

- `ALLOWED_HOSTS`: a custom domain (see below). The `onrender.com` name is allowed automatically.

API keys and the encryption key can't be set from the environment: they live in Settings and on the disk.

The scheduler then keeps itself up to date: an incremental sync every hour (~4 calls), every photo re-read daily, a full sync weekly, per-day stats with backfill, and a backup every 24 h. A fresh start's first sync is a full one (~180 calls, a few minutes). Photos uploaded after that land under **Review → To review** after each sync: processed when you open each one, or straight away with **Settings → Sync → New photos**. Older photos are under **Review → Not processed**.

## Custom domain

Add it in Render (Settings → Custom Domains) and at your DNS provider as Render shows, then add it to `ALLOWED_HOSTS`. Render issues the certificate.

## Updating

Push to `main`; Render rebuilds and redeploys (`autoDeployTrigger: commit`). Database changes upgrade themselves on start. There's a short gap while the old instance stops, because of the disk.

## Backups

- Automatic every `backup.every_hours` (24), keeping `backup.keep` (7), compressed, in `/var/data/backups`. **Settings → Backups** lists them, makes one now, and downloads one (asks for your password: it's the whole database).
- They're on the same disk as the database, so download one now and then and keep it elsewhere. Render also snapshots the disk daily, but a snapshot of a live database can be inconsistent; prefer the app's backups.
- API keys stay encrypted in backups; the key isn't in them. Keep a copy of `secret.key` somewhere safe and separate (a password manager): without it, a restored database's API keys can't be read and have to be set again.
- To restore: **Settings → Backups → Restore a backup** (your password; the key only if the backup came from another machine). The app restarts into it.

## Security checklist

- [ ] Repository private; no `config.yaml`, database, backups or keys in it (`.gitignore` and `.dockerignore` keep them out).
- [ ] A long, unique password.
- [ ] A copy of `secret.key` somewhere safe, not next to the downloaded backups.
- [ ] API keys in one place (Settings, encrypted, or Render's environment), not both.
- [ ] `ALLOWED_HOSTS` lists only your domains.
- [ ] Settings → Account: check signed-in sessions now and then.

What the app does for you: HTTPS only (Render redirects HTTP; the app sends HSTS and Secure, HttpOnly, SameSite=Strict cookies), argon2id passwords, throttled logins, a CSRF token on every change, a strict Content-Security-Policy, only your host names accepted, API keys encrypted at rest and never sent to the browser, your password required again for keys and backups, query strings kept out of the access log, and the app running as an unprivileged user.
