"""
Export cached campaign data (produced by app.py's live/"Sync all & cache")
into a single docs/data.json file that the static GitHub Pages site reads.

Run this AFTER logging into the Flask app live and clicking "Sync all & cache"
at least once, so cache/*.json actually has data in it.

Usage:
    python export_static.py

Then commit + push the docs/ folder (including the new data.json) to your
GitHub Pages repo/branch. See docs/README.md for exact setup steps.
"""

import json
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
DOCS_DIR = os.path.join(BASE_DIR, "docs")


def safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    campaigns_path = os.path.join(CACHE_DIR, "campaigns.json")
    if not os.path.exists(campaigns_path):
        raise SystemExit(
            "No cache/campaigns.json found.\n"
            "Run the Flask app (python app.py), log in with live DB credentials,\n"
            "and click 'Sync all & cache' at least once before running this script."
        )

    campaigns_payload = load(campaigns_path)
    campaigns = [r["Campaign_Name"] for r in campaigns_payload["rows"]]

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "campaigns": campaigns,
        "disposition": {},
        "calling": {},
    }

    missing = []
    for c in campaigns:
        safe = safe_name(c)
        disp_path = os.path.join(CACHE_DIR, f"{safe}__disposition.json")
        call_path = os.path.join(CACHE_DIR, f"{safe}__calling.json")

        if os.path.exists(disp_path):
            data["disposition"][c] = load(disp_path)
        else:
            missing.append(disp_path)

        if os.path.exists(call_path):
            data["calling"][c] = load(call_path)
        else:
            missing.append(call_path)

    os.makedirs(DOCS_DIR, exist_ok=True)
    out_path = os.path.join(DOCS_DIR, "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"Wrote {out_path} with {len(campaigns)} campaigns.")
    if missing:
        print("Note: some campaigns are missing disposition/calling data:")
        for m in missing:
            print(f"  - {m}")
        print("Run 'Sync all & cache' again in the live app to fill these in.")


if __name__ == "__main__":
    main()
