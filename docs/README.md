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

1. On a machine with DB access, run the live app and refresh the cache:
   ```bash
   python app.py
   ```
   Log in with real SQL credentials, then click **"Sync all & cache"** on
   any campaign page. This fills `cache/*.json` for every campaign.

2. Export that cache into this folder's `data.json`:
   ```bash
   python export_static.py
   ```
   This writes `docs/data.json`, combining every campaign's disposition
   and calling-report cache into one file, plus a `generated_at` timestamp
   (shown on the page so viewers know how fresh it is).

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

There's no automatic scheduling here — steps 1–3 above are a manual,
on-demand refresh. If you want this to update itself on a schedule
(e.g. every morning) without you running it by hand, that needs a machine
that can reach the DB and is either always-on (a scheduled task/cron job
calling `app.py`'s sync + `export_static.py`, then `git push`) or a small
CI job with DB network access — let me know if you want that wired up.

## Local preview

Opening `index.html` directly as a `file://` path will fail — browsers
block `fetch()` for local files by default. Preview it over a real (even
local) HTTP server instead:

```bash
cd docs
python -m http.server 8000
```

Then open `http://localhost:8000`.
