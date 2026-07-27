"""
Dialer Campaign Dashboard (Flask + server-rendered HTML)
----------------------------------------------------------
Two ways in:
  1. "Live" login (/login) — authenticates directly against SQL Server using
     the entered username/password as the SQL login, then runs live queries.
     Every live query result is also written to a JSON cache file on disk.
  2. "Offline / Cached View" login (/offline/login) — a separate passcode
     (not a SQL login) that lets you browse the most recent cached results
     from disk WITHOUT opening any DB connection at all. Useful when the DB
     or VPN is unreachable, or you just want a quick look without waiting
     on live queries.

Each campaign gets its own page (/dashboard/<campaign>) showing:
  1. Today's Disposition Summary
  2. Last 4 Months Calling Report (with connect % columns)

Run with:
    python app.py
Then open http://localhost:5000
"""

import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime
from functools import wraps

import pyodbc
from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

try:
    from dotenv import load_dotenv  # optional convenience, see .env.example
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
# All of these can be overridden with environment variables (e.g. via a
# local .env file — see .env.example) instead of editing this file, which
# keeps real server details out of source control.
SERVER = os.environ.get("DASHBOARD_DB_SERVER", "172.16.1.13")
DATABASE = os.environ.get("DASHBOARD_DB_NAME", "Ops_Analytics")
ODBC_DRIVER = os.environ.get("DASHBOARD_ODBC_DRIVER", "ODBC Driver 17 for SQL Server")
CACHE_TTL_SECONDS = int(os.environ.get("DASHBOARD_CACHE_TTL_SECONDS", "120"))

# Passcode for the offline/cached-only login. This is NOT a SQL login — it
# only unlocks reading the on-disk cache. Change it via env var in production:
#   set DASHBOARD_OFFLINE_PASSCODE=something-only-your-team-knows
OFFLINE_PASSCODE = os.environ.get("DASHBOARD_OFFLINE_PASSCODE", "view-only")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", secrets.token_hex(32))

# Server-side credential store, keyed by an opaque session id. Credentials
# never leave the server (only a random id lives in the browser cookie).
_SESSIONS: dict[str, dict] = {}

# In-memory read-through cache in front of the disk cache, keyed by
# (query_name, campaign) -> (timestamp, columns, rows). Not per-user: the
# underlying data isn't user-specific, so it's shared across everyone using
# the dashboard and survives independently of any one login session.
_CACHE: dict[tuple, tuple] = {}


# --------------------------------------------------------------------------
# Disk cache helpers
# --------------------------------------------------------------------------
def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)


def _cache_file(query_name: str, campaign: str | None) -> str:
    if campaign is None:
        fname = f"{_safe_name(query_name)}.json"
    else:
        fname = f"{_safe_name(campaign)}__{_safe_name(query_name)}.json"
    return os.path.join(CACHE_DIR, fname)


def save_cache_to_disk(query_name, campaign, columns, rows, timestamp):
    path = _cache_file(query_name, campaign)
    payload = {"timestamp": timestamp, "columns": columns, "rows": rows}
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, default=str)
    os.replace(tmp_path, path)  # atomic on both POSIX and Windows


def load_cache_from_disk(query_name, campaign=None):
    path = _cache_file(query_name, campaign)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload["timestamp"], payload["columns"], payload["rows"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def list_cached_campaigns() -> list[str]:
    """Campaigns we have *some* cached data for, derived from filenames on disk."""
    names = set()
    for fname in os.listdir(CACHE_DIR):
        if fname.endswith("__disposition.json") or fname.endswith("__calling.json"):
            # strip the trailing __disposition.json / __calling.json
            base = fname.rsplit("__", 1)[0]
            names.add(base)
    # Prefer the real (unsanitized) names from the cached campaign list if we have it
    cached_list = load_cache_from_disk("campaigns")
    if cached_list:
        _, _, rows = cached_list
        real_names = [r["Campaign_Name"] for r in rows]
        # keep only ones we actually have per-campaign cache files for
        safe_real = {_safe_name(n): n for n in real_names}
        return [safe_real[n] for n in names if n in safe_real] or sorted(real_names)
    return sorted(names)


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------
def build_connection_string(username: str, password: str) -> str:
    _ = urllib.parse.quote_plus(password)  # kept for parity with original snippet
    return (
        f"DRIVER={{{ODBC_DRIVER}}};"
        f"SERVER={SERVER};"
        f"DATABASE={DATABASE};"
        f"UID={username};"
        f"PWD={password};"
    )


def try_login(username: str, password: str):
    try:
        conn = pyodbc.connect(build_connection_string(username, password), timeout=10)
        conn.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def run_query(username: str, password: str, query: str, params=None):
    conn = pyodbc.connect(build_connection_string(username, password), timeout=10)
    try:
        cursor = conn.cursor()
        cursor.execute(query, params or [])
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return columns, rows
    finally:
        conn.close()


def cached_query(username, password, query_name, query, campaign=None, params=None, force_refresh=False):
    """Live query with a read-through memory cache; every live result is also
    persisted to disk so it's available later in offline mode."""
    key = (query_name, campaign)
    now = time.time()
    if not force_refresh:
        mem = _CACHE.get(key)
        if mem and (now - mem[0]) < CACHE_TTL_SECONDS:
            return mem[1], mem[2]

    columns, rows = run_query(username, password, query, params)
    _CACHE[key] = (now, columns, rows)
    save_cache_to_disk(query_name, campaign, columns, rows, now)
    return columns, rows


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def login_required(view):
    """Allows either a live DB session or an offline/cached-view session."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        sid = session.get("sid")
        if (sid and sid in _SESSIONS) or session.get("offline"):
            return view(*args, **kwargs)
        return redirect(url_for("login"))

    return wrapped


def live_login_required(view):
    """Stricter: only a real live DB session may pass (used for refresh/sync)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        sid = session.get("sid")
        if not sid or sid not in _SESSIONS:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def current_creds():
    sid = session["sid"]
    creds = _SESSIONS[sid]
    return sid, creds["username"], creds["password"]


def is_offline() -> bool:
    return bool(session.get("offline")) and not session.get("sid")


def session_username():
    if session.get("offline"):
        return "Offline / cached view"
    sid = session.get("sid")
    return _SESSIONS.get(sid, {}).get("username", "")


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
CAMPAIGN_LIST_QUERY = """
SELECT DISTINCT Campaign_Name
FROM RUNO.dbo.Dialer_CDR_NonDND WITH (NOLOCK)
WHERE MONTH(TRY_CAST(End_stamp AS DATE)) = MONTH(TRY_CAST(GETDATE() AS DATE))
ORDER BY Campaign_Name
"""

DISPOSITION_QUERY = """
WITH BaseCalls AS (
    SELECT
        Disposition_Name,
        RIGHT(Client_Number, 10) AS mobile,
        CAST([Date] AS DATE) AS Call_Date
    FROM [RUNO].[dbo].[Dialer_CDR_NonDND] WITH (NOLOCK)
    WHERE Disposition_Name IS NOT NULL
      AND Campaign_Name = ?
      AND CAST([Date] AS DATE) = CAST(GETDATE() AS DATE)
),
UniqueAttempts AS (
    SELECT DISTINCT Disposition_Name, mobile, Call_Date FROM BaseCalls
),
AttemptsPerDisposition AS (
    SELECT Disposition_Name, mobile, COUNT(*) AS Attempt_Count
    FROM BaseCalls
    GROUP BY Disposition_Name, mobile
),
DispositionSummary AS (
    SELECT
        ua.Disposition_Name,
        COUNT(DISTINCT ua.mobile) AS Unique_Clients,
        COUNT(*) AS Total_Attempts,
        ROUND(AVG(CAST(apd.Attempt_Count AS FLOAT)), 2) AS Avg_Attempts
    FROM UniqueAttempts ua
    JOIN AttemptsPerDisposition apd
        ON ua.Disposition_Name = apd.Disposition_Name AND ua.mobile = apd.mobile
    GROUP BY ua.Disposition_Name
),
TotalCalls AS (
    SELECT COUNT(*) AS Total_Unique_Calls FROM UniqueAttempts
),
FinalResult AS (
    SELECT
        ds.Disposition_Name,
        ds.Unique_Clients AS Unique_Calls,
        CONCAT(ROUND(ds.Unique_Clients * 100.0 / NULLIF(tc.Total_Unique_Calls, 0), 2), '%') AS Percentage_Of_Calls,
        ds.Avg_Attempts
    FROM DispositionSummary ds
    CROSS JOIN TotalCalls tc
)
SELECT Disposition_Name AS Disposition, Unique_Calls, Percentage_Of_Calls AS Percentage, Avg_Attempts
FROM (
    SELECT * FROM FinalResult
    UNION ALL
    SELECT
        'TOTAL',
        SUM(Unique_Calls),
        CONCAT(FORMAT(ROUND(SUM(CAST(REPLACE(Percentage_Of_Calls, '%', '') AS FLOAT)), 2), '0.00'), '%'),
        ROUND(AVG(Avg_Attempts), 2)
    FROM FinalResult
) dd
ORDER BY
    CASE WHEN Disposition_Name = 'TOTAL' THEN 1 ELSE 0 END,
    Unique_Calls DESC
"""

# Rolling last 4 months (current month + previous 3) instead of a hardcoded month number.
CALLING_REPORT_QUERY = """
DECLARE @StartDate DATE = DATEADD(MONTH, -3, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1));

SELECT
    TRY_CAST(End_Stamp AS DATE) AS date,
    Campaign_Name,
    COUNT(DISTINCT CASE WHEN Agent_Name <> 'None' THEN Agent_Name END) AS Agent_Count,
    COUNT(Client_Number) AS Attempt,
    COUNT(DISTINCT Client_Number) AS UNQ_Attempt,
    COUNT(CASE WHEN Answered_seconds > 0 THEN Client_Number END) AS Connected,
    COUNT(DISTINCT CASE WHEN Answered_seconds > 0 THEN Client_Number END) AS UNQ_Connected,
    COUNT(CASE WHEN Disposition_Name NOT LIKE 'Did Not Speak%' AND Answered_seconds > 0 THEN Client_Number END) AS True_Connected,
    SUM(CASE WHEN Answered_seconds > 0 THEN Answered_seconds END) AS AVG_TIME,
    COUNT(CASE WHEN Disposition_Name LIKE 'Interested%' THEN Client_Number END) AS INT,
    COUNT(CASE WHEN Disposition_Name LIKE 'follow up%' THEN Client_Number END) AS follow_up,
    COUNT(CASE WHEN Disposition_Name LIKE 'App Start%' THEN Client_Number END) AS App_Start,
    COUNT(CASE WHEN Disposition_Name LIKE 'App Complete%' THEN Client_Number END) AS App_Complete,
    COUNT(CASE WHEN Disposition_Name LIKE 'Final Not Interested%' THEN Client_Number END) AS FNI,
    COUNT(CASE WHEN Disposition_Name LIKE 'Did Not Speak%' THEN Client_Number END) AS DNS,
    COUNT(DISTINCT CASE WHEN Disposition_Name LIKE 'Did Not Speak%' THEN Client_Number END) AS UNQ_DNS
FROM RUNO.dbo.Dialer_CDR_NonDND WITH (NOLOCK)
WHERE Campaign_Name = ?
  AND TRY_CAST(End_Stamp AS DATE) >= @StartDate
GROUP BY TRY_CAST(End_Stamp AS DATE), Campaign_Name
ORDER BY date ASC
"""


def add_percentage_columns(columns, rows):
    """Add Connected_%, UNQ_Connected_%, True_Connected_% to calling-report rows."""
    def pct(numer, denom):
        if not denom:
            return "0%"
        return f"{round(numer / denom * 100, 2)}%"

    for row in rows:
        # rows loaded back from JSON already have these if cached post-computation;
        # recompute defensively so it works whether or not they're present.
        row["Connected_%"] = pct(row.get("Connected", 0) or 0, row.get("Attempt", 0) or 0)
        row["UNQ_Connected_%"] = pct(row.get("UNQ_Connected", 0) or 0, row.get("UNQ_Attempt", 0) or 0)
        row["True_Connected_%"] = pct(row.get("True_Connected", 0) or 0, row.get("Attempt", 0) or 0)

    extra_cols = ["Connected_%", "UNQ_Connected_%", "True_Connected_%"]
    cols = [c for c in columns if c not in extra_cols] + extra_cols
    return cols, rows


def fmt_ts(ts):
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Routes — auth
# --------------------------------------------------------------------------
@app.route("/")
def index():
    if (session.get("sid") in _SESSIONS) or session.get("offline"):
        return redirect(url_for("dashboard_root"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "Please enter both username and password."
        else:
            ok, err = try_login(username, password)
            if ok:
                sid = secrets.token_hex(16)
                _SESSIONS[sid] = {"username": username, "password": password}
                session.clear()
                session["sid"] = sid
                return redirect(url_for("dashboard_root"))
            error = f"Login failed: {err}"
    return render_template("login.html", error=error, database=DATABASE, server=SERVER)


@app.route("/offline/login", methods=["GET", "POST"])
def offline_login():
    error = None
    if request.method == "POST":
        passcode = request.form.get("passcode", "")
        if passcode == OFFLINE_PASSCODE:
            session.clear()
            session["offline"] = True
            return redirect(url_for("dashboard_root"))
        error = "Incorrect passcode."
    return render_template("offline_login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    sid = session.get("sid")
    if sid:
        _SESSIONS.pop(sid, None)
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Routes — dashboard
# --------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard_root():
    if is_offline():
        campaigns = list_cached_campaigns()
        if not campaigns:
            return render_template(
                "dashboard.html", campaigns=[], campaign=None, offline=True,
                username=session_username(),
            )
        return redirect(url_for("dashboard_campaign", campaign=campaigns[0]))

    sid, username, password = current_creds()
    try:
        _, campaign_rows = cached_query(username, password, "campaigns", CAMPAIGN_LIST_QUERY)
    except Exception as exc:  # noqa: BLE001
        return render_template("error.html", message=str(exc))

    campaigns = [r["Campaign_Name"] for r in campaign_rows]
    if not campaigns:
        return render_template("dashboard.html", campaigns=[], campaign=None, offline=False,
                                username=session_username())
    return redirect(url_for("dashboard_campaign", campaign=campaigns[0]))


@app.route("/dashboard/<campaign>")
@login_required
def dashboard_campaign(campaign):
    if is_offline():
        return render_offline_campaign(campaign)

    sid, username, password = current_creds()
    try:
        _, campaign_rows = cached_query(username, password, "campaigns", CAMPAIGN_LIST_QUERY)
        campaigns = [r["Campaign_Name"] for r in campaign_rows]

        disp_cols, disp_rows = cached_query(
            username, password, "disposition", DISPOSITION_QUERY,
            campaign=campaign, params=[campaign],
        )

        call_cols, call_rows = cached_query(
            username, password, "calling", CALLING_REPORT_QUERY,
            campaign=campaign, params=[campaign],
        )
        call_cols, call_rows = add_percentage_columns(call_cols, call_rows)
    except Exception as exc:  # noqa: BLE001
        return render_template("error.html", message=str(exc))

    disp_ts = _CACHE.get(("disposition", campaign), (None,))[0]
    call_ts = _CACHE.get(("calling", campaign), (None,))[0]

    return render_template(
        "dashboard.html",
        campaigns=campaigns,
        campaign=campaign,
        disp_cols=disp_cols,
        disp_rows=disp_rows,
        call_cols=call_cols,
        call_rows=call_rows,
        refreshed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        disp_cache_ts=fmt_ts(disp_ts),
        call_cache_ts=fmt_ts(call_ts),
        username=session_username(),
        offline=False,
    )


def render_offline_campaign(campaign):
    campaigns = list_cached_campaigns()

    disp = load_cache_from_disk("disposition", campaign)
    call = load_cache_from_disk("calling", campaign)

    disp_cols, disp_rows, disp_ts = ([], [], None) if not disp else (disp[1], disp[2], disp[0])
    call_cols, call_rows, call_ts = ([], [], None) if not call else (call[1], call[2], call[0])
    if call_cols:
        call_cols, call_rows = add_percentage_columns(call_cols, call_rows)

    return render_template(
        "dashboard.html",
        campaigns=campaigns,
        campaign=campaign,
        disp_cols=disp_cols,
        disp_rows=disp_rows,
        call_cols=call_cols,
        call_rows=call_rows,
        refreshed_at=None,
        disp_cache_ts=fmt_ts(disp_ts),
        call_cache_ts=fmt_ts(call_ts),
        username=session_username(),
        offline=True,
    )


@app.route("/dashboard/<campaign>/refresh", methods=["POST"])
@live_login_required
def refresh_campaign(campaign):
    sid, username, password = current_creds()
    try:
        cached_query(username, password, "disposition", DISPOSITION_QUERY,
                     campaign=campaign, params=[campaign], force_refresh=True)
        cached_query(username, password, "calling", CALLING_REPORT_QUERY,
                     campaign=campaign, params=[campaign], force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        return render_template("error.html", message=str(exc))
    return redirect(url_for("dashboard_campaign", campaign=campaign))


@app.route("/sync-all", methods=["POST"])
@live_login_required
def sync_all():
    """Force-refresh and cache every campaign's data in one go, so the
    offline view has a complete, up-to-date snapshot to fall back on."""
    sid, username, password = current_creds()
    try:
        _, campaign_rows = cached_query(
            username, password, "campaigns", CAMPAIGN_LIST_QUERY, force_refresh=True
        )
        campaigns = [r["Campaign_Name"] for r in campaign_rows]
        for c in campaigns:
            cached_query(username, password, "disposition", DISPOSITION_QUERY,
                         campaign=c, params=[c], force_refresh=True)
            cached_query(username, password, "calling", CALLING_REPORT_QUERY,
                         campaign=c, params=[c], force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        return render_template("error.html", message=str(exc))

    return redirect(url_for("dashboard_campaign", campaign=campaigns[0]) if campaigns else url_for("dashboard_root"))


@app.template_filter("pct_class")
def pct_class(value):
    """Classify a '45.2%' style string into low/mid/high for colour-coding."""
    if value is None:
        return "pct-neutral"
    try:
        number = float(str(value).replace("%", ""))
    except ValueError:
        return "pct-neutral"
    if number >= 60:
        return "pct-high"
    if number >= 30:
        return "pct-mid"
    return "pct-low"


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
