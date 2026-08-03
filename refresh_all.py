"""
Refresh all campaigns' cache from the command line — the CLI equivalent of
logging into the web UI and clicking "Sync all & cache".

Runs the same live queries as app.py, writes the same cache/*.json files, and
does NOT need the Flask server (app.py) to be running at the same time.

USAGE

    Prompt for password (safest — nothing typed shows on screen, nothing
    saved to shell history):

        python refresh_all.py --username your_sql_login

    Pass everything as arguments (fine for manual runs, avoid in scripts
    that might get logged):

        python refresh_all.py --username your_sql_login --password your_pw

    For unattended/scheduled runs (Windows Task Scheduler, cron, etc.), set
    the password via an environment variable instead of a flag, so it never
    appears in a process list or a saved .bat file:

        set DASHBOARD_DB_USER=your_sql_login
        set DASHBOARD_DB_PASSWORD=your_pw
        python refresh_all.py

    Add --export to also regenerate docs/data.json for the static GitHub
    Pages site in the same run:

        python refresh_all.py --username your_sql_login --export
"""

import argparse
import getpass
import os
import sys
import time

import app as dash  # reuses app.py's queries, connection logic, and cache writer


def refresh_all(username: str, password: str) -> list[str]:
    ok, err = dash.try_login(username, password)
    if not ok:
        print(f"Login failed: {err}", file=sys.stderr)
        sys.exit(1)
    print(f"Connected as {username} to {dash.SERVER}/{dash.DATABASE}")

    print("Fetching campaign list...")
    _, campaign_rows = dash.run_query(username, password, dash.CAMPAIGN_LIST_QUERY)
    campaigns = [r["Campaign_Name"] for r in campaign_rows]
    dash.save_cache_to_disk("campaigns", None, ["Campaign_Name"], campaign_rows, time.time())

    if not campaigns:
        print("No campaigns found for the current month. Nothing to cache.")
        return []

    print(f"Found {len(campaigns)} campaign(s): {', '.join(campaigns)}")

    for i, campaign in enumerate(campaigns, start=1):
        print(f"[{i}/{len(campaigns)}] {campaign} ... ", end="", flush=True)

        disp_cols, disp_rows = dash.run_query(
            username, password, dash.DISPOSITION_QUERY, params=[campaign]
        )
        dash.save_cache_to_disk("disposition", campaign, disp_cols, disp_rows, time.time())

        call_cols, call_rows = dash.run_query(
            username, password, dash.CALLING_REPORT_QUERY, params=[campaign]
        )
        dash.save_cache_to_disk("calling", campaign, call_cols, call_rows, time.time())

        print(f"disposition={len(disp_rows)} rows, calling={len(call_rows)} rows — cached")

    return campaigns


def main():
    parser = argparse.ArgumentParser(description="Refresh cache/*.json for every campaign.")
    parser.add_argument(
        "--username",
        default=os.environ.get("DASHBOARD_DB_USER"),
        help="SQL Server username (or set env var DASHBOARD_DB_USER)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("DASHBOARD_DB_PASSWORD"),
        help="SQL Server password (or set env var DASHBOARD_DB_PASSWORD; "
        "omit both to be prompted securely)",
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Also regenerate docs/data.json for the static GitHub Pages site after syncing",
    )
    args = parser.parse_args()

    username = args.username or input("SQL username: ").strip()
    password = args.password or getpass.getpass("SQL password: ")

    if not username or not password:
        print("Username and password are required.", file=sys.stderr)
        sys.exit(1)

    start = time.time()
    campaigns = refresh_all(username, password)
    elapsed = round(time.time() - start, 1)
    print(f"\nDone. Cached {len(campaigns)} campaign(s) in {elapsed}s -> {dash.CACHE_DIR}")

    if args.export:
        print("\nRunning export_static.py ...")
        import export_static
        export_static.main()


if __name__ == "__main__":
    main()
