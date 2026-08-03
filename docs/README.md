# Static snapshot (GitHub Pages)

GitHub Pages only serves static files — it can't run Python/Flask, and it
can't reach an internal SQL Server IP like `172.16.1.13` even if it could
run code. So this folder is a **read-only snapshot**: real queries run
locally (via `app.py`, where you have DB access), and get exported here as
plain JSON that a static page reads in the browser. Nothing in `docs/`
ever talks to the database.

## Files

- `index.html` — page shell (sidebar + content area)
- `app.js` — fetches `data.json` and renders the campaign nav + both
  tables client-side (vanilla JS, no build step, no framework)
- `style.css` — same visual design as the live app
- `data.json` — **generated**, not written by hand (see below)

## How to publish an update

**Option A — one command (recommended):**
```bash
python refresh_all.py --username your_sql_login --export
git add docs/data.json
git commit -m "Update dashboard snapshot"
git push
```
`--export` runs `export_static.py` automatically right after syncing, so
this does everything in one shot.

**Option B — via the browser:**
1. Run the live app (`python app.py`), log in with real SQL credentials,
   and click **"Sync all & cache"** on any campaign page. This fills
   `cache/*.json` for every campaign.
2. Export that cache into this folder's `data.json`:
   ```bash
   python export_static.py
   ```
3. Commit and push:
   ```bash
   git add docs/data.json
   git commit -m "Update dashboard snapshot"
   git push
   ```
   GitHub Pages picks up the change automatically (usually within a
   minute or two).

## One-time GitHub Pages setup

In your repo: **Settings → Pages → Build and deployment → Source:**
"Deploy from a branch", then pick your branch and the **`/docs`** folder.
Save. Your site will be served at `https://<username>.github.io/<repo>/`.

If your repo is *itself* meant to be the site root instead (i.e. you don't
want a `/docs` subfolder), just copy `index.html`, `app.js`, `style.css`,
and `data.json` straight into the repo root and set Pages source to `/`
(root) instead — either layout works, `data.json` just needs to sit next
to `index.html`.

## Refresh cadence

There's no automatic scheduling here by default — but it's now a single
command, so wiring one up is straightforward:

```bash
python refresh_all.py --username your_sql_login --export
```

(set `DASHBOARD_DB_USER` / `DASHBOARD_DB_PASSWORD` as environment
variables instead of typing the password, for unattended runs — see the
main `README.md`). Point Windows Task Scheduler / cron at that command,
followed by a `git add docs/data.json && git commit -m "auto sync" && git
push`, and the site updates itself on whatever schedule you set.

## Local preview

Opening `index.html` directly as a `file://` path will fail — browsers
block `fetch()` for local files by default. Preview it over a real (even
local) HTTP server instead:

```bash
cd docs
python -m http.server 8000
```

Then open `http://localhost:8000`.
