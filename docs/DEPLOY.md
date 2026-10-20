# Deploying to Render

The repository includes a Blueprint (`render.yaml`) and a `Dockerfile`. Render builds the image from GitHub on every push to `main`, serves it over HTTPS, and keeps everything that must survive a redeploy on a persistent disk at `/var/data`: the database, the Flickr login, the rate-limit file and the backups.

## What it needs

- **A paid Render instance with a disk.** Free instances have no disk and sleep after 15 minutes without visitors, which would lose the database and stop the hourly sync. The Blueprint asks for `1c-2g` (1 CPU, 2 GB): planning the whole library peaks around 400 MB, so the 512 MB size is too tight. Check render.com/pricing for current prices; the disk starts at 5 GB (the database is ~140 MB, plus compressed backups).
- **This repository on GitHub, private.** It has no secrets in it, but there's no reason to publish your categories and handles.
- One instance only: a disk can't be shared, and each deploy has a short gap while the old instance stops.

## 1. Prepare locally (to keep your history)

Bringing your current database keeps everything: the review decisions (so nothing you rejected comes back), the paid-for vision results, the stats history, comments, faves and groups. Skip to step 2 to start fresh instead.

```bash
uv run flickr-organizer settings import-file config.yaml   # every setting + API keys (encrypted) into the database
uv run flickr-organizer user list                          # make sure you have a login (else: user add <name>)
uv run flickr-organizer backup                             # → backups/flickr_organizer-<time>.db.gz
cat ~/.config/flickr-organizer/secret.key                  # the key the API keys are encrypted with
```

After `import-file`, the deployed app needs no config.yaml: the settings travel inside the database. The settings that protect the server itself (host, port, allowed hosts and so on) aren't stored; Render's environment provides them.

## 2. Create the service

1. Push to GitHub (`git push`).
2. In Render: **New → Blueprint**, pick the repository. Render reads `render.yaml`.
3. Fill in the environment variables it asks for:

| Variable | Fresh start | Bringing your database |
|---|---|---|
| `FLICKR_ORGANIZER_SECRET_KEY` | leave the generated value | **replace it with the contents of your local `secret.key`**, or the stored API keys can't be read |
| `FLICKR_ORGANIZER_ADMIN_USER` / `_PASSWORD` | your login (12+ characters); created on first start | leave empty (your login is in the database) |
| `FLICKR_ORGANIZER_ALLOWED_HOSTS` | your custom domain, if any (comma-separated); the `onrender.com` name is allowed automatically | same |
| `FLICKR_API_KEY`, `FLICKR_API_SECRET`, `ANTHROPIC_API_KEY` | optional: or set them in Settings once you're in (stored encrypted) | leave empty (they're in the database) |

Environment variables win over the admin page, and show as locked there. Keep API keys in one place or the other.

4. Create it. The first deploy builds the image (a few minutes) and starts the app. `/healthz` tells Render it's up.

## 3. Bring your database (if you're keeping it)

Upload the backup to the disk as `restore.db.gz`, then restart. On start, the app checks it, restores it before anything opens the database, keeps the empty one beside it, and renames the file so it's never applied twice.

- **SSH / scp** (set up SSH for the service in Render first):
  `scp -s backups/flickr_organizer-<time>.db.gz <service>@ssh.<region>.render.com:/var/data/restore.db.gz`
- **Or Render's Shell** (Dashboard → the service → Shell): run `wormhole receive` there after `wormhole send <file>` locally (magic-wormhole; in the Shell install it with `pip install magic-wormhole` first), then `mv <file> /var/data/restore.db.gz`.

Then **Manual Deploy → Restart service**. The log shows `Restored the database from restore.db.gz`.

## 4. First sign-in

1. Open `https://<service>.onrender.com` and sign in.
2. Fresh start: **Settings → Account → change the password**, then delete `FLICKR_ORGANIZER_ADMIN_USER` and `FLICKR_ORGANIZER_ADMIN_PASSWORD` in Render.
3. **Settings → API keys**: set the keys if you didn't use environment variables, then **Connect Flickr**. That opens flickr.com to approve access and brings you back; the login is saved on the disk. (If Flickr refuses the return address, set your Flickr app's callback URL to `https://<your host>/api/flickr/callback` at flickr.com/services/apps.)
4. Fresh start only: **Settings** for categories, briefs and the rest (or import them: `settings import-file` locally, then bring the database as in step 3).

The scheduler then keeps itself up to date: an incremental sync every hour (~4 calls), every photo re-read daily, a full sync weekly, per-day stats with backfill, and a backup every 24 h. A fresh start's first sync is a full one (~180 calls, a few minutes). Queue photos for review with **Review → Not processed → Process now**, or set **Settings → Web app → Queue new photos after each sync**.

## Custom domain

Add it in Render (Settings → Custom Domains) and at your DNS provider as Render shows, then add it to `FLICKR_ORGANIZER_ALLOWED_HOSTS`. Render issues the certificate.

## Updating

Push to `main`; Render rebuilds and redeploys (`autoDeployTrigger: commit`). Database changes upgrade themselves on start. There's a short gap while the old instance stops, because of the disk.

## Backups

- Automatic every `backup.every_hours` (24), keeping `backup.keep` (7), compressed, in `/var/data/backups`. **Settings → Backups** lists them, makes one now, and downloads one (asks for your password: it's the whole database).
- They're on the same disk as the database, so download one now and then and keep it elsewhere. Render also snapshots the disk daily, but a snapshot of a live database can be inconsistent; prefer the app's backups.
- API keys stay encrypted in backups. Keep `FLICKR_ORGANIZER_SECRET_KEY` somewhere safe and separate (a password manager): without it, a restored database's API keys can't be read and have to be set again.
- To restore on Render: upload the backup as `/var/data/restore.db.gz` and restart (step 3).

## Security checklist

- [ ] Repository private; no `config.yaml`, database, backups or keys in it (`.gitignore` and `.dockerignore` keep them out).
- [ ] A long, unique password; the bootstrap variables deleted after the first sign-in.
- [ ] `FLICKR_ORGANIZER_SECRET_KEY` saved somewhere safe, not next to the backups.
- [ ] API keys in one place (Settings, encrypted, or Render's environment), not both.
- [ ] `FLICKR_ORGANIZER_ALLOWED_HOSTS` lists only your domains.
- [ ] Settings → Account: check signed-in sessions now and then.

What the app does for you: HTTPS only (Render redirects HTTP; the app sends HSTS and Secure, HttpOnly, SameSite=Strict cookies), argon2id passwords, throttled logins, a CSRF token on every change, a strict Content-Security-Policy, only your host names accepted, API keys encrypted at rest and never sent to the browser, your password required again for keys and backups, query strings kept out of the access log, and the app running as an unprivileged user.
