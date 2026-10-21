# Deploying to Render

The repository includes a Blueprint (`render.yaml`) and a `Dockerfile`. Render builds the image from GitHub on every push to `main`, serves it over HTTPS, and keeps everything that must survive a redeploy on a persistent disk at `/var/data`: the database, the Flickr login, the rate-limit file and the backups.

## Moving an existing SQLite deployment to DuckDB (one time)

This version keeps its data in DuckDB (`/var/data/flickr_organizer.duckdb`). A deployment from before keeps its library in SQLite (`/var/data/flickr_organizer.db`, with its logins, settings, encrypted API keys and review history). On the first start the app finds that file beside the new database and offers to import it. Nothing is deleted or changed: the SQLite file is only read, and the import happens inside one transaction, so it either completes or leaves the new database as it was. No environment variables are needed; the Blueprint is unchanged.

1. **Before deploying**, with the current version: Settings → Backups → make and download a backup, and keep a copy of `secret.key` (on the disk at `/var/data/secret.key`). This is your way back.
2. **Deploy.** The new instance starts with an empty DuckDB database and, in the Logs, prints `finish setup with this code: XXXX-XXXX-XXXX` and `A SQLite library was found; the setup page can import it.`
3. **Import.** Open the app. The setup page has an **Import the SQLite library** card: enter the code from the Logs and press Import. It shows the table and row count as it goes (about 400,000 rows takes under half a minute). When it says complete, sign in with the login you already had; settings and API keys came across too.

   The same thing from a terminal, with the code from the Logs (progress is public and shows counts only):

   ```bash
   curl -X POST https://<service>.onrender.com/api/setup/migrate -H 'Content-Type: application/json' -d '{"code": "XXXX-XXXX-XXXX"}'
   curl https://<service>.onrender.com/api/migration/status
   ```

   If you created a login on the setup page first, import from Settings instead: `POST /api/admin/database/migration` with `{"password": "..."}` and the CSRF token. It replaces that login with the imported ones. Either way the import only runs into a database that has no photos yet.
4. **If it fails**, the status says `failed` with the reason, and the new database is still empty: fix the cause (or restore your backup) and import again. The app doesn't need a restart.

To rehearse it first, convert a copy on your own machine: `flickr-organizer migrate-sqlite flickr_organizer.db /tmp/try.duckdb` (it refuses to overwrite an existing file and checks every table's row count against the source).

### Winding SQLite down

Once you've used the app on DuckDB for a while and a fresh backup (Settings → Backups) looks right:

1. Delete the old files from the disk (Render: the service's Shell): `rm /var/data/flickr_organizer.db*` (the database and its `-wal`/`-shm` files). The import status file `flickr_organizer.duckdb.migration.json` can go too.
2. Take the import code out of the app (the import page, `flickr_organizer/store/migration.py`, `POST /api/setup/migrate`, `/api/admin/database/migration`, `/api/migration/status`, the `migrate-sqlite` command, and SQLite backup conversion in `backup.py`), which leaves nothing in the deployment that uses SQLite.

Rolling back before step 1: deploy the previous version; it still finds its SQLite file untouched.

DuckDB lets one process use a database at a time, so run command-line commands (`sync`, `backup`, ...) only while the web app is stopped; the app makes its own backups on the schedule in Settings.

## What it needs

- **A paid Render instance with a disk.** Free instances have no disk and sleep after 15 minutes without visitors, which would lose the database and stop the hourly sync. The Blueprint asks for `1c-2g` (1 CPU, 2 GB): planning the whole library peaks around 400 MB, so the 512 MB size is too tight. Check render.com/pricing for current prices; the disk starts at 5 GB (the database is ~140 MB, plus compressed backups).
- **This repository on GitHub, private.** It has no secrets in it, but there's no reason to publish your categories and handles.
- One instance only: a disk can't be shared, and each deploy has a short gap while the old instance stops.

## 1. Prepare locally (to keep your history)

Bringing your current database keeps everything: the review decisions (so nothing you rejected comes back), the paid-for vision results, the stats history, comments, faves, groups, your login and your settings. Skip to step 2 to start fresh instead.

```bash
uv run flickr-organizer settings import-file config.yaml
uv run flickr-organizer backup
ls -lh backups/
```

`import-file` copies every setting, and the API keys (encrypted), into the database, so the server needs no config.yaml. The settings that protect the server itself (host, port, allowed hosts and so on) aren't copied; the server works them out. Keep two files at hand: the newest `backups/flickr_organizer-….db.gz`, and `~/.config/flickr-organizer/secret.key` (the key the API keys are encrypted with).

## 2. Create the service

1. Push to GitHub (`git push`).
2. In Render: **New → Blueprint**, pick the repository. Render reads `render.yaml`. No environment variables are needed.
3. Create it. The first deploy builds the image (a few minutes) and starts the app. `/healthz` tells Render it's up.

## 3. First-run setup

With no login yet, the app opens on a setup page instead of the sign-in page.

1. In Render, open the service's **Logs** and find `finish setup with this code: XXXX-XXXX-XXXX`. The page asks for it, so a stranger who finds the new address first can't claim the server. A new code is made at every start until setup is done.
2. Open `https://<service>.onrender.com`, enter the code, and either:
   - **Restore a backup:** drag `flickr_organizer-….db.gz` and `secret.key` onto the drop zone (or paste the key), then **Restore**. The backup is checked as a valid Coconut Barometer library database, and the key is checked against its encrypted API keys. The app restarts into it, keeping the empty database beside it. The page reloads by itself; sign in with your usual login.
   - **Create a login:** a username and a password of 12+ characters. You're signed in straight away.
3. **Settings → API keys**: set the keys if you started fresh, then **Connect Flickr**. That opens flickr.com to approve access and brings you back; the login is saved on the disk. (If Flickr refuses the return address, set your Flickr app's callback URL to `https://<your host>/api/flickr/callback` at flickr.com/services/apps.)
4. Fresh start only: **Settings** for categories, briefs and the rest (or bring them later: `settings import-file` locally, back up, and restore it in **Settings → Backups**).

The encryption key lives at `/var/data/secret.key` on the disk. It's made when you first set an API key, or comes with a restore.

### Optional environment variables

- `FLICKR_ORGANIZER_ALLOWED_HOSTS`: a custom domain (see below). The `onrender.com` name is allowed automatically.
- `FLICKR_ORGANIZER_SECRET_KEY`, `FLICKR_API_KEY`, `FLICKR_API_SECRET`, `ANTHROPIC_API_KEY`: these win over the disk and the Settings page, and show as locked there. If an earlier Blueprint set `FLICKR_ORGANIZER_SECRET_KEY`, delete it, or a restored backup's API keys can't be read.

The scheduler then keeps itself up to date: an incremental sync every hour (~4 calls), every photo re-read daily, a full sync weekly, per-day stats with backfill, and a backup every 24 h. A fresh start's first sync is a full one (~180 calls, a few minutes). Photos uploaded after that land under **Review → To review** after each sync: processed when you open each one, or straight away with **Settings → Sync → New photos**. Older photos are under **Review → Not processed**.

## Custom domain

Add it in Render (Settings → Custom Domains) and at your DNS provider as Render shows, then add it to `FLICKR_ORGANIZER_ALLOWED_HOSTS`. Render issues the certificate.

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
- [ ] `FLICKR_ORGANIZER_ALLOWED_HOSTS` lists only your domains.
- [ ] Settings → Account: check signed-in sessions now and then.

What the app does for you: HTTPS only (Render redirects HTTP; the app sends HSTS and Secure, HttpOnly, SameSite=Strict cookies), argon2id passwords, throttled logins, a CSRF token on every change, a strict Content-Security-Policy, only your host names accepted, API keys encrypted at rest and never sent to the browser, your password required again for keys and backups, query strings kept out of the access log, and the app running as an unprivileged user.
