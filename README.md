# Dialer Campaign Console (HTML / Flask version)

A small Flask app with real server-rendered HTML pages: `/login`, then one
page per campaign at `/dashboard/<campaign>` showing today's disposition
summary and a rolling 4-month calling report.

Plain HTML/JS alone can't open a SQL Server connection (browsers can't run
`pyodbc`), so this keeps a tiny Python/Flask backend to do the DB work and
just renders normal HTML/CSS pages — no React, no Streamlit widgets.

> **Deploying to GitHub Pages instead of running Flask somewhere?**
> GitHub Pages can only serve static files — it can't run this Flask app or
> reach your internal DB at all. See **`docs/README.md`** for a static,
> snapshot-based version that works there instead.

## 1. Requirements

- Python 3.9+
- ODBC Driver 17 (or 18) for SQL Server installed on the machine running this
- Network access from this machine to `172.16.1.13`

Check the driver:
```bash
odbcinst -q -d
```
If you only have Driver 18, change `ODBC_DRIVER` near the top of `app.py` to
`"ODBC Driver 18 for SQL Server"`, and if your server uses a self-signed
cert, add `Encrypt=no;` (or `TrustServerCertificate=yes;`) inside
`build_connection_string()`.

## 2. Install

```bash
pip install -r requirements.txt
```

## Setting up from this repo (GitHub-style)

This repo is set up so nothing sensitive is hardcoded or committed:

- `.gitignore` excludes `.env`, the `cache/` folder (generated campaign
  data), `__pycache__/`, and virtual envs.
- `.env.example` lists every configurable setting. Copy it to a real `.env`
  and fill in your values — `.env` itself is git-ignored, so it never gets
  committed:
  ```bash
  cp .env.example .env
  ```
  Then edit `.env` and set at least:
  - `DASHBOARD_DB_SERVER`, `DASHBOARD_DB_NAME` — your actual SQL Server
  - `DASHBOARD_SECRET_KEY` — a random string (see the comment in
    `.env.example` for how to generate one)
  - `DASHBOARD_OFFLINE_PASSCODE` — change from the `view-only` default
- `app.py` auto-loads `.env` via `python-dotenv` if it's installed
  (included in `requirements.txt`). If you'd rather not use a `.env` file,
  just set the same names as real environment variables instead — either
  way works.
- If you fork/clone this to a **public** GitHub repo, double check `.env`
  never got committed (`git status` should never show it) and that no real
  server IP, credentials, or passcode end up in `app.py` itself — they
  should only live in your local `.env` / environment.

## 3. Run

```bash
python app.py
```

Open **http://localhost:5000**. For real deployment, run it behind a WSGI
server (gunicorn/waitress) and a reverse proxy with HTTPS rather than the
Flask dev server — see "Production notes" below.

## 4. Two ways to log in

**Live login (`/login`)** — same as before: real SQL credentials, tested
against the server, then live queries.

**Offline / Cached View (`/offline/login`, linked from the login page)** —
a separate passcode (not a SQL login) that browses the **last cached
snapshot from disk**, with zero DB connection attempted. Useful when the
VPN/DB is down or you just want a fast look without waiting on live
queries.

- Default passcode is `view-only` — **change this** before handing the link
  to anyone, via the `DASHBOARD_OFFLINE_PASSCODE` environment variable:
  ```
  set DASHBOARD_OFFLINE_PASSCODE=your-passcode-here    (Windows cmd)
  $env:DASHBOARD_OFFLINE_PASSCODE="your-passcode-here" (PowerShell)
  ```
- Every campaign page in offline mode shows a purple "Offline mode" banner
  and a `cached as of <timestamp>` label under each table, so it's always
  clear how stale the data might be.
- If nothing has been cached yet, offline mode shows an empty state telling
  you to log in live once and sync.

## 5. The disk cache

- Every live query result (campaign list, each campaign's disposition
  summary, each campaign's calling report) is written to a JSON file in a
  `cache/` folder next to `app.py` the moment it's fetched. This cache
  survives app restarts — it's what offline mode reads from.
- **"Sync all & cache"** button (top right of any live campaign page) loops
  through every campaign and force-refreshes + caches both queries in one
  click — the easiest way to get a complete, up-to-date snapshot ready for
  offline use before, say, going somewhere without VPN access.
- **"Refresh this campaign"** only force-refreshes the currently viewed
  campaign.
- Outside of an explicit refresh/sync, live pages reuse an in-memory result
  for `CACHE_TTL_SECONDS` (default 120s) before querying again, to avoid
  hammering the DB on every click.
- You can inspect the cache files directly — they're plain JSON:
  `cache/campaigns.json`, `cache/<Campaign_Name>__disposition.json`,
  `cache/<Campaign_Name>__calling.json`. Delete any of them (or the whole
  `cache/` folder) to force a clean slate.

## 6. How it works

- **Login (`/login`)** — the username/password you enter is used directly
  as the SQL Server login and tested with a real connection attempt. Wrong
  credentials show an error instead of letting you in.
- Credentials are **not** put in a cookie. Only a random session id goes to
  the browser; the actual username/password stay server-side in memory,
  keyed by that id, and are dropped on logout or server restart.
- **Sidebar nav** — one link per campaign (this month's distinct
  `Campaign_Name` values). Clicking a campaign loads its own full page:
  `/dashboard/<campaign>`.
- Each campaign page has two panels:
  - **Today's Disposition Summary** — your disposition query, parameterized.
  - **Last 4 Months Calling Report** — your `#calling` query, but the month
    filter is now dynamic (current month + previous 3, instead of a
    hardcoded `month(7)`), with three extra columns computed in Python:
    `Connected_%`, `UNQ_Connected_%`, `True_Connected_%` — each colour-coded
    green/amber/red so low connect days jump out.
- **Refresh** re-runs both queries for that campaign immediately. Otherwise
  live pages reuse results for 2 minutes (`CACHE_TTL_SECONDS`) before
  re-querying, so clicking around doesn't hit the DB every time.
- The query cache itself is **not** per-user — it's shared/disk-persisted
  (see section 5) so offline mode and other live users all benefit from
  each other's syncs. **Logout** only ends your login session; it doesn't
  clear the cache.

## Production notes

- `app.py` auto-creates a `cache/` folder next to itself on startup — make
  sure the account running the app has write access to that folder.
- The `cache/` folder contains real campaign metrics (no credentials) in
  plain JSON. Treat it like any other exported report file — keep it out
  of source control / don't share it outside the team if the numbers are
  sensitive.

- `app.secret_key` falls back to a random key generated at process start —
  set the `DASHBOARD_SECRET_KEY` environment variable to a fixed secret if
  you run multiple worker processes (otherwise sessions won't be portable
  between workers).
- The in-memory `_SESSIONS` / `_CACHE` dicts only work for a single
  process. If you deploy with multiple gunicorn workers, move these to
  Redis or similar — say the word and I can wire that in.
- Don't run `app.run(debug=True)` in production; use gunicorn/waitress
  behind nginx or IIS, and put it behind HTTPS since real SQL credentials
  are submitted via the login form.

## Things you may want to adjust

- `AVG_TIME` in the calling report is actually a `SUM` of answered seconds
  (kept as-is from your original query/column name) — let me know if you
  intended a true average.
- Percentage colour thresholds (`pct_class` filter in `app.py`): currently
  <30% red, 30–60% amber, ≥60% green. Adjust to whatever your team
  considers a healthy connect rate.
- If you'd rather use one shared service account for the DB and a separate,
  simpler dashboard login (instead of handing out real SQL Server
  credentials to everyone using the dashboard), that's a straightforward
  change — just ask.
