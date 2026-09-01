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
def data_route():
    # Mobile 	Amount	Name 
    # 8093330810	150000	Unknown
    # 8180814151	35000	OM PRAKASH
    # 8374026145	100000	LAKSHMANA RAO METTA
    # 9861138442	40000	Unknown
    # 8299642214	45000	Unknown
    # 7984906074	50000	Unknown
    # 9546042807	50000	Unknown
    # 9921685044	40000	Unknown
    # 6202991739	200000	Unknown
    # 9824194789	60000	Unknown
    # 6200130495	20000	AMIT  SINGH
    # 8861265435	30000	Unknown
    # 7751954352	35000	Unknown
    # 9602997102	100000	Unknown
    # 7865989570	15000	Unknown
    # 8826126027	300000	Unknown
    # 9325421801	100000	Unknown
    # 9702075009	40000	Unknown
    # 9146358286	15000	Unknown
    # 8630386910	50000	Unknown
    # 9699961444	100000	Unknown
    # 9995530230	75000	Unknown
    # 9718546301	7000	Unknown
    # 8369548923	60000	Unknown
    # 9836339169	50000	Unknown
    # 9507360558	100000	Unknown
    # 8686648300	40000	Unknown
    # 9165896834	50000	Unknown
    # 9986764882	50000	Unknown
    # 8007569241	50000	Unknown
    # 9466163050	150000	RAJENDER RAJENDER
    # 8523809027	50000	Unknown
    # 9518128785	40000	Unknown
    # 7348632120	75000	Unknown
    # 8885757378	50000	SURESH KUMAR ILLINGI
    # 9898541657	30000	Unknown
    # 9111973570	10000	Unknown
    # 7620550570	75000	Unknown
    # 9792542604	75000	Unknown
    # 8962133786	75000	Unknown
    # 8208008822	50000	Unknown
    # 9880504377	40000	Unknown
    # 8780997116	20000	Unknown
    # 9835242178	300000	GOVIND  BARNWAL
    # 9924931815	15000	Unknown
    # 7020227375	25000	PRADIP LOKESH KHOBRAGADE
    # 9989430803	50000	Unknown
    # 9591815576	20000	Unknown
    # 9441260630	60000	Unknown
    # 9022549571	7000	MINAKSHI PRASHANT SALUNKE
    # 9754728715	30000	Unknown
    # 9527070779	100000	Unknown
    # 9956431900	50000	Unknown
    # 8084476756	15000	Unknown
    # 9380098624	20000	Unknown
    # 6350649680	20000	CHENA RAM
    # 7219616558	15000	Unknown
    # 9921464379	15000	Unknown
    # 6371541301	25000	Unknown
    # 9762271618	60000	Unknown
    # 7004971378	100000	Unknown
    # 8853822358	50000	ASHVANI  KUMAR
    # 7091543626	20000	Unknown
    # 8108507350	40000	Unknown
    # 9915984844	75000	Unknown
    # 8156063404	100000	Unknown
    # 7031992667	40000	BISWARUP MONDAL
    # 9657495108	40000	Unknown
    # 8007918283	60000	Unknown
    # 9989150854	75000	Unknown
    # 6260730038	50000	Unknown
    # 9365745097	75000	Unknown
    # 9632421568	75000	Unknown
    # 6009325003	7000	Unknown
    # 9460529522	7000	JAIRAM GIDWANI
    # 8092125573	40000	Unknown
    # 9766062264	50000	Unknown
    # 9555388885	50000	Unknown
    # 7987450233	150000	Unknown
    # 9660060190	45000	Unknown
    # 7671040799	150000	HARISH DEVARASHETTI
    # 9333658096	45000	Unknown
    # 8015571857	100000	Unknown
    # 9110908958	30000	Unknown
    # 7043095623	40000	Unknown
    # 6353438907	30000	Unknown
    # 9359644071	50000	Unknown
    # 6202347595	30000	Unknown
    # 7988205442	15000	Unknown
    # 7495096929	75000	Unknown
    # 8709373091	300000	Unknown
    # 8837790992	60000	Unknown
    # 7499950823	65000	Unknown
    # 9536429441	200000	Unknown
    # 7052727261	75000	Unknown
    # 7383150615	75000	ATEL  CHIRAGKUMAR SHAILESHBHAI P
    # 7339365500	40000	Unknown
    # 6307344894	30000	AFJAL AHMAD
    # 9096555584	75000	Unknown
    # 9029272805	15000	Unknown
    # 8874645261	7000	Unknown
    # 7038177605	15000	Unknown
    # 9526533709	50000	Unknown
    # 7337429776	150000	Unknown
    # 7607580586	25000	Unknown
    # 8806624577	275000	Unknown
    # 9771367163	150000	Unknown
    # 8107367074	10000	Unknown
    # 9480090017	100000	Unknown
    # 6350241563	25000	Unknown
    # 7093107103	100000	Unknown
    # 8511549286	15000	Unknown
    # 6395795083	200000	Unknown
    # 9849866109	60000	Kolla uday bhaskar
    # 6289140293	15000	Unknown
    # 9590365057	15000	Unknown
    # 9953641757	180000	Unknown
    # 8620827739	75000	Unknown
    # 8090444677	40000	Unknown
    # 6363921135	20000	Unknown
    # 8455804377	50000	Unknown
    # 9030533997	200000	Unknown
    # 9608798346	7000	Unknown
    # 9177476443	25000	Unknown
    # 9902967626	50000	Unknown
    # 6393565593	50000	Unknown
    # 8858614347	150000	Unknown
    # 6207132977	25000	Unknown
    # 6201719127	100000	ABHISHEK KUMAR
    # 8535002589	30000	Unknown
    # 9591955534	45000	Unknown
    # 9904216133	40000	Unknown
    # 9910136430	235000	Unknown
    # 9769659377	25000	Unknown
    # 8825212383	10000	Unknown
    # 8469191309	50000	Unknown
    # 6206018873	50000	Unknown
    # 9985047063	100000	Unknown
    # 8419956618	50000	Unknown
    # 9703803809	65000	Unknown
    # 9742909676	7000	Unknown
    # 9495644822	20000	Unknown
    # 7870145860	15000	Unknown
    # 8848662312	25000	Unknown
    # 7359958012	75000	VISHALGIRI GULABGIRI MEGHNATHI
    # 8107806748	20000	Unknown
    # 9623894425	15000	Unknown
    # 9637920040	20000	Unknown
    # 9004488722	20000	Unknown
    # 9648030934	30000	Unknown
    # 8126812268	200000	Unknown
    # 9666031583	25000	Unknown
    # 9661669090	30000	Unknown
    # 7355212924	40000	Unknown
    # 7435916423	10000	Unknown
    # 7735021478	50000	NILU BEHERA
    # 8757381784	50000	Unknown
    # 9591324272	40000	Unknown
    # 9414000788	300000	Unknown
    # 7249684384	50000	Unknown
    # 6266230752	40000	Unknown
    # 9658272088	75000	SASI KANTA PRADHAN
    # 9887408040	40000	Unknown
    # 6206018873	200000	Unknown
    # 6375959356	40000	Unknown
    # 9308558382	75000	Unknown
    # 9654621097	7000	Unknown
    # 8580787153	15000	Unknown
    # 9492842247	30000	Unknown
    # 9832876012	40000	Unknown
    # 8866132006	75000	Thakor Akash Bharatbhai
    # 8624950989	100000	KUMAR  MOHAN
    # 9011544465	40000	Unknown
    # 9461850502	120000	DEVDUTT SINGH RAJAWAT
    # 9098548349	200000	Unknown
    # 7011958198	50000	Unknown
    # 8898548518	20000	Unknown
    # 9471131133	7000	Unknown
    # 8960379725	5000	Unknown
    # 9738237025	40000	Unknown
    # 9742538689	40000	Unknown
    # 9879267384	200000	Unknown
    # 7352151854	10000	Unknown
    # 7888189802	50000	Unknown
    # 9836190238	20000	Unknown
    # 9595627740	7000	RAJKUMAR RANGRAO PATIL
    # 9932031721	65000	Unknown
    # 6381346461	120000	Unknown
    # 7350411390	30000	Unknown
    # 7300571093	60000	Unknown
    # 7739236602	30000	Unknown
    # 9680404591	20000	Unknown
    # 9826389594	10000	Unknown
    # 9966933569	75000	Unknown
    # 9632365571	100000	Unknown
    # 9931784445	30000	Unknown
    # 9642215136	50000	PATTIPAKA SUBRAMANYAM
    # 9923066754	40000	Unknown
    # 8108832854	75000	Unknown
    # 9435398152	75000	Unknown
    # 7096980106	40000	Unknown
    # 8668939856	40000	MAHTAB HASAN KHAN MAHBUBULHASAN KHAN
    # 9050247205	30000	Unknown
    # 7300713562	40000	Unknown
    # 8630926944	150000	Unknown
    # 8129237103	60000	Unknown
    # 9979931293	50000	Unknown
    # 8637289606	40000	Unknown
    # 6000834645	20000	Unknown
    # 8562831539	70000	Unknown
    # 9324614346	50000	Unknown
    # 8866336083	75000	Unknown
    # 8522988595	20000	Unknown
    # 9265903215	120000	Unknown
    # 8969917165	200000	Unknown
    # 9845576643	100000	Unknown
    # 9662870098	300000	Unknown
    # 8330853850	40000	Unknown
    # 9776977886	50000	Unknown
    # 7974767952	30000	NARENDRA YADAV
    # 7326803170	60000	Unknown
    # 9992912443	25000	CHHOTELAL CHHOTELAL
    # 7617619108	150000	Unknown
    # 9819706836	60000	Unknown
    # 7974528598	7000	NAROTTAM JATAV
    # 9993733615	25000	PRAKASH MANDAL
    # 9351095313	40000	Unknown
    # 8955880290	40000	Unknown
    # 7989646617	10000	Unknown
    # 9518836358	75000	Unknown
    # 8660935094	100000	Unknown
    # 9932225270	7000	Unknown
    # 8369470870	120000	Unknown
    # 8369498519	200000	Unknown
    # 9758419455	60000	Unknown
    # 9883392046	25000	Unknown
    # 7026049483	75000	Unknown
    # 9834650583	30000	Unknown
    # 8007986239	150000	Unknown
    # 8007986239	150000	Unknown
    # 8408021414	40000	Unknown
    # 9172265375	10000	Unknown
    # 8299813761	7000	Unknown
    # 8237358107	15000	Unknown
    # 7049934616	40000	Unknown
    # 9982281522	75000	Unknown
    # 8586873940	50000	Unknown
    # 7030421675	75000	Unknown
    # 9567784623	75000	Unknown
    # 9619167560	100000	SUBHASH  MISHRA
    # 7310821029	50000	Unknown
    # 7767981889	200000	LOKESH MEENA
    # 7417034295	60000	Unknown
    # 9359719332	120000	Unknown
    # 7836888232	50000	Unknown
    # 8080198332	100000	Unknown
    # 9972005863	100000	Unknown
    # 7276697882	200000	RAMDASJI KALATKAR PANKAJKUMAR
    # 9096791779	50000	Unknown
    # 9316593579	30000	Unknown
    # 7878553682	100000	Unknown
    # 7014776150	50000	Unknown
    # 8308656980	75000	Unknown
    # 9010905069	120000	Unknown
    # 7457895113	75000	Unknown
    # 7631764608	25000	Unknown
    # 7046908575	100000	Unknown
    # 7566221314	25000	Unknown
    # 9515551939	25000	Unknown
    # 6388379632	40000	Unknown
    # 9324650742	100000	Unknown
    # 8510971901	7000	Unknown
    # 9910136430	300000	Unknown
    # 9041562663	30000	Unknown
    # 8320975426	200000	Unknown
    # 7077402007	15000	Unknown
    # 6900796336	10000	Unknown
    # 8529999330	50000	Unknown
    # 7304814919	7000	Unknown
    # 7259839872	50000	Unknown
    # 8952056502	40000	SHER MOHAMMED
    # 9785684254	75000	KARAN SINGH DHOBI
    # 9887833947	40000	Unknown
    # 8434462754	50000	Unknown
    # 8459421945	20000	Unknown
    # 8240249239	45000	Unknown
    # 7201919517	50000	Unknown
    # 8638311995	95000	Unknown
    # 9137411959	15000	Unknown
    # 9887790971	95000	Unknown
    # 9660885673	10000	Unknown
    # 7002965143	40000	Unknown
    # 9141040630	40000	Unknown
    # 8572940894	15000	Unknown
    # 9890798841	50000	Unknown
    # 9324151478	50000	Unknown
    # 7829811322	7000	Unknown
    # 9860558786	7000	Unknown
    # 8299378709	5000	Unknown
    # 8208077811	25000	Unknown
    # 9777175502	10000	Unknown
    # 9869786760	20000	KAMAL  KISHORE
    # 6387939636	20000	Unknown
    # 9169375120	7000	Unknown
    # 9718038352	80000	Unknown
    # 9030867161	7000	Unknown
    # 9691887181	150000	Unknown
    # 8789374966	40000	Unknown
    # 9362310828	75000	Unknown
    # 9177746992	65000	Unknown
    # 6376207369	10000	Unknown
    # 9795891809	95000	Unknown
    # 9951195662	20000	Unknown
    # 9696459738	100000	RIZWAN  HABIB
    # 9422194177	20000	Unknown
    # 9887871002	50000	Unknown
    # 8758624429	7000	Unknown
    # 9011143577	200000	DIPAK PRAKASH GOSAVI
    # 8825181502	50000	Unknown
    # 9550620900	300000	Unknown
    # 9740787273	40000	Unknown
    # 9908279165	50000	Unknown
    # 9590982296	50000	Unknown
    # 7041575553	25000	Unknown
    # 7995161845	50000	Unknown
    # 9781483401	50000	Unknown
    # 9724737513	60000	VALAND  KANTIBHAI
    # 9880685803	55000	Unknown
    # 9672239713	60000	Unknown
    # 9199547679	200000	Unknown
    # 9773883794	50000	Unknown
    # 7509158127	80000	HARICHAND  SONKAR
    # 9732147308	15000	Unknown
    # 9399360984	100000	AMRIT CHOUDHARY
    # 7226848071	50000	Unknown
    # 6006875878	40000	Unknown
    # 9389501708	100000	Unknown
    # 9798982512	7000	Unknown
    # 7607002848	50000	Unknown
    # 9389099270	40000	Unknown
    # 9782891146	40000	Unknown
    # 8658980852	50000	Unknown
    # 9997466944	50000	Unknown
    # 9135117953	30000	Unknown
    # 7477233458	30000	Unknown
    # 6352982472	40000	Unknown
    # 9023223885	100000	Unknown
    # 9382831513	50000	Unknown
    # 9936925238	30000	Unknown
    # 8888498549	150000	Unknown
    # 8789662983	35000	Unknown
    # 9552485004	120000	Unknown
    # 7818087682	20000	Unknown
    # 9799196697	50000	Unknown
    # 9561885817	80000	Unknown
    # 6361831653	35000	RUSHI KUMAR SINGH
    # 8239254275	75000	Unknown
    # 9411220036	100000	Unknown
    # 6304569517	60000	Unknown
    # 7004763339	25000	Unknown
    # 9167411570	100000	Unknown
    # 7678074886	65000	Unknown
    # 9471564897	50000	Unknown
    # 9717534555	100000	Unknown
    # 8999292114	150000	Unknown
    # 9080997467	30000	SALIHA ABDUL KADHAR
    # 7250691396	60000	Unknown
    # 9518082611	5000	Unknown
    # 9321580584	35000	Unknown
    # 9880003250	150000	Unknown
    # 7667221301	10000	Unknown
    # 7870883936	75000	Unknown
    # 6397947543	35000	Unknown
    # 9769480549	100000	Unknown
    # 9860364953	50000	Unknown
    # 8780671254	100000	FARUKBHAI MEMON SOHIL
    # 8712918791	50000	Unknown
    # 7057428269	75000	Unknown
    # 9782807808	75000	Unknown
    # 9138595913	150000	Unknown
    # 9337704052	100000	Unknown
    # 9638179638	50000	Unknown
    # 8297839135	100000	Unknown
    # 7565915993	50000	Unknown
    # 7204640678	20000	Unknown
    # 6371161968	25000	Unknown
    # 9929637900	200000	Unknown
    # 9706440806	40000	Unknown
    # 8971962493	50000	Unknown
    # 8318950443	50000	Unknown
    # 9146376301	30000	Unknown
    # 9033431110	85000	Unknown
    # 9920513739	75000	Unknown
    # 9686793612	75000	Unknown
    # 9368875995	25000	Unknown
    # 9724729257	120000	Unknown
    # 9771224844	30000	Unknown
    # 6238696401	50000	KAUSHIK  MANDAL
    # 7983040983	100000	Unknown
    # 8879167957	50000	Unknown
    # 9163838308	165000	Unknown
    # 8087618433	100000	Unknown
    # 9870961950	100000	PRAVIN ANANT PATIL
    # 9874663519	50000	Unknown
    # 9887833947	65000	Unknown
    # 9874583659	25000	Unknown
    # 9507346030	75000	Unknown
    # 6260661081	10000	Unknown
    # 9004968108	35000	Unknown
    # 9005737227	60000	Unknown
    # 7717610096	50000	ROMY ROMY
    # 7698555346	100000	Unknown
    # 8788950855	50000	Unknown
    # 9989064301	120000	Unknown
    # 8688207845	15000	Unknown
    # 9675877746	50000	Unknown
    # 9822200012	100000	Unknown
    # 9359329334	95000	Unknown
    # 8218958017	75000	CHAND KURAISHI
    # 7757841960	60000	Unknown
    # 7008472213	100000	Unknown
    # 9977068133	75000	Unknown
    # 7060034860	50000	Unknown
    # 7859053080	50000	Unknown
    # 8086084139	30000	Unknown
    # 8677034589	50000	Unknown
    # 9960401254	200000	ABHIJEET GULABRAO CHAVAN
    # 9772750476	75000	Unknown
    # 8882846275	20000	Unknown
    # 9734032366	200000	Unknown
    # 9785569911	100000	Unknown
    # 9836322511	40000	Unknown
    # 7768873729	50000	Unknown
    # 9691313170	50000	Unknown
    # 6289741366	50000	Unknown
    # 8407031027	40000	Unknown
    # 9606509336	120000	Lincy Cp
    # 9050986028	100000	Unknown
    # 9884506332	35000	Unknown
    # 8884479938	100000	BASAVARAJ SHARNAYYA MATHAPATI
    # 8770650725	20000	Unknown
    # 7022183442	75000	Unknown
    # 7838438003	200000	Unknown
    # 6378484925	50000	Unknown
    # 9996546803	75000	Unknown
    # 9799411759	100000	Unknown
    # 8340295965	70000	Unknown
    # 9689334584	200000	Unknown
    # 8755004740	5000	Unknown
    # 6203370578	20000	Unknown
    # 9431415617	40000	LALAN KUMAR JHA
    # 7984005866	75000	Unknown
    # 9390077041	40000	Unknown
    # 9325760772	200000	Unknown
    # 7309852907	45000	Unknown
    # 7507711219	75000	Unknown
    # 8291008909	40000	Unknown
    # 9823758155	150000	Unknown
    # 6375745822	35000	Unknown
    # 9689039891	150000	Unknown
    # 9112393793	50000	Unknown
    # 9668380965	100000	Unknown
    # 9671522126	50000	Unknown
    # 9962494356	50000	Unknown
    # 9468865643	60000	Unknown
    # 8080988931	30000	Unknown
    # 9373953193	50000	MOHAN ARJUN RAHANE
    # 9312774249	50000	Unknown
    # 7042175257	75000	KUMAR  PANKAJ
    # 7757832444	40000	Unknown
    # 9916355377	50000	RAVI C
    # 8790950545	50000	RANJIT KUMAR  LANKA
    # 7380480067	40000	Unknown
    # 7870719999	75000	SACHIN KUMAR
    # 8197713149	50000	Unknown
    # 9209948544	120000	Unknown
    # 7271009191	10000	Unknown
    # 9546785933	60000	Unknown
    # 9713470102	50000	Unknown
    # 9702557874	40000	Unknown
    # 9896037313	100000	Unknown
    # 7737932029	75000	CHANDRA SHEKAR SOLANKI
    # 7737522719	40000	Unknown
    # 8793710848	40000	Unknown
    # 8788920642	50000	Unknown
    # 6397321293	15000	Unknown
    # 9699912724	120000	Unknown
    # 8375841943	100000	Unknown
    # 7010839653	150000	Unknown
    # 8813852273	100000	Unknown
    # 9989750802	75000	Unknown
    # 9062377022	50000	Unknown
    # 8447783268	25000	PANKAJ KUMAR
    # 9832010788	40000	Unknown
    # 7717372941	7000	Unknown
    # 7894553088	50000	Unknown
    # 6393905997	100000	Unknown
    # 9101619870	40000	Unknown
    # 6305454972	7000	Unknown
    # 9082984866	60000	DIPIKA MORE
    # 9558073291	35000	Unknown
    # 9610752863	40000	Unknown
    # 8409565657	30000	Unknown
    # 9766027382	60000	Unknown
    # 6380531793	10000	Unknown
    # 9927730315	50000	Unknown
    # 7405082620	50000	Unknown
    # 8099288288	50000	Unknown
    # 8439709620	100000	Unknown
    # 9828557222	45000	Unknown
    # 8853345896	100000	Unknown
    # 9332250869	100000	Unknown
    # 9973284381	200000	Unknown
    # 9524433198	200000	Unknown
    # 9572680110	100000	Unknown
    # 7007497872	35000	Unknown
    # 9537766130	35000	Unknown
    # 8899123873	100000	Unknown
    # 9178747120	40000	Unknown
    # 9667810282	50000	Unknown
    # 7827793168	50000	Unknown
    # 7903871516	15000	Unknown
    # 9140737834	50000	Unknown
    # 9016838003	40000	Unknown
    # 9829294482	150000	Unknown
    # 9640692941	200000	Unknown
    # 8104346592	10000	Unknown
    # 9060849850	50000	MOHAMMAD MOJAHID
    # 8433676776	55000	BINOY MATHEW PUTHENPURICKAL
    # 8980614453	300000	Unknown
    # 7666964100	60000	Unknown
    # 8791596853	40000	Unknown
    # 7981571990	50000	Unknown
    # 9717158753	75000	Aftab Qureshi
    # 9861199759	175000	Unknown
    # 9523152242	30000	VIKASH KUMAR RANA
    # 9769740089	15000	Unknown
    # 8877960536	65000	TAHERA PERWEEN
    # 9353783725	50000	Unknown
    # 7091304239	40000	Unknown
    # 9879057595	200000	Unknown
    # 6200411484	25000	Unknown
    # 8839467581	15000	Unknown
    # 9119068759	40000	Unknown
    # 7982911238	200000	HITESHI CLARE
    # 9113103895	50000	Unknown
    # 8390333682	50000	Unknown
    # 9836661480	75000	Unknown
    # 7057071556	40000	Unknown
    # 9063104036	10000	Unknown
    # 9511836118	50000	Unknown
    # 7356071113	75000	FAUSIYA SANOJ
    # 7395880683	50000	Unknown
    # 9389221232	35000	Unknown
    # 7881171520	15000	Unknown
    # 8789191632	75000	Unknown
    # 9777155526	50000	Unknown
    # 7086525917	75000	Unknown
    # 9700100243	150000	Unknown
    # 7706012887	40000	Unknown
    # 8001870160	5000	Unknown
    # 8308411946	35000	Unknown
    # 9900491551	40000	Unknown
    # 8928260935	120000	Unknown
    # 9917552925	75000	Unknown
    # 8790131214	75000	Unknown
    # 8850874610	10000	Unknown
    # 6203997607	10000	Unknown
    # 9023643796	75000	Unknown
    # 7973181757	75000	PREETINDER  SINGH
    # 7000221090	50000	Unknown
    # 9359333083	30000	Unknown
    # 9313446510	20000	Unknown
    # 9958834378	85000	Unknown
    # 7028366117	7000	Unknown
    # 7407494843	50000	Sanjeev Gajmer
    # 9304893005	40000	Unknown
    # 9892050199	15000	Unknown
    # 9059072638	50000	Unknown
    # 8095735545	50000	Unknown
    # 8309499640	50000	Unknown
    # 9490802592	55000	Unknown
    # 9719613784	10000	Unknown
    # 9638233362	75000	Unknown
    # 9015601490	150000	Unknown
    # 9567338335	10000	Unknown
    # 7740944636	210000	Unknown
    # 9764364873	25000	Unknown
    # 9445644844	50000	Unknown
    # 9404916246	75000	Unknown
    # 6002112090	40000	Unknown
    # 7053461943	100000	Unknown
    # 7488229122	120000	Unknown
    # 6351819174	20000	Unknown
    # 9405930061	20000	Unknown
    # 8801006245	50000	TALEB BIN MOHESIN
    # 7852925123	30000	Unknown
    # 6294719771	40000	Unknown
    # 8980044710	100000	Unknown
    # 8651839665	100000	MADAN KUMAR SONI
    # 7304170414	50000	Unknown
    # 9894369486	40000	Unknown
    # 9885842605	100000	Unknown
    # 9798475704	40000	Unknown
    # 7745858319	60000	Unknown
    # 7558408388	50000	Unknown
    # 9860919967	85000	Unknown
    # 9874583659	25000	Unknown
    # 9738348420	150000	Unknown
    # 8383981453	25000	Unknown
    # 8225879437	150000	Unknown
    # 7043328678	15000	Unknown
    # 8796740303	40000	Unknown
    # 7568496169	60000	Unknown
    # 7042736275	200000	AFSAR ALAM MOHD
    # 8670623908	60000	Unknown
    # 8002855087	25000	Unknown
    # 9543382558	100000	Unknown
    # 9789754281	50000	Unknown
    # 7676509665	7000	Unknown
    # 9178699533	50000	Unknown
    # 9137129439	100000	Unknown
    # 9625445273	50000	Unknown
    # 9336595981	50000	Unknown
    # 9594423929	40000	Unknown
    # 8154903635	120000	Unknown
    # 8105358289	150000	Unknown
    # 7611942158	50000	Unknown
    # 9373620944	10000	Unknown
    # 9035863646	35000	Unknown
    # 8191905397	50000	Unknown
    # 8154093453	40000	Unknown
    # 9885740077	40000	Unknown
    # 8906956420	50000	SUKUMAR MANNA
    # 9842526969	120000	Unknown
    # 9066274231	50000	Unknown
    # 9007384636	7000	Unknown
    # 9649343075	50000	NARESH KUMAR
    # 9753355010	30000	Unknown
    # 9164803173	50000	Unknown
    # 7879746779	100000	Unknown
    # 9007158318	50000	Unknown
    # 9666809487	80000	Unknown
    # 7978197492	60000	Unknown
    # 7504661360	100000	Unknown
    # 8954001691	75000	Unknown
    # 9511524282	30000	Unknown
    # 8484050567	150000	AMIT  UPADHYE
    # 9122287000	75000	Unknown
    # 9330395807	100000	Unknown
    # 7621929837	50000	Unknown
    # 8948350550	30000	Unknown
    # 9785118371	100000	Unknown
    # 9373078719	25000	AJAY  MISHRA
    # 9783064965	60000	Unknown
    # 8860566860	7000	Unknown
    # 7631076052	50000	Unknown
    # 8005517748	50000	Unknown
    # 9663566701	150000	Unknown
    # 9059638253	300000	Unknown
    # 9501866688	200000	AMANDEEP SINGH
    # 7597281984	100000	Unknown
    # 9358450467	30000	Unknown
    # 9602817331	200000	SURENDRA  SINGH
    # 9475825223	15000	Unknown
    # 8696942745	55000	KALU SINGH RAWAT
    # 9866205028	15000	Unknown
    # 8368783573	5000	Unknown
    # 7015142541	200000	Unknown
    # 8828347541	10000	Unknown
    # 9140374536	50000	Unknown
    # 8369850152	55000	AWADHRAJ RAMNATH YADAV
    # 8249738936	100000	Unknown
    # 9304427969	50000	Unknown
    # 8799260073	50000	Unknown
    # 9717534555	100000	Unknown
    # 7738494388	40000	Unknown
    # 9328758560	10000	Unknown
    # 7304769918	25000	AMOL UTTAM SONAWANE
    # 8754580602	300000	Unknown
    # 6355195066	50000	Unknown
    # 8630887049	60000	Unknown
    # 9631016264	20000	Unknown
    # 7522886497	20000	Unknown
    # 9199007787	35000	Unknown
    # 9733514713	50000	Sudipta Manna
    # 9472229577	50000	Unknown
    # 8178366033	15000	Unknown
    # 9337315007	40000	KRUSHNA CHANDRA SWAIN
    # 8652367750	100000	Unknown
    # 7873491124	75000	Unknown
    # 9028810477	90000	Unknown
    # 9774656247	20000	Unknown
    # 9044816228	40000	Unknown
    # 6388855707	200000	Unknown
    # 6375173272	15000	Unknown
    # 9720615651	30000	Unknown
    # 8107332115	45000	Unknown
    # 6390570860	40000	Unknown
    # 9818791366	200000	HARVINDER
    # 8225813479	15000	Unknown
    # 9511292952	7000	Unknown
    # 9772195571	100000	Unknown
    # 8890024301	50000	Unknown
    # 8017320019	150000	Shabnum Parveen
    # 9817136320	50000	Unknown
    # 6387134258	75000	Unknown
    # 8398880009	70000	SHAKIL SHAKIL
    # 9735597615	120000	Unknown
    # 9743083922	10000	Unknown
    # 9659324912	50000	Unknown
    # 9672574465	50000	Unknown
    # 7073707976	50000	Unknown
    # 9485159086	50000	Unknown
    # 8018831743	45000	Unknown
    # 7715894524	50000	Unknown
    # 7742004537	35000	Unknown
    # 9784561167	105000	Unknown
    # 9771099845	7000	Unknown
    # 7339272430	40000	KUNDAN  KUMAR
    # 7790981786	45000	Unknown
    # 9671416112	40000	Unknown
    # 7648996154	50000	Unknown
    # 9594697497	30000	Unknown
    # 9082972798	7000	Unknown
    # 9717781739	30000	Unknown
    # 8849623048	100000	Unknown
    # 7541974557	15000	Unknown
    # 9937813668	100000	Unknown
    # 9654643933	300000	Unknown
    # 8668740563	30000	Unknown
    # 8219553737	50000	Unknown
    # 7046908575	100000	Unknown
    # 9835519837	15000	Unknown
    # 9711989169	60000	Unknown
    # 9741446591	50000	Unknown
    # 8421949394	300000	Unknown
    # 9015970695	40000	Unknown
    # 9784605486	20000	Unknown
    # 9989235754	50000	Unknown
    # 8755257788	15000	Unknown
    # 9130641802	50000	Unknown
    # 9890355790	40000	PRASHANT SUKHDEO GEDAM
    # 7018309008	5000	Unknown
    # 9079649247	150000	Unknown
    # 6203798473	45000	Unknown
    # 9937654263	50000	TRIPATI KUMAR  PATRO
    # 6304321150	75000	Unknown
    # 8097732756	100000	Unknown
    # 9778043569	7000	MALAYA KUMAR NAYAK
    # 8561929502	75000	Unknown
    # 9540344645	90000	Unknown
    # 9880573937	30000	Unknown
    # 8970126424	50000	Unknown
    # 9643860127	55000	Unknown
    # 8073822240	50000	Unknown
    # 8793922429	40000	Unknown
    # 9996091296	135000	Unknown
    # 9106217359	30000	Unknown
    # 7219104297	60000	Unknown
    # 8001506632	50000	Unknown
    # 7747982169	25000	Unknown
    # 9870204393	40000	Unknown
    # 7000719900	100000	Unknown
    # 9030941964	150000	Unknown
    # 8688902265	40000	Unknown
    # 9836473726	50000	Unknown
    # 8638047853	75000	Unknown
    # 9755829634	40000	Unknown
    # 7567105487	40000	Unknown
    # 9067663292	40000	Unknown
    # 6354104402	30000	Unknown
    # 9834248295	40000	Unknown
    # 9247775252	50000	Unknown
    # 9816855706	50000	Unknown
    # 9251724912	40000	Unknown
    # 7634975657	25000	Unknown
    # 9369131445	10000	Unknown
    # 8619299158	75000	SONU KUMAR
    # 7772070207	45000	Unknown
    # 7903209116	50000	Unknown
    # 9921967514	200000	Unknown
    # 7571972116	10000	KAUSHAL KISHOR SINGH
    # 9587975182	65000	Unknown
    # 8179306824	150000	Unknown
    # 8019326781	195000	Unknown
    # 8789666010	100000	Unknown
    # 7411981944	75000	Unknown
    # 7976105312	7000	LALCHAND  SHARMA
    # 8885829192	40000	Unknown
    # 7889857224	45000	Unknown
    # 8120849424	25000	Unknown
    # 6386066450	50000	Unknown
    # 7396254779	200000	Unknown
    # 9686953106	100000	Unknown
    # 8055405969	40000	Unknown
    # 9065655918	15000	Unknown
    # 8790323407	100000	Unknown
    # 9036856355	40000	Unknown
    # 8567943530	150000	Unknown
    # 9934133939	10000	Unknown
    # 9110996594	45000	JAY PRAKASH MAHATO
    # 8097107425	300000	ROHIT RAJARAM BHILARE
    # 9346467386	75000	Unknown
    # 8171387806	50000	Unknown
    # 9823457919	300000	Unknown
    # 7737942019	40000	Unknown
    # 6392048955	50000	Unknown
    # 9426424495	50000	Unknown
    # 9398209832	110000	Unknown
    # 6207328352	40000	REHANA NASREEN
    # 8294989557	120000	Unknown
    # 8585978120	30000	Unknown
    # 7600788012	75000	Unknown
    # 9875207791	10000	Unknown
    # 7385916044	50000	Unknown
    # 9827252755	50000	Unknown
    # 9594453863	100000	Unknown
    # 9521139617	35000	Unknown
    # 7872171798	20000	Unknown
    # 7330747412	50000	MOHAMMED ALI
    # 8697773465	120000	Unknown
    # 8148723054	300000	Unknown
    # 9958602472	150000	Unknown
    # 9905146789	200000	Unknown
    # 7070752383	15000	Unknown
    # 8578816651	65000	Unknown
    # 9784630269	120000	Unknown
    # 7751915603	25000	Unknown
    # 7702668063	40000	Unknown
    # 9575546488	50000	Unknown
    # 9772266051	75000	Unknown
    # 8229836441	15000	Unknown
    # 9815773041	30000	Unknown
    # 9610735413	30000	Unknown
    # 8132967600	75000	Unknown
    # 9992997106	200000	Unknown
    # 8999500456	200000	Unknown
    # 9449605012	50000	Unknown
    # 7797245771	10000	Unknown
    # 9123284203	10000	Unknown
    # 9568164032	150000	Unknown
    # 7288831248	30000	ZAREENA BEGUM
    # 9915571454	120000	Unknown
    # 9992997106	50000	Unknown
    # 8279376767	100000	Unknown
    # 9289731995	30000	Unknown
    # 7350911951	15000	Unknown
    # 9440118480	75000	Unknown
    # 9800106058	7000	Unknown
    # 9666262314	120000	Unknown
    # 9733277180	30000	MITHU KUMAR SARKAR
    # 8126101012	20000	Unknown
    # 9985943606	75000	Unknown
    # 7090537140	15000	Unknown
    # 9827442210	50000	SANJAY RORIYA
    # 7895717356	50000	DHARMENDRA DHARMENDRA
    # 7086615014	40000	Unknown
    # 7835964003	65000	Unknown
    # 7891815719	95000	Unknown
    # 9826648852	150000	Unknown
    # 9340302525	30000	Unknown
    # 8492921314	85000	Unknown
    # 7829066928	100000	Unknown
    # 9156074402	200000	Unknown
    # 9199404250	120000	Unknown
    # 7448925432	40000	Unknown
    # 8528282825	50000	BALWINDER SINGH
    # 7661042828	60000	Unknown
    # 8178819902	30000	Unknown
    # 8102559965	50000	PRAKASH  KUMAR
    # 7907999167	50000	Unknown
    # 9027755591	50000	Mohammad Irfan
    # 8892444579	50000	Unknown
    # 8083842250	40000	Unknown
    # 7001209212	45000	SOURAV ROY
    # 7660096563	100000	Unknown
    # 8126180540	55000	Unknown
    # 9582309587	125000	Unknown
    # 8427906235	25000	Unknown
    # 7020078303	200000	Unknown
    # 9558348390	150000	Unknown
    # 7075032732	10000	Unknown
    # 7891754922	25000	Unknown
    # 8446317030	50000	Unknown
    # 9368238621	20000	Unknown
    # 9724297522	50000	Unknown
    # 9022537966	50000	Unknown
    # 7500606621	50000	Unknown
    # 7368997662	75000	Unknown
    # 8707469650	15000	Unknown
    # 9534146989	25000	Unknown
    # 9725152990	50000	Unknown
    # 8806661762	100000	Unknown
    # 8317531405	50000	Unknown
    # 8762064700	60000	Unknown
    # 7774810171	7000	Unknown
    # 8882888314	100000	Unknown
    # 9713155552	120000	Unknown
    # 9544150274	75000	Unknown
    # 9133251319	25000	Unknown
    # 9437835084	35000	Unknown
    # 9955723641	50000	Unknown
    # 7028621122	80000	Unknown
    # 7588385141	15000	Unknown
    # 9993103691	30000	Unknown
    # 9938055658	45000	BASANTA KUMAR NAYAK
    # 8328149018	40000	Unknown
    # 9494927030	300000	Unknown
    # 8077995904	40000	Unknown
    # 6302803883	60000	Unknown
    # 8952036304	65000	Unknown
    # 8754583712	75000	Unknown
    # 9529207292	75000	Unknown
    # 9536780686	50000	SALIM  KHAN
    # 9310596620	50000	Unknown
    # 7982894105	60000	Unknown
    # 8789577852	50000	Unknown
    # 8528110110	20000	Unknown
    # 7975700274	50000	SHANTHA  RAJU
    # 9924793764	50000	Unknown
    # 7499516262	20000	Unknown
    # 7795211957	200000	Unknown
    # 9777530515	50000	Unknown
    # 9839064574	40000	Unknown
    # 7995254033	40000	Unknown
    # 9794389250	150000	Unknown
    # 8879998944	135000	Unknown
    # 6001929248	50000	Unknown
    # 9561098149	60000	Unknown
    # 7028206091	200000	Unknown
    # 8115179606	150000	Unknown
    # 7505508655	50000	NEELU SINGH
    # 9837892025	40000	Unknown
    # 9513489582	15000	Unknown
    # 7008472213	200000	Unknown
    # 7280921582	50000	Unknown
    # 9373709358	50000	Unknown
    # 9637375739	150000	HUSEN AHAMADSAB  SHAIKH
    # 9327422783	35000	Unknown
    # 7093499868	50000	Unknown
    # 9920112560	45000	Unknown
    # 9975809023	50000	RAHUL POPATRAO PAWAR
    # 9304783214	50000	Unknown
    # 8624090428	75000	Unknown
    # 9738368699	150000	Unknown
    # 8975985452	100000	SHIVAJI BHIVARAJ GADEKAR
    # 9805376712	75000	Unknown
    # 9986270112	150000	Unknown
    # 9174587560	35000	Unknown
    # 9549500360	150000	Unknown
    # 9150978587	30000	Unknown
    # 6398787220	7000	Unknown
    # 9452179434	30000	Unknown
    # 6361974504	35000	Unknown
    # 9911934702	25000	Unknown
    # 7600426001	75000	ARVINDBHAI R GIRI
    # 9222117888	260000	Unknown
    # 9300033916	7000	Unknown
    # 7004543266	35000	Unknown
    # 6203910435	200000	Unknown
    # 9714159836	150000	Unknown
    # 7293152376	7000	Unknown
    # 7424938645	20000	Unknown
    # 9901889280	20000	Unknown
    # 9944946898	35000	Unknown
    # 8800150148	20000	Unknown
    # 8308845777	50000	Unknown
    # 8895494458	55000	BHAGBAN CHANDRA LENKA
    # 9377687736	60000	SHAMSUDDIN ANVARBHAI SHAIKH
    # 8373945758	200000	Unknown
    # 9265183802	25000	Unknown
    # 7351646280	25000	Unknown
    # 9931610257	40000	Unknown
    # 9391498830	50000	Unknown
    # 9949195427	20000	Unknown
    # 9457376562	10000	Unknown
    # 9958944930	10000	Unknown
    # 7380604886	30000	ANIL ANIL
    # 9761942178	30000	Unknown
    # 7458888582	70000	Unknown
    # 9045594743	150000	Unknown
    # 8879683314	40000	Unknown
    # 9870063100	75000	Unknown
    # 6350319460	50000	Unknown
    # 6363849077	35000	Unknown
    # 7906605966	200000	Unknown
    # 7359051046	30000	Unknown
    # 8421234109	20000	Unknown
    # 8805164132	20000	GAJANAN DATTA KOKATE
    # 8521056206	10000	Unknown
    # 7355603088	50000	Unknown
    # 8769397483	50000	Unknown
    # 9811045834	40000	Unknown
    # 8838464041	100000	Unknown
    # 7016916598	30000	Unknown
    # 9655568176	45000	Unknown
    # 9974902355	100000	Unknown
    # 9831997287	150000	Unknown
    # 7738755622	7000	Unknown
    # 6201079881	35000	Unknown
    # 8955377182	10000	Unknown
    # 8886025745	45000	Unknown
    # 9850357563	100000	Unknown
    # 9764025454	120000	Unknown
    # 9557809652	40000	Unknown
    # 9852601697	50000	Unknown
    # 8700734589	150000	CHANDAN  KUMAR
    # 9657256778	15000	ABDUL WAHAB ABDUL RAHAMAN
    # 7008305203	50000	Unknown
    # 6205451962	7000	Unknown
    # 9898619086	45000	ASFAK MUSA PATEL
    # 8976146684	100000	PRAVIN MANOHAR MANOHAR PAT
    # 8143177660	200000	MOHD PASHA
    # 7000063822	25000	Unknown
    # 9602429354	40000	Unknown
    # 9999597392	150000	Unknown
    # 8964028934	60000	Unknown
    # 9895254950	30000	Unknown
    # 9386337527	120000	Unknown
    # 9642472021	150000	Unknown
    # 7054867089	50000	Unknown
    # 9354711182	10000	YASHVARDHAN YASHVARDHAN
    # 9660362549	40000	Unknown
    # 7892195561	40000	Unknown
    # 8827446858	60000	Unknown
    # 8957489654	7000	Unknown
    # 8955690691	30000	MOHAN LAL
    # 9956851760	60000	Unknown
    # 7045175266	150000	Unknown
    # 7408148426	30000	Unknown
    # 7022571241	100000	Unknown
    # 7430916852	50000	Unknown
    # 9393538844	7000	Unknown
    # 6299228036	50000	Unknown
    # 8960334900	150000	Unknown
    # 9136185718	75000	Unknown
    # 7699298388	30000	Unknown
    # 7007264763	50000	Unknown
    # 9621373280	40000	BRINDABAN TRIPATHI
    # 9813410962	140000	Unknown
    # 9676787025	50000	Unknown
    # 9676787025	50000	Unknown
    # 9797875729	45000	Unknown
    # 9631473500	40000	Unknown
    # 9887408040	50000	Unknown
    # 7092167756	25000	Unknown
    # 7706994752	7000	Unknown
    # 9887341646	240000	MAHENDER KUMAR
    # 9960517333	145000	Unknown
    # 9314695839	200000	Unknown
    # 7715933626	7000	Unknown
    # 9728809223	20000	Unknown
    # 8073944521	150000	Unknown
    # 8390315164	7000	Unknown
    # 8058378289	200000	Unknown
    # 9518317557	85000	Unknown
    # 8853662794	15000	Unknown
    # 9081201866	75000	Unknown
    # 8618346183	200000	Unknown
    # 8892547781	50000	Unknown
    # 9637240191	20000	AJIT SURESH CHIKANKAR
    # 8953502487	150000	Unknown
    # 9982132815	50000	Unknown
    # 8861414104	25000	Unknown
    # 7991339375	40000	Unknown
    # 6354060461	40000	Unknown
    # 9810589407	20000	Unknown
    # 9319330068	40000	Unknown
    # 7383161242	7000	Unknown
    # 9833552432	150000	Unknown
    # 9920592232	150000	Unknown
    # 9933897908	65000	Unknown
    # 8604286875	15000	Unknown
    # 7903474854	200000	Unknown
    # 8817198445	45000	Unknown
    # 9986689993	120000	Unknown
    # 7398942006	15000	Unknown
    # 9705760498	150000	Unknown
    # 7983553345	5000	Unknown
    # 6000548439	35000	NUR AHMED
    # 7771880513	50000	Unknown
    # 9798856472	40000	Unknown
    # 9540113870	50000	Unknown
    # 8910040668	200000	Unknown
    # 7987844776	40000	SUNIL CHOLKAR
    # 7784086193	40000	Unknown
    # 7874134835	60000	Unknown
    # 8095389899	40000	Unknown
    # 7439579198	15000	Unknown
    # 8905682805	95000	Unknown
    # 8421130258	15000	Unknown
    # 8871889620	70000	Unknown
    # 8875436167	40000	Unknown
    # 9340498011	200000	Unknown
    # 7873418723	15000	Unknown
    # 8598828699	150000	Unknown
    # 8766209026	150000	Unknown
    # 9304226954	20000	Unknown
    # 6269613017	25000	Unknown
    # 9450692175	40000	KRIPA SHANKER PURI
    # 9358450467	50000	Unknown
    # 7007153468	50000	Unknown
    # 9963554649	70000	Unknown
    # 9652227040	100000	Unknown
    # 8755003881	50000	Unknown
    # 8866316090	55000	Unknown
    # 7543086415	100000	Narendra Pandit
    # 9110301191	50000	Unknown
    # 9164421562	40000	Unknown
    # 9067919124	15000	Unknown
    # 9099581463	50000	KIRANBHAI  DAMOR
    # 7874009553	20000	Unknown
    # 9648029047	50000	Unknown
    # 9340625760	40000	Unknown
    # 7490814161	30000	Unknown
    # 8169002949	50000	Unknown
    # 7702355327	75000	Unknown
    # 9999152812	50000	Unknown
    # 9912119069	75000	Unknown
    # 9661844598	35000	Unknown
    # 8755277048	75000	Unknown
    # 9769971289	185000	Unknown
    # 8650747930	75000	Toukeer
    # 7439279831	30000	Unknown
    # 9955839517	30000	Unknown
    # 8757380956	35000	Unknown
    # 7517632735	50000	Unknown
    # 9494584475	5000	MUKERA  MALLIKARJUNA
    # 9656391664	50000	Unknown
    # 7276717797	25000	Unknown
    # 8200210576	45000	Unknown
    # 9199242659	40000	Unknown
    # 7275231342	5000	Unknown
    # 9999843321	50000	Unknown
    # 9050205352	50000	Unknown
    # 9733113864	100000	Unknown
    # 9998125200	50000	Unknown
    # 8922947634	150000	RAJESH KUMAR YADAV
    # 9998227032	50000	GANCHI ZAKIRHUSAIN
    # 9324120377	5000	Unknown
    # 9939578080	100000	Unknown
    # 8606833368	25000	Unknown
    # 7392871087	50000	Unknown
    # 9128153125	20000	Unknown
    # 9002420526	100000	Unknown
    # 8121775022	55000	Unknown
    # 9039859985	150000	SONI  AMIT
    # 9004642330	50000	Unknown
    # 9570180374	65000	IDRISH ANSARI
    # 9805321349	15000	SANTOSH KUMAR SINGH
    # 9829877558	110000	Unknown
    # 9323032679	60000	Unknown
    # 9899440017	15000	Unknown
    # 9810017173	75000	Unknown
    # 9540003948	75000	Utpal Jha
    # 8109205138	50000	Unknown
    # 8650000406	200000	Unknown
    # 7086342164	50000	Unknown
    # 9868024234	10000	Unknown
    # 8238448860	50000	PRADIP KUMAR BAMAN RAUTARAY
    # 9400841366	50000	Unknown
    # 9886976457	40000	Unknown
    # 7537055677	150000	Unknown
    # 7999240396	150000	SANJAY BAIRAGI
    # 6306368207	35000	MOHIT PAL
    # 8959601811	50000	Unknown
    # 9956323353	75000	Unknown
    # 9109122277	75000	Rohan Kumar Pandey
    # 7411798253	50000	Unknown
    # 7002826521	15000	Unknown
    # 7477219699	40000	Unknown
    # 9905626533	5000	VIKAS  KUMAR
    # 9686683566	50000	Unknown
    # 6294946685	30000	Unknown
    # 8826203835	30000	Unknown
    # 6379394663	125000	Unknown
    # 9901732956	120000	Unknown
    # 8002630038	100000	MOHAMMAD GYASUDDIN
    # 8237396795	200000	Unknown
    # 9050294854	90000	Unknown
    # 9587828054	30000	Unknown
    # 9775083908	75000	BALLAL MOLLA
    # 9146376301	30000	Unknown
    # 9898881709	150000	Unknown
    # 9759653934	45000	Unknown
    # 8102563013	15000	Unknown
    # 8691818830	150000	Unknown
    # 7869695349	50000	Unknown
    # 6260000961	45000	MOHAMMAD SABIR
    # 9790812191	150000	Unknown
    # 9867835411	60000	Unknown
    # 9798940638	40000	Unknown
    # 6267340631	30000	RAM SEWAK
    # 9985260764	100000	Unknown
    # 7667389134	50000	Unknown
    # 7600498802	10000	Unknown
    # 9985047063	100000	Unknown
    # 9587664141	15000	Unknown
    # 9823783568	30000	Unknown
    # 9955873286	40000	Unknown
    # 9795802012	35000	Unknown
    # 9505460270	20000	Unknown
    # 9949440754	20000	Unknown
    # 9887306980	50000	Unknown
    # 8052078779	10000	Unknown
    # 8806883362	20000	Unknown
    # 9897936707	100000	Unknown
    # 9414656574	7000	Unknown
    # 7732920845	100000	SATISH KUMAR
    # 8848048662	15000	Unknown
    # 9632259651	15000	Unknown
    # 9664414859	10000	Unknown
    # 9830464403	50000	FULLARA DHAR
    # 9398226731	200000	Unknown
    # 7033869715	40000	Unknown
    # 8551059738	100000	Unknown
    # 9840028410	40000	Unknown
    # 9925628805	200000	Unknown
    # 9822370635	5000	RAVINDRA DHARMARAJ YADAV
    # 9600192002	60000	Unknown
    # 6303470671	75000	Unknown
    # 8130895215	150000	Unknown
    # 9759995585	75000	Unknown
    # 9740757254	150000	Unknown
    # 8892668677	50000	IMRAN  AHMED
    # 9561151121	75000	Unknown
    # 8417829721	50000	Unknown
    # 7600277073	65000	Unknown
    # 9602202767	30000	Unknown
    # 9931807977	50000	Unknown
    # 9036227165	60000	Unknown
    # 9373434834	15000	Unknown
    # 9898687602	150000	Unknown
    # 7989354449	25000	Unknown
    # 8618122702	50000	Unknown
    # 9859338230	75000	Unknown
    # 8260777970	60000	Unknown
    # 6361008773	20000	BASHASAB MULLA
    # 9500416556	75000	Unknown
    # 9449317076	50000	JAHANGEER BELARI
    # 8390717086	150000	Unknown
    # 9924243144	75000	Unknown
    # 9730129241	30000	Unknown
    # 9970171746	25000	SHARMA PRATISHTHA
    # 7385685654	50000	Unknown
    # 8825370589	50000	Unknown
    # 6398431077	50000	Unknown
    # 8755192472	20000	Unknown
    # 6363004494	100000	Unknown
    # 9579855590	40000	Unknown
    # 9332176721	40000	Unknown
    # 7041598448	7000	Unknown
    # 7631673888	7000	Unknown
    # 8450929926	50000	Unknown
    # 6202206587	10000	Unknown
    # 9273786519	75000	Unknown
    # 7722007618	50000	Unknown
    # 7652057673	30000	NIKHIL AGARWAL
    # 7063692062	35000	Unknown
    # 9959855100	150000	Unknown
    # 9958714901	300000	Unknown
    # 7972764250	100000	Unknown
    # 8088649007	60000	RAMAYYA PRAMOD
    # 8605533460	50000	Unknown
    # 8057652187	40000	Unknown
    # 9784830794	100000	BIHARI LAL BANJARA  BANJARA
    # 9582128000	40000	Unknown
    # 8108128107	45000	Unknown
    # 9024357764	40000	Unknown
    # 8447938081	200000	JAIPAL JAIPAL
    # 9080910849	7000	Unknown
    # 9149011544	20000	Unknown
    # 9034293951	60000	Unknown
    # 8650943158	100000	Unknown
    # 7737423681	30000	Unknown
    # 9707595920	40000	Unknown
    # 7975264916	125000	Unknown
    # 8466842019	20000	Unknown
    # 7828691545	200000	Unknown
    # 9493625439	50000	Unknown
    # 9617814665	25000	Unknown
    # 9892519470	15000	Unknown
    # 7974353716	40000	Unknown
    # 7261046788	200000	Unknown
    # 9908149129	7000	Unknown
    # 9441858564	150000	Unknown
    # 6203387304	110000	Unknown
    # 9867130013	45000	Unknown
    # 9829828400	50000	Unknown
    # 8085441737	150000	RAJU SANODIYA
    # 8210914464	75000	MD  HUMAYUN
    # 7597661961	60000	Unknown
    # 9154443429	50000	Unknown
    # 8006984101	7000	Unknown
    # 8979901078	30000	Unknown
    # 9773253327	30000	Unknown
    # 7306894757	50000	Unknown
    # 8971511871	30000	Unknown
    # 7808629716	50000	Unknown
    # 7352926680	65000	Unknown
    # 8105804497	20000	BADDI  VINOD
    # 9879261590	150000	Unknown
    # 9824703903	10000	Unknown
    # 9892113572	90000	Unknown
    # 9167633988	40000	Unknown
    # 7011816751	7000	Unknown
    # 9082122019	15000	Unknown
    # 6363221585	40000	Unknown
    # 6307352804	10000	RAM  BHUPENDRA KUMAR SO PARASU
    # 9032676123	30000	Unknown
    # 9819254085	50000	Unknown
    # 9978234869	150000	JANPAVALA  VALIBHAI
    # 7207684692	20000	VIJAY YADAV
    # 9832915371	125000	Unknown
    # 7506095116	40000	Unknown
    # 9141805219	50000	Unknown
    # 9835535144	50000	Unknown
    # 9121673767	75000	Unknown
    # 9938521926	200000	Unknown
    # 9468456699	200000	Unknown
    # 9492678106	50000	Unknown
    # 9896514486	15000	JWALA KUMAR SINGH
    # 8431710007	50000	Unknown
    # 8233501068	150000	Harinarayan Khinchi
    # 7517839631	60000	Unknown
    # 9142673628	40000	MOHAMMAD SHAMSHAD KURAISI
    # 7310836969	60000	PREETI  TOMAR
    # 9958865722	75000	Unknown
    # 9891666476	5000	Unknown
    # 9503089488	40000	Unknown
    # 7083430567	40000	Unknown
    # 9657723983	300000	SUCHITA SATISH HULE
    # 7030290091	50000	Unknown
    # 7814039720	40000	Unknown
    # 7002976977	40000	Unknown
    # 8103631316	15000	Unknown
    # 9397654658	30000	Unknown
    # 9125994290	95000	Unknown
    # 8505053934	25000	Unknown
    # 9934216600	120000	Unknown
    # 9920112560	45000	Unknown
    # 8006860875	200000	Unknown
    # 9328815905	50000	Unknown
    # 7004030069	60000	Unknown
    # 9382838476	40000	Unknown
    # 7499868834	10000	Unknown
    # 6302709057	75000	RAJESH  YADAV
    # 7505747959	20000	Unknown
    # 8383865067	35000	Unknown
    # 7838279089	75000	GUDDU  SINGH
    # 7599447180	60000	Unknown
    # 8107294536	100000	Unknown
    # 8874764279	75000	Unknown
    # 9702784527	200000	Unknown
    # 9123803130	50000	Unknown
    # 9762307497	100000	Unknown
    # 9130069948	100000	Unknown
    # 9769211353	300000	ASHUTOSH CHAURA
    # 9500679827	75000	Unknown
    # 7240578562	135000	Unknown
    # 9204710745	45000	Unknown
    # 7890213540	45000	Unknown
    # 8658362333	75000	Unknown
    # 9327677310	50000	Unknown
    # 9654120730	15000	MAHENDI HASAN
    # 9963916265	60000	Unknown
    # 9978494828	300000	Unknown
    # 9604085527	50000	Unknown
    # 9661980515	50000	Unknown
    # 9594808202	30000	Unknown
    # 9334844005	100000	MOHAMMAD NAUSHAD ALAM
    # 9113637707	60000	Unknown
    # 8787841279	50000	Unknown
    # 9910597704	90000	Unknown
    # 9703088200	100000	Unknown
    # 9284467818	60000	Unknown
    # 7500873156	35000	Unknown
    # 9823943353	25000	Unknown
    # 8340337988	45000	Unknown
    # 9672447771	15000	Unknown
    # 9893415792	50000	Unknown
    # 9058412494	20000	Unknown
    # 8928971711	20000	Unknown
    # 9824658937	50000	Unknown
    # 7019802300	105000	Unknown
    # 8600933672	25000	Unknown
    # 9437207633	40000	Unknown
    # 8208258790	50000	Unknown
    # 7257087592	7000	Unknown
    # 9556682255	50000	Unknown
    # 9717075179	75000	Unknown
    # 9545456467	100000	SAINATH SHAMARAO RANDIVE
    # 9105835911	100000	Unknown
    # 9105835911	100000	Unknown
    # 9951867354	40000	Unknown
    # 9879629394	150000	Unknown
    # 7448925432	50000	Unknown
    # 9782656244	150000	Unknown
    # 7905015327	75000	Unknown
    # 9145434345	50000	Unknown
    # 7433013566	50000	FASAHAT  SALMANI
    # 7872416043	40000	Unknown
    # 9828555780	15000	Unknown
    # 7798321692	150000	Unknown
    # 7208121230	7000	Unknown
    # 9096310295	40000	Unknown
    # 9000134132	100000	Unknown
    # 8747873887	10000	Unknown
    # 9199779575	50000	Unknown
    # 6268122711	15000	Unknown
    # 9771770591	40000	Unknown
    # 9440689685	150000	Unknown
    # 8975508382	7000	Unknown
    # 8559818271	40000	Unknown
    # 8308589355	150000	Unknown
    # 9640130997	100000	Unknown
    # 7905749926	40000	Unknown
    # 7875827810	10000	Unknown
    # 7866879270	40000	MINTU ADHIKARI
    # 9021498120	15000	Unknown
    # 7488452215	30000	Unknown
    # 9975144237	15000	Unknown
    # 9661463003	25000	Unknown
    # 9824073549	40000	Unknown
    # 7088291849	40000	Unknown
    # 7488133503	30000	Unknown
    # 8148723054	300000	Unknown
    # 8010208856	25000	Unknown
    # 7568735975	50000	Unknown
    # 8806318584	7000	SAYAD  YUSUF RUSTAMSAB ANIGIRI
    # 9060737960	35000	Unknown
    # 9009792308	50000	Unknown
    # 6364500398	75000	Unknown
    # 7596096607	30000	Unknown
    # 9654189865	40000	Unknown
    # 8186099309	50000	Unknown
    # 9772018690	35000	Unknown
    # 8951535892	120000	Unknown
    # 9642851234	15000	Unknown
    # 6207314927	15000	Unknown
    # 8809080616	150000	Unknown
    # 8638822536	150000	Unknown
    # 8887808170	40000	Unknown
    # 9174836798	200000	Unknown
    # 8083906240	60000	Unknown
    # 7568098307	75000	Unknown
    # 8050240753	15000	Unknown
    # 7970307576	35000	Unknown
    # 9671249360	50000	Unknown
    # 7257899737	10000	Unknown
    # 9948149894	15000	Unknown
    # 9390054405	60000	Unknown
    # 9090726135	50000	Unknown
    # 8867952117	35000	Unknown
    # 8950429074	25000	AJAY AJAY
    # 9748521497	35000	Unknown
    # 9164850247	7000	Unknown
    # 9950240057	50000	JAGDISH CHANDRA MEGHWAL
    # 6206028468	30000	Unknown
    # 9436935941	35000	Unknown
    # 8826049341	30000	SHAHID SHAHID
    # 7205796116	7000	Unknown
    # 7439489399	30000	Unknown
    # 9417518927	30000	RAJESH RAI
    # 8981869708	110000	Unknown
    # 9617504534	40000	Unknown
    # 9983278266	45000	Unknown
    # 9971695434	150000	DEEPAK  BHATI
    # 9311313193	120000	Unknown
    # 9966889186	200000	Unknown
    # 9163704662	40000	HALDER BABLU HALDER
    # 7666001546	200000	Unknown
    # 9024847481	10000	Unknown
    # 7017987537	200000	Unknown
    # 9834420627	60000	Unknown
    # 6204891266	75000	Unknown
    # 9038739410	40000	Unknown
    # 8678017448	50000	Unknown
    # 7284063681	40000	Unknown
    # 8452089191	100000	Unknown
    # 9662943316	50000	Unknown
    # 9326241480	200000	Unknown
    # 7620208017	7000	Unknown
    # 8280038758	60000	Unknown
    # 9490802592	30000	Unknown
    # 8894210408	15000	Unknown
    # 9754162447	150000	VIJAY VIJAY
    # 9875080752	50000	Unknown
    # 9134132148	35000	Unknown
    # 8780487951	20000	Unknown
    # 8669030894	40000	Unknown
    # 9616559940	35000	PRADEEP YADAV
    # 7014711208	35000	Unknown
    # 8813852273	100000	Unknown
    # 9101181958	50000	Unknown
    # 7249068396	50000	Unknown
    # 7042555418	150000	Unknown
    # 9619932856	40000	Unknown
    # 7661943938	35000	Unknown
    # 9382370445	25000	Unknown
    # 7007318166	40000	Unknown
    # 7411890958	7000	Unknown
    # 9990710408	65000	Unknown
    # 8006440062	150000	Unknown
    # 7000361734	200000	Unknown
    # 7406112636	30000	Unknown
    # 8805348423	300000	Unknown
    # 9989432377	35000	Unknown
    # 6203758662	60000	Unknown
    # 8273864664	7000	Unknown
    # 9887296211	50000	Unknown
    # 9552499568	55000	Unknown
    # 7284066421	50000	Unknown
    # 7719804548	150000	Unknown
    # 8445217326	40000	Unknown
    # 8698215191	50000	Unknown
    # 9204710745	45000	Unknown
    # 9702247310	30000	Unknown
    # 7048926181	25000	Unknown
    # 9483696589	75000	Unknown
    # 9429307450	40000	Unknown
    # 9823714745	5000	Unknown
    # 9096532584	20000	Unknown
    # 9672664240	100000	Unknown
    # 9522592501	40000	Unknown
    # 7018966848	25000	JEET LAL PATWA
    # 9079027201	7000	Unknown
    # 9284858118	120000	PINTOO BAHADUR GURUNG
    # 8740025091	50000	Unknown
    # 9113868582	150000	Unknown
    # 9636292113	120000	KAILASH NATH
    # 7357190392	50000	Unknown
    # 7619931419	40000	Unknown
    # 9420578474	75000	Unknown
    # 9584661845	15000	Unknown
    # 8858617403	50000	Unknown
    # 8874414020	15000	Unknown
    # 9102222794	10000	Unknown
    # 9140563137	15000	Unknown
    # 9649852346	70000	Unknown
    # 8901264985	5000	Unknown
    # 6206042078	15000	Unknown
    # 7407980016	30000	Unknown
    # 9909214665	60000	Unknown
    # 9304531083	40000	Unknown
    # 9720265104	115000	Unknown
    # 8971805807	200000	Unknown
    # 9760382906	200000	Unknown
    # 9365800915	245000	Unknown
    # 8414990957	50000	SUJAN  DATTA
    # 9713474776	185000	Unknown]
    # 9932635365	50000	Unknown
    # 9391051566	100000	Unknown
    # 7602706978	50000	Unknown
    # 9601672751	185000	Unknown
    # 9844128077	200000	Unknown
    # 8473903678	10000	Unknown
    # 7014873619	75000	SAMARIYA  VAINKAT
    # 8144935348	20000	Unknown
    # 9687013585	40000	Unknown
    # 9776094273	150000	Unknown
    # 6260415856	40000	Unknown
    # 8709697820	75000	vikash kumar
    # 7000762502	15000	PANKAJ  DESHMUKH
    # 9044426840	15000	Unknown
    # 9572445812	20000	Unknown
    # 8118851415	75000	Unknown
    # 7061902094	10000	Unknown
    # 9876053242	25000	Unknown
    # 8583022107	100000	Unknown
    # 8583022107	100000	Unknown
    # 7001330384	95000	SANAT BERA
    # 9678710773	40000	Unknown
    # 8115040853	15000	Unknown
    # 9673677468	200000	Unknown
    # 8861621291	60000	Unknown
    # 8188999669	150000	Unknown
    # 9552693232	200000	Unknown
    # 9653222925	40000	Unknown
    # 9667337116	40000	Unknown
    # 7080293636	40000	Unknown
    # 8602082798	75000	VIJAY KUMAR YADAV
    # 7483209380	15000	Unknown
    # 9179037516	20000	Unknown
    # 9273766553	100000	Unknown
    # 8340248984	25000	Unknown
    # 9966335538	100000	Unknown
    # 8518868421	60000	Unknown
    # 7352813621	50000	Unknown
    # 9559343100	50000	Unknown
    # 9968984213	75000	Unknown
    # 9919092597	50000	Unknown
    # 7498227691	20000	Unknown
    # 9050538652	40000	Unknown
    # 9101549543	65000	Unknown
    # 6200443649	35000	Unknown
    # 8383961257	95000	Unknown
    # 9999538993	110000	Unknown
    # 9111616464	10000	Unknown
    # 7022647631	5000	Unknown
    # 8200722532	200000	Unknown
    # 9836755708	7000	Unknown
    # 9284587189	40000	Unknown
    # 8368695153	50000	Unknown
    # 7549685996	50000	Unknown
    # 8695721485	7000	Unknown
    # 9011256522	40000	JAMIL ISMAIL DESAI
    # 9686364813	100000	Unknown
    # 9437796389	50000	Unknown
    # 6304300170	50000	Unknown
    # 8187002045	100000	MdMuktharmd Md muktharmd
    # 9827387600	200000	MANSUR AHAMED
    # 9794458409	120000	Ritesh Tewari
    # 9929315171	50000	RAJESH RAWAT
    # 9589557730	40000	Unknown
    # 9478741560	10000	Unknown
    # 9918771920	200000	Unknown
    # 9724056868	300000	Unknown
    # 9158292539	200000	Unknown
    # 6260477110	35000	Unknown
    # 9867557402	100000	Unknown
    # 7987476643	50000	PRADEEP KUMAR CHANDRAVANSHI
    # 8271310563	40000	Unknown
    # 9989812203	15000	Unknown
    # 8006055258	40000	Unknown
    # 7989707678	10000	MOHAMMED MUNAWAR
    # 8554825786	50000	Unknown
    # 8791429050	65000	PUSHPENDRA GUPTA
    # 9610603768	50000	Unknown
    # 8596998759	20000	Unknown
    # 8298727142	50000	Unknown
    # 7902573151	40000	Unknown
    # 9312856993	10000	Unknown
    # 6304430407	50000	Unknown
    # 8552980170	50000	Unknown
    # 9826389594	20000	Unknown
    # 8200743618	50000	Unknown
    # 9413303512	100000	Unknown
    # 9330875014	200000	Unknown
    # 9818365191	40000	NARESH KUMAR SARKANIYA
    # 9270155948	100000	Unknown
    # 8709635940	7000	Unknown]
    # 9029688708	100000	Unknown
    # 9686624019	7000	Unknown
    # 9109823495	120000	Unknown
    # 9000576846	100000	Unknown
    # 9989203361	200000	Unknown
    # 9887353613	100000	AZIJ  AHMED
    # 9667164853	60000	Unknown
    # 9831868269	100000	Unknown
    # 9998192565	50000	Unknown
    # 8308772635	15000	Unknown
    # 8960486215	150000	Unknown
    # 9680793667	150000	JAGDISH KUMAR
    # 7007762520	25000	Unknown
    # 7756889175	50000	MANOHAR RAMA PARAB
    # 9799626719	7000	Unknown
    # 9612224696	20000	Unknown
    # 9538272483	40000	GURUGADAHALLI NINGAPPA SHYAMALA
    # 7875991895	45000	Unknown
    # 9926824150	50000	SANJAY SANJAY
    # 8532058175	50000	Unknown
    # 9934683312	60000	Unknown
    # 7021259101	100000	Unknown
    # 7441119783	7000	Unknown
    # 9411485376	20000	Unknown
    # 9425565539	100000	Unknown
    # 9763676302	40000	Unknown
    # 9730202174	50000	Unknown
    # 8127005683	10000	Unknown
    # 9930112691	100000	Unknown
    # 7076200480	50000	Unknown
    # 6376580698	50000	Unknown
    # 8923119493	150000	Unknown
    # 9008679689	35000	Unknown
    # 8240524237	15000	Unknown
    # 9980432374	7000	Unknown
    # 9927113649	200000	Unknown
    # 8795965996	100000	Unknown
    # 7008993156	40000	Unknown
    # 6205591499	10000	Unknown
    # 8800867818	10000	Unknown
    # 9880298554	120000	Unknown
    # 8390614331	150000	Unknown
    # 6291185944	75000	Unknown
    # 8587878626	200000	Unknown
    # 9938814911	200000	JASHABANTA  BALIARSINGH
    # 9829415379	25000	Unknown
    # 9602368798	25000	Unknown
    # 9938702351	30000	Unknown
    # 9652159365	100000	C  RAMANJANEYULU
    # 8789623296	100000	Unknown
    # 9001460729	200000	Unknown
    # 9948120806	150000	Unknown
    # 9858888618	35000	Unknown
    # 8171877726	50000	Unknown
    # 7054867089	40000	Unknown
    # 7050417590	30000	Unknown
    # 8001676512	50000	Unknown
    # 9695724862	50000	Unknown
    # 9278002275	80000	Unknown
    # 8527083379	75000	Unknown
    # 9749195063	10000	Unknown
    # 8766720277	7000	Unknown
    # 7218518713	50000	Unknown
    # 9849280728	40000	Unknown
    # 9011686988	50000	Unknown
    # 8050824563	20000	Arbia Begum
    # 7274889151	50000	Unknown
    # 9508140492	50000	Unknown
    # 7340225010	60000	Unknown
    # 9752775553	60000	Unknown
    # 7436063082	40000	Unknown
    # 8671853485	15000	Panchal Jigar
    # 9827020133	15000	Unknown
    # 7278446028	50000	Unknown
    # 6307958345	15000	Unknown
    # 6394984219	50000	Unknown
    # 9461850502	120000	DEVDUTT SINGH RAJAWAT
    # 9172676862	15000	Unknown
    # 9769260528	100000	Unknown
    # 7057428269	75000	Unknown
    # 7300687599	60000	Unknown
    # 8630266372	45000	Unknown
    # 8766854678	95000	Unknown
    # 9711170271	295000	Unknown
    # 9635459991	75000	SUJIT KUMAR BURNWAL
    # 9989516426	30000	Unknown
    # 8058610512	100000	REKHA KUMARI
    # 9021992558	55000	Unknown
    # 8978307457	50000	Unknown
    # 7545902287	15000	Unknown
    # 7019011073	40000	Unknown
    # 8108490103	35000	Unknown
    # 7033667276	45000	Unknown
    # 9994584722	75000	Unknown
    # 9680947794	200000	USMAN GANI MEWAFAROSH
    # 9439671240	50000	Unknown
    # 8423053384	200000	AKASH JAISWAL
    # 8008932299	100000	DONKANA BHASKARARAO
    # 8684889148	20000	Unknown
    # 9123990345	100000	Unknown
    # 9148839730	30000	Unknown
    # 7838199026	40000	Unknown
    # 8918705922	10000	Unknown
    # 7718011102	50000	Unknown
    # 8619761915	30000	Unknown
    # 8918509829	70000	Unknown
    # 9452293158	50000	Unknown
    # 9768731129	115000	Unknown
    # 9573118510	30000	Unknown
    # 9494927030	300000	Unknown
    # 9633860387	100000	Unknown
    # 9905089297	300000	Unknown
    # 9130637996	30000	Unknown
    # 9269078804	100000	Unknown
    # 9040028663	75000	Unknown
    # 9842800909	20000	Unknown
    # 9928868325	15000	Unknown
    # 9970525105	15000	Unknown
    # 7798259505	50000	Unknown
    # 7970777446	20000	Unknown
    # 9373994935	75000	Unknown
    # 8779706221	75000	Unknown
    # 9307879179	15000	Unknown
    # 9869944368	100000	Unknown
    # 8107458799	30000	Unknown
    # 8766231905	195000	Unknown
    # 9811262061	60000	Unknown
    # 9097242997	50000	Unknown
    # 7990293349	100000	Unknown
    # 9309161530	30000	Unknown
    # 8123055994	20000	KAYIM BASHA
    # 9049078259	75000	Unknown
    # 9555423340	150000	Unknown
    # 7060179740	7000	Unknown
    # 9948449616	200000	Unknown
    # 7759011489	7000	Unknown
    # 9338661148	15000	Bijay Kumar  Behera
    # 9864184221	50000	PULAK DAS
    # 8369314849	90000	Unknown
    # 8144707918	25000	Unknown
    # 7023628558	120000	Unknown
    # 9823462607	20000	Unknown
    # 9734911574	50000	Unknown
    # 9145774276	100000	Unknown
    # 6205724515	85000	Unknown
    # 9506503502	40000	Unknown
    # 9065896944	25000	Unknown
    # 7737867105	50000	Unknown
    # 8757033329	10000	Unknown
    # 6202622147	50000	Unknown
    # 9702261107	10000	Unknown
    # 9771549659	75000	Unknown
    # 8761087050	200000	Unknown
    # 8851211788	20000	Unknown
    # 9263657639	25000	Unknown
    # 9527486418	50000	Unknown
    # 9867704213	60000	Unknown
    # 7547053432	50000	BALRAM KUMAR  BALRAM KUMAR
    # 9984500250	200000	SONAM PAL
    # 7974087420	120000	Unknown
    # 9071851342	40000	ASMA TAJ
    # 8130040156	30000	Unknown
    # 8796901191	120000	MORE YOGESH SHANKAR
    # 8287198047	7000	Unknown
    # 9396306090	40000	Unknown
    # 8967956578	50000	Unknown
    # 9950448565	60000	Unknown
    # 8171814921	20000	Unknown
    # 8545003329	15000	Unknown
    # 9300377722	110000	Unknown
    # 9535184671	20000	Unknown
    # 8096363520	100000	Unknown
    # 7697167858	50000	Unknown
    # 9140082209	150000	Unknown
    # 9812697939	140000	Unknown
    # 9689062181	200000	Unknown
    # 9561714904	100000	Unknown
    # 9929258610	40000	Heera Lal Purbia
    # 9892841681	15000	Unknown
    # 9680331936	90000	Unknown
    # 8971227474	45000	Unknown
    # 9664175761	35000	Unknown
    # 9879652732	15000	Unknown
    # 7091349720	20000	Unknown
    # 9648268935	25000	Unknown
    # 7294827377	15000	BOBY DEVI
    # 9887257957	10000	Unknown
    # 9680658376	100000	Unknown
    # 9490802592	110000	Unknown
    # 9001047244	60000	Unknown
    # 9660950811	50000	Unknown
    # 9717026359	40000	Unknown
    # 9989168377	10000	Unknown
    # 7725820420	150000	Unknown
    # 9583893733	30000	Unknown
    # 7745816351	40000	Unknown
    # 9636078804	50000	Unknown
    # 8452082332	75000	Unknown
    # 8826628681	15000	Unknown
    # 8707746346	50000	Unknown
    # 8512936918	15000	Unknown
    # 9769794657	60000	Unknown
    # 9493425808	300000	Unknown
    # 7983671323	25000	Unknown
    # 9955291291	75000	Unknown
    # 9544152431	150000	Unknown
    # 8959860418	50000	Unknown
    # 8959986799	50000	Unknown
    # 9861138442	50000	Unknown
    # 8979941094	150000	Unknown
    # 6352839107	30000	Unknown
    # 9991616743	140000	Unknown
    # 9899909370	50000	Unknown
    # 8800606499	75000	Unknown
    # 8797902667	50000	BAMA  UPADHYAY
    # 9631937999	45000	AJAY KUMAR MAHTO
    # 7851088479	40000	Unknown
    # 8178263771	30000	Unknown
    # 9319070011	40000	Unknown
    # 6370246603	100000	Unknown
    # 9131410165	7000	Unknown
    # 9632472885	60000	Unknown
    # 8789990497	50000	HAFEEZ AHMAD
    # 8104475256	25000	NARAYAN LAL CHOUDHARY
    # 9140606208	15000	Unknown
    # 8103413145	15000	Unknown
    # 7739719686	45000	Unknown
    # 9980699823	15000	Unknown
    # 8123355793	7000	Unknown
    # 9689225550	60000	Unknown
    # 9315559204	40000	rajkumar harvansh
    # 9424669806	40000	Unknown
    # 9074956868	50000	Unknown
    # 9219354144	35000	Unknown
    # 9415336139	100000	Unknown
    # 9665047700	50000	Unknown
    # 9876203812	40000	Unknown
    # 6291185944	60000	Unknown
    # 9901154450	30000	Unkown
    # 9702201028	100000	Unknown
    # 7055098911	50000	Unknown
    # 9749097689	30000	Unknown
    # 7548915411	20000	Unknown
    # 9452065761	30000	Unknown
    # 7024023331	20000	Unknown
    # 9205543665	50000	Unknown
    # 9374059479	45000	Unknown
    # 8450939322	100000	Unknown
    # 7017696148	50000	Unknown
    # 9106458599	15000	MANOJ KUMAR PRASAD
    # 9665539975	300000	Unknown
    # 9724318728	100000	Unknown
    # 9779165172	40000	Unknown
    # 9674134001	10000	Unknown
    # 9351412397	50000	Unknown
    # 7301158145	40000	Unknown
    # 7460853838	75000	Unknown
    # 6394258549	50000	Unknown
    # 7991131215	100000	Unknown
    # 8826674195	5000	Unknown
    # 9973702747	35000	Unknown
    # 7340602088	80000	Unknown
    # 8652010558	10000	Unknown
    # 9847711124	120000	Unknown
    # 9040555463	100000	Unknown
    # 8238416501	50000	Unknown
    # 9505452549	50000	Unknown
    # 9822523173	50000	Unknown
    # 9833042629	20000	Unknown
    # 9588938640	40000	Unknown
    # 8826869830	25000	Unknown
    # 9805068847	85000	Unknown
    # 9933660451	40000	Unknown
    # 7989833037	30000	Unknown
    # 8447433754	40000	Unknown
    # 7568735975	95000	Unknown
    # 9926824150	75000	SANJAY SANJAY
    # 9818127349	75000	renu dhingra
    # 8169187734	75000	Unknown
    # 9453219533	200000	Unknown
    # 6395238790	7000	Unknown
    # 8923152388	40000	Unknown
    # 9678147892	40000	Unknown
    # 9534690894	95000	Unknown
    # 8424956758	20000	Unknown
    # 9783895535	30000	Unknown
    # 8249671365	40000	Unknown
    # 7684002051	50000	Unknown
    # 9623414995	40000	Unknown
    # 7384638151	200000	Unknown
    # 8123214869	50000	Unknown
    # 7002326058	55000	Unknown
    # 8825143273	50000	Unknown
    # 8980067477	25000	Unknown
    # 9693592068	25000	Unknown
    # 6394527724	75000	Unknown
    # 9731426696	40000	Unknown
    # 8780234527	50000	Unknown
    # 7350501249	50000	Unknown
    # 6205546243	200000	Unknown
    # 9161655333	50000	Unknown
    # 6290638973	7000	Unknown
    # 7027668023	20000	Unknown
    # 9944720707	200000	Unknown
    # 9627278707	300000	Unknown
    # 8120806211	20000	Unknown
    # 9782547447	75000	Unknown
    # 9920415895	40000	Unknown
    # 8730077109	100000	Unknown
    # 9460158572	50000	Unknown
    # 9928663333	30000	Unknown
    # 9014106829	10000	Unknown
    # 9368562980	20000	Unknown
    # 9890467489	130000	Unknown
    # 9767729447	75000	Unknown
    # 9619151173	60000	Unknown
    # 8369696179	50000	Unknown
    # 9785118371	40000	Unknown
    # 7990309615	10000	Unknown
    # 9463157211	40000	HARJIT SINGH
    # 9346235155	40000	Unknown
    # 9932913132	25000	Unknown
    # 8455027908	75000	Unknown
    # 9949047686	50000	Unknown
    # 8090389932	25000	Unknown
    # 8080525303	40000	Unknown
    # 8461898241	15000	Unknown
    # 9552713945	30000	Unknown
    # 8920264033	30000	Unknown
    # 6282639901	40000	Unknown
    # 9970667967	7000	Unknown
    # 9922115382	45000	Unknown
    # 9928734799	5000	Unknown
    # 9783203081	5000	Unknown
    # 9116843866	40000	Unknown
    # 7533886339	30000	Unknown
    # 7208680868	25000	Unknown
    # 7983175883	200000	Unknown
    # 9686728924	100000	Unknown
    # 9890317707	50000	Unknown
    # 9030796050	300000	Unknown
    # 9026517333	15000	Unknown
    # 9503354281	50000	Unknown
    # 9910929552	200000	Unknown
    # 6239347085	35000	Unknown
    # 8320983942	10000	Unknown
    # 9957449620	40000	Unknown
    # 9967241166	15000	Unknown
    # 7052104205	15000	Unknown
    # 9829062768	40000	Unknown
    # 7008339487	50000	Unknown
    # 9668054831	20000	Unknown
    # 9975318440	100000	Unknown
    # 6299449207	85000	Unknown
    # 9722929454	100000	Unknown
    # 9494588295	50000	Unknown
    # 8308520999	75000	Unknown
    # 9730202174	50000	Unknown
    # 9719540034	40000	Unknown
    # 9000090799	50000	Unknown
    # 9561206182	200000	Unknown
    # 9978983029	50000	Unknown
    # 9518187162	30000	Unknown
    # 8081993556	50000	Unknown
    # 8073794583	7000	Unknown
    # 9098175420	7000	Unknown
    # 9310422853	7000	Unknown
    # 9971220759	65000	Unknown
    # 7549233434	50000	KAILASH KUMAR
    # 7028224588	60000	Unknown
    # 7504402408	200000	Unknown
    # 9100523922	60000	MOHD  HUSSAIN
    # 9917226457	200000	Unknown
    # 9631454777	140000	Unknown
    # 7042783110	50000	Unknown
    # 7519137802	7000	Unknown
    # 6360692089	75000	Unknown
    # 9380002526	15000	Unknown
    # 7879377073	40000	Unknown
    # 8698575038	50000	Unknown
    # 9891922568	20000	Unknown
    # 9113312670	25000	Unknown
    # 9214720097	7000	Unknown
    # 8318870510	7000	Unknown
    # 7705048851	50000	Unknown
    # 9765025010	50000	IRFAN NIJAMUDDIN SHAIKH
    # 9892947527	30000	Unknown
    # 7907438951	40000	Unknown
    # 8009875993	55000	Unknown
    # 7014278277	100000	Unknown
    # 9833320290	50000	Unknown
    # 8989125238	50000	Unknown
    # 8583056399	150000	GEETANJALI KUMARI
    # 9770593131	105000	Unknown
    # 8921374815	200000	Unknown
    # 9901997553	90000	Unknown
    # 8129191390	25000	Unknown
    # 8700601529	300000	YUVRAJ YUVRAJ
    # 9689225550	75000	Unknown
    # 9994381109	75000	Unknown
    # 9928108607	10000	Unknown
    # 7030304446	50000	Unknown
    # 9521289127	50000	Unknown
    # 7228874189	75000	Unknown
    # 9932958749	50000	Unknown
    # 6201003169	10000	Unknown
    # 9059595454	150000	Unknown
    # 9307773568	30000	Unknown
    # 7495064480	20000	Unknown
    # 8738031638	50000	Unknown
    # 9303294584	20000	Unknown
    # 7738867276	7000	Unknown
    # 9771916551	50000	Unknown
    # 9865197747	30000	Unknown
    # 9007187092	50000	Unknown
    # 8356969326	55000	Unknown
    # 9898213483	75000	Unknown
    # 9177132073	15000	Unknown
    # 9887408040	40000	Unknown
    # 9981322360	150000	Unknown
    # 8087998135	40000	Unknown
    # 9610021392	25000	Unknown
    # 6202378386	15000	Unknown
    # 9915832138	50000	JASPREET KAUR
    # 9611005533	50000	Unknown
    # 7506300550	40000	Unknown
    # 8252502300	60000	Unknown
    # 9775338722	50000	Unknown
    # 8331974819	65000	Unknown
    # 7631138592	100000	Unknown
    # 9021371794	100000	Unknown
    # 8824967474	15000	Unknown
    # 9811045834	40000	Unknown
    # 9340171013	150000	Unknown
    # 6393232507	30000	Unknown
    # 7097487299	25000	Unknown
    # 9493229750	45000	Unknown
    # 9891219272	240000	Unknown
    # 9991241868	150000	Unknown
    # 9012912805	150000	VIJAY KUMAR
    # 9400097338	25000	Unknown
    # 8440902855	20000	Unknown
    # 7983093787	50000	Unknown
    # 9503210761	35000	Unknown
    # 7380937920	50000	Unknown
    # 7488201678	40000	Unknown
    # 7388389221	200000	Unknown
    # 8182059000	15000	Unknown
    # 7620171908	10000	Unknown
    # 9455001628	80000	Unknown
    # 9098700001	35000	Unknown
    # 9532926836	7000	Unknown
    # 9868628574	50000	JAGDISH  CHANDRA
    # 6202039214	50000	Unknown
    # 9563534016	240000	Unknown
    # 8608762621	100000	Unknown
    # 8899105595	100000	Unknown
    # 7830492102	100000	LOGARAJ BABU
    # 8356904901	5000	Unknown
    # 7340336973	15000	Unknown
    # 8857995491	150000	Unknown
    # 9463141928	40000	MUKESH SINGH
    # 8839316954	65000	Unknown
    # 8930895790	40000	SURESH KUMAR
    # 9324170987	170000	Unknown
    # 9958911675	45000	Unknown
    # 7095924748	30000	Unknown
    # 9265306437	50000	BHAVESHKUMAR PANCHAL
    # 9465605987	75000	DHIRANJAN DAS
    # 9468958181	50000	Unknown
    # 6301287597	50000	Unknown
    # 9725312803	20000	Unknown
    # 7518182212	7000	Unknown
    # 9702097161	40000	Unknown
    # 7408542889	20000	Unknown
    # 9921238557	55000	DNYANOBA RAGHU PADHER
    # 8763145457	50000	Unknown
    # 8969210078	75000	Unknown
    # 8169101114	200000	Unknown
    # 8003513629	150000	Unknown
    # 8698491930	50000	Unknown
    # 8126274945	150000	Unknown
    # 8685922710	10000	Unknown
    # 7829760718	200000	Unknown
    # 8715033768	15000	Unknown
    # 8123999772	200000	KUNIGAL MANOHAR RAJ SANTHOSH
    # 7057590708	60000	Unknown
    # 9604831392	20000	Unknown
    # 9767912058	50000	Unknown
    # 9693542848	25000	Unknown
    # 8317531405	115000	Unknown
    # 9721113295	40000	Unknown
    # 9984289912	75000	Unknown
    # 9708574198	40000	Unknown
    # 7995951014	10000	Unknown
    # 8858614347	100000	Unknown
    # 9950847460	75000	Unknown
    # 9745796933	100000	Unknown
    # 9742797866	50000	Unknown
    # 8600679066	100000	Unknown
    # 8719007031	7000	Unknown
    # 9748521497	35000	Unknown
    # 8269975015	15000	ANGAD PRASAD SHAH
    # 9831794089	30000	PRASENJIT PAL
    # 9763666808	40000	Unknown
    # 8432232812	75000	SUMER SINGH
    # 8279742783	125000	GAURAV KUMAR
    # 8130308763	85000	Unknown
    # 8637218297	50000	Unknown
    # 9573685269	100000	Unknown
    # 7038000769	25000	Unknown
    # 8273819291	200000	Unknown
    # 9178568659	7000	Unknown
    # 9889187093	15000	Unknown
    # 8341410214	150000	SRIDEVI  KATTA
    # 9004798003	35000	Unknown
    # 8955396934	30000	Unknown
    # 8600873229	40000	Unknown
    # 6393905997	100000	Unknown
    # 9172676862	10000	Unknown
    # 9985952373	7000	Unknown
    # 9792349479	45000	Unknown
    # 7078558366	20000	Mohammad  Anas 
    # 8792688263	30000	Unknown
    # 8849174586	20000	Unknown
    # 9830428757	60000	Unknown
    # 9785530546	50000	Unknown
    # 7987774414	40000	Unknown
    # 9507593139	50000	Unknown
    # 9340686237	7000	Unknown
    # 9708462406	40000	Unknown
    # 7584046535	40000	Unknown
    # 9142673628	50000	MOHAMMAD SHAMSHAD KURAISI
    # 9712883237	10000	Unknown
    # 9766528665	40000	Unknown
    # 7830529909	5000	Unknown
    # 9752453211	15000	Unknown
    # 8850660886	120000	Unknown
    # 8287284758	100000	Unknown
    # 9589396248	100000	SUDHIR YADAV
    # 7022458047	7000	Unknown
    # 8768946780	20000	Unknown
    # 9830545246	10000	Unknown
    # 9386012174	7000	MOHAMMAD ASGAR
    # 9085854960	50000	Unknown
    # 7668613367	50000	Unknown
    # 6201575694	300000	Unknown
    # 8887796572	100000	Unknown
    # 7377236847	50000	Unknown
    # 7000213198	100000	Unknown
    # 9461541152	75000	Unknown
    # 8750396524	40000	Unknown
    # 8583012247	25000	Unknown
    # 8958869749	10000	Unknown
    # 9334166799	75000	Unknown
    # 7742923954	70000	Unknown
    # 8125694804	50000	SYED FAZAL ALI
    # 7991339375	40000	Unknown
    # 9776094273	150000	Unknown
    # 8105034304	200000	Unknown
    # 8167875494	40000	Unknown
    # 9888371085	75000	Unknown
    # 9440016519	40000	Unknown
    # 9777870352	35000	Unknown
    # 9899596264	300000	Unknown
    # 8610034197	20000	Unknown
    # 9842520760	300000	Unknown
    # 9350020020	7000	Unknown
    # 8305448452	30000	Unknown
    # 7999338591	75000	KANCHAN JAGDISHKUMAR KUBARE
    # 8018529945	50000	Unknown
    # 7891850702	50000	Unknown
    # 8293259480	35000	LAKSHMAN PAL
    # 9834536868	15000	Unknown
    # 7645017407	40000	Unknown
    # 8779894482	40000	Unknown
    # 9950889813	50000	Unknown
    # 6366344646	50000	Unknown
    # 9960913315	30000	Unknown
    # 9905893283	5000	MR RAHUL PRASAD VERMA  RAHUL PRASAD VERMA
    # 8738031638	15000	Unknown
    # 6398141139	7000	Unknown
    # 9671640435	40000	Unknown
    # 9420186796	75000	Unknown
    # 9823610836	45000	Unknown
    # 8240084680	15000	Unknown
    # 7004971378	50000	Unknown
    # 9895953000	50000	Unknown
    # 8197832248	30000	Unknown
    # 8308354715	45000	ZAREKAR SAIRAJ MAHESH  SAIRAJ MAHESH ZAREKAR
    # 8877333268	50000	GOPAL PRASAD SAH
    # 9632804440	140000	Unknown
    # 9033856907	100000	Unknown
    # 9935919181	20000	Unknown
    # 9464415488	35000	Unknown
    # 7061296034	10000	Unknown
    # 7887905404	25000	Unknown
    # 9657418585	50000	Unknown
    # 8140470618	30000	Unknown
    # 9767195321	40000	Unknown
    # 9665659029	100000	Unknown
    # 7526846501	200000	Unknown
    # 9834687317	50000	Unknown
    # 9064144942	40000	Unknown
    # 9538314238	7000	Unknown
    # 9855759633	7000	Unknown
    # 9131464102	20000	Unknown
    # 6000419353	60000	Unknown
    # 9938515359	20000	Unknown
    # 8292910130	30000	Unknown
    # 7000078807	10000	Unknown
    # 9700100243	150000	Unknown
    # 8552980170	7000	Unknown
    # 7505973290	40000	Unknown
    # 9425061649	60000	Unknown
    # 9975795727	150000	Unknown
    # 9975795727	150000	Unknown
    # 7411081321	30000	Unknown
    # 8759054254	50000	Unknown
    # 9369316531	10000	Unknown
    # 8955377182	35000	Unknown
    # 9996589948	200000	Unknown
    # 9602950654	50000	Unknown
    # 7891026765	35000	Unknown
    # 9978983029	50000	Unknown
    # 6396474072	30000	Unknown
    # 8528521727	40000	Unknown
    # 9769877590	10000	Unknown
    # 7257899737	40000	Unknown
    # 9929364651	40000	YASHPAL SINGH RAJPUT
    # 8650000406	150000	Unknown
    # 9729113742	50000	Unknown
    # 7002048267	100000	Unknown
    # 9945981715	40000	Unknown
    # 9838286601	20000	Unknown
    # 9953159915	100000	Unknown
    # 8290125833	120000	Unknown
    # 9919995790	50000	Unknown
    # 7892653705	200000	Unknown
    # 9725476063	75000	Unknown
    # 9445411869	105000	Unknown
    # 9740462968	150000	Unknown
    # 9977702444	40000	Manish Rajput
    # 9047570138	50000	Unknown
    # 9000634333	100000	VENKAT  MERIGE
    # 8639747817	40000	Unknown
    # 7000023204	50000	Unknown
    # 6200848661	150000	Unknown
    # 8436251660	75000	Unknown
    # 8299135345	100000	Unknown
    # 9949972261	70000	Unknown
    # 7073431138	40000	Unknown
    # 9731215925	7000	Unknown
    # 9439098487	40000	MANORANJAN MEKAP
    # 9051223765	50000	Unknown
    # 9224212944	100000	Unknown
    # 9887599591	35000	Unknown
    # 8730807040	60000	Unknown
    # 7738981452	120000	Unknown
    # 6307235124	30000	Unknown
    # 8094753310	150000	Unknown
    # 9742428346	7000	Unknown
    # 9998881734	105000	Unknown
    # 8959860418	7000	Unknown
    # 8892668677	50000	IMRAN  AHMED
    # 7201820439	50000	Unknown
    # 9631248082	50000	Unknown
    # 7097487299	20000	Unknown
    # 9913045425	50000	Unknown
    # 7678111912	40000	Unknown
    # 9628680625	25000	Unknown
    # 9936378114	7000	Unknown
    # 9096483579	10000	Unknown
    # 9741870898	60000	Unknown
    # 8369678368	75000	Unknown
    # 7991125809	60000	Unknown
    # 9601501851	15000	Unknown
    # 6355890785	50000	Unknown
    # 9837863501	50000	Unknown
    # 9777391318	100000	Unknown
    # 9340374933	30000	Unknown
    # 9533312189	150000	Unknown
    # 8687939400	50000	Unknown
    # 6306351037	40000	Unknown
    # 6202821706	60000	Unknown
    # 7870321553	30000	VIKASH  KUMAR
    # 8114419280	75000	PREETAM  DEWASI
    # 9849445728	50000	SAJID KHAN
    # 9896330912	30000	Unknown
    # 9417518927	30000	RAJESH RAI
    # 8618373299	10000	Unknown
    # 8690965839	15000	Unknown
    # 7262015165	7000	Unknown
    # 7338222720	15000	Unknown
    # 7509028264	200000	Unknown
    # 9784585761	60000	Unknown
    # 7249830718	40000	Unknown
    # 6261017485	50000	Unknown
    # 8722029635	50000	Unknown
    # 7875234500	50000	Unknown
    # 6380327805	35000	Unknown
    # 8982601808	7000	Unknown
    # 8412051246	20000	Unknown
    # 6294542315	60000	Unknown
    # 9563441511	30000	Unknown
    # 8148723054	300000	Unknown
    # 9887121915	60000	Unknown
    # 7765883517	30000	Unknown
    # 7355223103	150000	Unknown
    # 9389073738	15000	Unknown
    # 9548990265	40000	Unknown
    # 9887353613	200000	AZIJ  AHMED
    # 9439164985	50000	Unknown
    # 9290261195	120000	Unknown
    # 9820160276	150000	Unknown
    # 8851007860	30000	Unknown
    # 8919511310	100000	Unknown
    # 7894182298	100000	Unknown
    # 9920143639	150000	CHETAN ISHWARLAL CHAUHAN
    # 8006089825	50000	Unknown
    # 9975077630	150000	Unknown
    # 8617757503	10000	Unknown
    # 7499315155	90000	Syed Asif
    # 8639256016	50000	Unknown
    # 8805223186	75000	Unknown
    # 8007587836	75000	Unknown
    # 9014131395	50000	Unknown
    # 6295252569	100000	Unknown
    # 9121566824	150000	Unknown
    # 9321661975	15000	Unknown
    # 9875202098	150000	Unknown
    # 8885378777	20000	Unknown
    # 9324279202	40000	Unknown
    # 8270493782	25000	Unknown
    # 9593681112	40000	Unknown
    # 9960886034	100000	SHILPA  AHIRE
    # 9561885404	30000	Unknown
    # 7869331457	100000	Unknown
    # 9767375464	35000	Unknown
    # 9582787305	150000	Unknown
    # 9456964394	40000	Unknown
    # 9011580460	100000	Unknown
    # 8140515212	20000	Unknown
    # 8888698050	25000	Unknown
    # 7498166686	75000	Unknown
    # 9511569156	50000	Unknown
    # 9945639592	55000	Unknown
    # 9771387819	100000	Unknown
    # 9024818426	35000	Unknown
    # 9660362549	40000	Unknown
    # 6291741689	30000	Unknown
    # 9691806681	15000	Unknown
    # 8561072375	50000	Unknown
    # 8809282154	50000	Unknown
    # 9661992839	200000	Manoj Prasad
    # 7568283448	7000	Unknown
    # 8555892634	120000	SHANMUGAM SHANTI KUMAR
    # 9990231610	100000	RAJ KUMAR SHARMA
    # 8527379438	50000	Unknown
    # 9799771712	15000	Unknown
    # 8506986507	75000	Unknown
    # 9529213209	50000	Unknown
    # 7411753977	75000	Unknown
    # 9113909380	7000	Unknown
    # 9041229330	40000	Unknown
    # 8329721637	50000	Unknown
    # 7875250237	75000	Unknown
    # 7896290174	50000	Unknown
    # 6301148188	50000	Unknown
    # 7600179600	100000	ZALA JAYDIPSINH
    # 8168176545	40000	Unknown
    # 8010860511	50000	Unknown
    # 7056568477	65000	Unknown
    # 9693542848	10000	Unknown
    # 8121668199	120000	Unknown
    # 9837251383	200000	Unknown
    # 9270081458	60000	Unknown
    # 9679106290	40000	Unknown
    # 9431273020	15000	Unknown
    # 9738085101	150000	SURAJ PASHA
    # 8333960666	100000	Unknown
    # 9873216717	15000	Unknown
    # 9867555088	150000	Unknown
    # 9898140383	50000	Unknown
    # 7666153917	20000	Unknown
    # 9818698815	70000	Unknown
    # 8153036072	30000	Unknown
    # 8638233326	50000	Unknown
    # 7021437714	150000	Unknown
    # 9924627223	50000	Unknown
    # 7499265478	50000	Unknown
    # 7035850102	20000	Unknown
    # 9008394841	35000	Unknown
    # 8815250081	35000	Unknown
    # 7894295309	10000	Unknown
    # 9844663843	40000	Unknown
    # 9461993889	7000	Unknown
    # 9006700570	300000	Unknown
    # 8960808080	50000	Unknown
    # 7501071570	100000	Unknown
    # 7733991575	100000	Unknown
    # 8517816714	40000	Unknown
    # 9514727580	75000	Unknown
    # 9568149492	60000	Unknown
    # 8860996153	100000	Unknown
    # 9956209852	40000	Unknown
    # 7068944407	50000	Unknown
    # 9741153354	75000	Unknown
    # 9887207595	100000	BALABH  SHARMA JEEVAN
    # 8207563626	15000	RAHUL KUMAR
    # 7250644092	100000	Unknown
    # 7898499679	100000	Unknown
    # 8125280502	40000	Unknown
    # 9913406805	50000	Unknown
    # 7546975610	45000	Unknown
    # 9354711182	30000	YASHVARDHAN YASHVARDHAN
    # 8766843217	60000	Unknown
    # 9921298247	25000	KRUSHNAA SANJAY JADHAV
    # 9994573215	50000	Unknown
    # 9987408872	30000	Unknown
    # 9916135320	25000	ANAND MIRAJKAR
    # 7250529401	50000	Unknown
    # 8618100577	75000	Unknown
    # 8873577104	40000	Unknown
    # 8007594106	110000	Unknown
    # 9341745333	50000	Unknown
    # 7367888188	65000	Unknown
    # 6262717728	40000	Unknown
    # 8929255699	75000	Unknown
    # 9593771885	40000	Unknown
    # 8239874994	7000	Unknown
    # 8169847935	15000	Unknown
    # 8411938079	300000	Unknown
    # 7010256587	30000	Unknown
    # 8121919007	15000	Unknown
    # 8105786587	40000	Unknown
    # 9860349795	60000	Unknown
    # 6374849282	10000	Unknown
    # 6005819617	100000	Unknown
    # 7980632090	45000	Unknown
    # 8709924467	40000	Unknown
    # 9911633113	50000	Unknown
    # 8218268636	70000	Unknown
    # 8190995269	50000	PATHMANABAN RAJASEKARAN
    # 9795663280	100000	Unknown
    # 7005269015	50000	Unknown
    # 9213399119	300000	Unknown
    # 7990767379	150000	Unknown
    # 6362124569	110000	Unknown
    # 8007007807	50000	Unknown
    # 8441092208	35000	Unknown
    # 9782123233	75000	Unknown
    # 9141740011	200000	Unknown
    # 7665270395	100000	Unknown
    # 9438655249	150000	Unknown
    # 9663044519	150000	MOHAN MOHAN
    # 8384815331	60000	Unknown
    # 9908912177	60000	NAKKA SURYA PRAKASH RAO
    # 9415831685	10000	Unknown
    # 8109170916	50000	Unknown
    # 9518184097	25000	Unknown
    # 9145771650	7000	Unknown
    # 9415238898	10000	Unknown
    # 8658453843	7000	SHEK YASIN
    # 9724262897	30000	SOHILBHAI PARMAR
    # 7845183006	45000	Unknown
    # 9594610256	7000	Unknown
    # 9506757892	95000	Unknown
    # 8899494966	50000	Unknown
    # 6207751214	50000	Unknown
    # 9800866071	7000	Unknown
    # 7324930791	35000	Unknown
    # 9271775228	50000	Unknown
    # 7739346729	60000	Unknown
    # 8369498519	150000	Unknown
    # 7569116906	50000	Unknown
    # 9730828674	40000	Unknown
    # 8445922764	75000	Unknown
    # 9366160299	50000	Unknown
    # 9325670498	75000	Unknown
    # 9024386392	30000	Unknown
    # 7780152769	75000	Unknown
    # 9515595280	40000	Unknown
    # 7665456324	20000	Unknown
    # 8867823272	45000	Unknown
    # 9702071032	10000	Unknown
    # 7030282131	35000	Unknown
    # 9461024575	60000	Unknown
    # 6204984620	15000	Unknown
    # 9980370758	50000	Unknown
    # 8308689530	100000	Unknown
    # 9130202540	45000	Unknown
    # 9213996390	35000	Unknown
    # 9059790313	200000	Unknown
    # 8056279492	60000	Unknown
    # 8755390101	50000	VIKAS VIKAS
    # 9686556450	200000	Unknown
    # 9381802207	100000	Unknown
    # 8295414912	60000	Unknown
    # 9050205352	30000	Unknown
    # 9736486473	50000	Unknown
    # 7982729116	150000	Unknown
    # 9164442520	15000	Unknown
    # 9001602800	60000	SONU SUTHAR
    # 9574374644	30000	Unknown
    # 9711141675	120000	KANDETI NARESH
    # 8934861598	7000	Unknown
    # 7588610612	75000	Unknown
    # 8755192363	75000	Unknown
    # 6394581079	60000	Unknown
    # 9078517475	75000	Unknown
    # 8523809027	50000	Unknown
    # 8197337476	40000	Unknown
    # 9664184053	40000	Unknown
    # 6205383493	15000	Unknown
    # 8226882172	100000	Unknown
    # 9061164285	40000	Unknown
    # 8587020978	15000	Unknown
    # 8080175995	40000	Unknown
    # 8169494633	125000	Unknown
    # 9785149570	50000	Unknown
    # 8617204386	50000	Unknown
    # 9568616124	20000	Unknown
    # 9304463232	40000	Unknown
    # 9326509157	75000	Unknown
    # 9588404162	150000	VAISHALI RAJENDRA CHAVAN
    # 7065790412	150000	Unknown
    # 7903053091	100000	SOMESH KUMAR  SOMESH KUMAR
    # 9532005388	25000	Unknown
    # 7399934910	100000	Unknown
    # 7000849283	200000	Unknown
    # 9998450567	150000	CHHIPA MOHSHINBHAI
    # 9463157211	75000	HARJIT SINGH
    # 9435543827	200000	Unknown
    # 9901170186	45000	Unknown
    # 7019849179	40000	Unknown
    # 9994626871	145000	Unknown
    # 9995227729	110000	Unknown
    # 9174394936	35000	Unknown
    # 7255812212	50000	Unknown
    # 9902420703	200000	Unknown
    # 8595718746	50000	Unknown
    # 9544656568	200000	Unknown
    # 8975129131	120000	Unknown
    # 8858812377	20000	Unknown
    # 6372406855	50000	Unknown
    # 7355189828	100000	Unknown
    # 7257910122	55000	Unknown
    # 9690077354	7000	Unknown
    # 8434582536	15000	Unknown
    # 6305955638	15000	Unknown
    # 7000157896	65000	Unknown
    # 8750697192	50000	Unknown
    # 9852360760	50000	Unknown
    # 8169762983	150000	Unknown
    # 6396137399	75000	Unknown
    # 9975468192	50000	Unknown
    # 8809348553	150000	Unknown
    # 9797662357	60000	Unknown
    # 8416862521	100000	Unknown
    # 8076229671	60000	Unknown
    # 9306185721	110000	Unknown
    # 9406879177	15000	VIKAS HEMRAJ KHANDELWAL
    # 9696654553	15000	Unknown
    # 9714727142	15000	Unknown
    # 8981966833	40000	Unknown
    # 9045116090	60000	Unknown
    # 6232060032	40000	Unknown
    # 7295919272	50000	Unknown
    # 9864154406	60000	Unknown
    # 9460473770	135000	Unknown
    # 9204710745	45000	Unknown
    # 9928314966	10000	Unknown
    # 9650804419	50000	Unknown
    # 7008971884	75000	Unknown
    # 7004022749	300000	Unknown
    # 9880493863	45000	Unknown
    # 7795045769	50000	Unknown
    # 7003920223	100000	Unknown
    # 7669966231	45000	Unknown
    # 9828505224	150000	Unknown
    # 9370617064	15000	Unknown
    # 8885757378	50000	SURESH KUMAR ILLINGI
    # 7357295530	40000	Unknown
    # 9542744719	7000	DYAGALA  KRISHNAMRAJU
    # 8858614347	150000	Unknown
    # 7006553797	150000	Unknown
    # 8928945193	135000	Unknown
    # 9743804931	70000	Unknown
    # 9945046389	35000	Unknown
    # 9946272357	75000	Unknown
    # 7654052576	165000	Unknown
    # 8898791096	75000	Unknown
    # 8448218608	30000	Unknown
    # 9177210260	60000	Unknown
    # 9643342827	25000	Unknown
    # 8766843217	55000	Unknown
    # 9027269586	20000	Unknown
    # 9002497226	40000	SANATAN DAS
    # 8276810848	15000	Unknown
    # 9428171515	45000	Unknown
    # 9772951026	50000	Unknown
    # 9986689993	285000	Unknown
    # 9665692064	40000	Unknown
    # 7079030003	45000	Unknown
    # 7310540055	75000	Unknown
    # 9572121065	35000	Unknown
    # 8283825712	30000	Unknown
    # 9741614927	200000	Unknown
    # 9256900050	120000	Unknown
    # 9755765337	50000	Unknown
    # 8825598861	40000	Unknown
    # 8210927634	10000	Unknown
    # 6388876224	65000	Unknown
    # 8302945912	10000	Unknown
    # 9549626215	20000	Unknown
    # 9717210694	20000	Unknown
    # 9848583864	7000	Unknown
    # 8789202122	75000	Unknown
    # 7987655314	15000	Unknown
    # 9704830312	20000	Unknown
    # 9687271668	40000	Unknown
    # 8329976791	30000	Unknown
    # 9414649005	75000	Unknown
    # 7373108360	50000	Unknown
    # 8433192457	7000	Unknown
    # 7000052020	7000	Unknown
    # 8291238445	25000	Unknown
    # 9799844408	150000	Unknown
    # 9085427460	100000	Unknown
    # 9886061323	50000	Unknown
    # 9930253412	35000	SACHIN BHANJI
    # 9742679680	60000	Unknown
    # 6283832822	7000	Unknown
    # 9373046009	100000	Unknown
    # 7049001492	75000	Unknown
    # 7013305349	200000	Unknown
    # 9629515476	150000	Unknown
    # 9849615040	50000	Unknown
    # 9922135730	50000	Unknown
    # 8526795907	300000	Unknown
    # 9373248841	100000	Unknown
    # 9986131104	75000	Unknown
    # 8896920914	20000	Unknown
    # 9133771305	20000	Unknown
    # 8894948335	40000	Unknown
    # 8450822190	40000	Unknown
    # 9937696917	50000	Unknown
    # 9929604758	50000	KUMAR KUMAR RAKESH
    # 6301287597	50000	Unknown
    # 7568256912	40000	Unknown
    # 9665700949	15000	PRASHANT BHARAT JAMDADE
    # 6283709603	20000	LAKHVIR KAUR
    # 9439720303	200000	Unknown
    # 6200174392	200000	Unknown
    # 9377140407	25000	Unknown
    # 7739973181	150000	Unknown
    # 8943019710	150000	Unknown
    # 9777144078	20000	Unknown
    # 8295066688	150000	Unknown
    # 8002330949	50000	Hareram Sharma NA
    # 8466842019	100000	Unknown
    # 8619683777	30000	Unknown
    # 9279016833	30000	Unknown
    # 7070048265	50000	Unknown
    # 8806366462	75000	Unknown
    # 8849283019	60000	Unknown
    # 9733582375	100000	Unknown
    # 9122345277	30000	Unknown
    # 7983850475	15000	Unknown
    # 7278337833	150000	Unknown
    # 9734132447	200000	Unknown
    # 9945868452	20000	Unknown
    # 9456957937	15000	Unknown
    # 9821271254	75000	Unknown
    # 9454274980	7000	Unknown
    # 6005415589	20000	Unknown
    # 9106076973	60000	VINAY  KUMARMISHRA
    # 9004303529	75000	Unknown
    # 8348601402	15000	NABIN MAITY
    # 7400369349	50000	Unknown
    # 9891464943	200000	Unknown
    # 8464092226	75000	Unknown
    # 9033740050	50000	KUMMARI SWATHI
    # 8860723722	40000	Unknown
    # 7488124687	40000	Unknown
    # 7038660000	20000	Unknown
    # 8340784911	20000	Unknown
    # 8884699718	150000	Unknown
    # 7773044798	40000	Unknown
    # 7893500582	40000	Unknown
    # 7478312472	50000	Unknown
    # 9711823964	20000	Unknown
    # 8094357980	20000	Unknown
    # 9789253760	40000	Unknown
    # 7011091214	50000	Unknown
    # 8857995190	25000	Unknown
    # 9130614047	50000	PADWAL SANGHARSH SADASHIV
    # 9125349482	150000	Unknown
    # 8787627027	100000	Unknown
    # 9934729540	40000	Laxman Kumar
    # 7894178503	75000	Unknown
    # 9703712503	75000	Unknown
    # 8999878832	75000	Unknown
    # 6386719738	15000	AMIT AMIT
    # 8409134693	20000	UMESH  SONI
    # 9637844546	70000	RAFIK  KAZI
    # 9016787750	50000	Unknown
    # 7877154652	10000	Unknown
    # 9767401703	40000	Unknown
    # 9256376605	150000	Unknown
    # 8247855037	50000	MIRZA KHADER ALIBAIG
    # 7427099056	7000	SK JAVED ALI
    # 7275867296	7000	Unknown
    # 8130493810	30000	Unknown
    # 9841912273	150000	Unknown
    # 9741446591	15000	Unknown
    # 9920143639	100000	CHETAN ISHWARLAL CHAUHAN
    # 9829038730	15000	Unknown
    # 8120699890	60000	Unknown
    # 7382866711	50000	Unknown
    # 9938213208	50000	Unknown
    # 9581266123	30000	Unknown
    # 8487802834	35000	Unknown
    # 9471646977	45000	Unknown
    # 9973262038	25000	Unknown
    # 8058491475	45000	Unknown
    # 9798074200	40000	Unknown
    # 9739058653	150000	Unknown
    # 9602575143	300000	MUKESH  CHOUDHARI
    # 8299447439	100000	Unknown
    # 9555185773	150000	KUMAR  SUBODH
    # 9764854357	40000	Unknown
    # 7339558072	60000	Unknown
    # 9158289796	50000	Unknown
    # 9871244159	50000	Unknown
    # 9365324787	50000	Unknown
    # 9571618874	30000	Unknown
    # 9561209082	20000	Unknown
    # 7727901284	50000	Unknown
    # 9601116721	20000	Unknown
    # 9050281897	30000	Unknown
    # 9305370140	150000	Unknown
    # 9960867009	50000	Unknown
    # 7002660254	50000	Unknown
    # 8292143850	30000	Unknown
    # 9959162646	15000	Unknown
    # 9639329283	15000	Unknown
    # 9720632996	10000	Unknown
    # 8078631664	15000	Unknown
    # 9899436238	120000	Unknown
    # 8356000957	150000	Unknown
    # 7990195915	75000	Unknown
    # 8699728914	15000	Unknown
    # 7776961888	100000	Unknown
    # 9952083326	30000	BALAJI KALPANA
    # 7499516262	7000	Unknown
    # 7709346730	150000	Unknown
    # 9989788848	40000	Unknown
    # 9931799993	5000	SANJEEV KUMAR
    # 8824115388	15000	Unknown
    # 6306433811	15000	Unknown
    # 8545910016	50000	Unknown
    # 9846198350	50000	Unknown
    # 8286686672	75000	JAVED ABDUL QURESHI
    # 7749008206	5000	Unknown
    # 9042164396	120000	ABDULLATHIEF ALAVUDEEN
    # 7531939736	150000	Unknown
    # 8082789724	40000	MANDAN WANGSA
    # 6302922719	50000	Unknown
    # 9527638615	60000	Unknown
    # 9415614572	40000	Unknown
    # 7875299191	50000	Unknown
    # 9529970763	15000	OMKAR RAJU SHRIVASTAV
    # 8469692691	75000	Unknown
    # 8521265165	95000	Unknown
    # 8918705922	25000	Unknown
    # 9467441798	105000	Unknown
    # 7322823686	200000	Unknown
    # 9065706436	30000	Unknown
    # 9917261222	25000	Unknown
    # 7507298113	80000	Unknown
    # 8077636167	20000	Unknown
    # 9993530394	7000	Unknown
    # 9113473380	35000	Unknown
    # 7044398175	150000	Unknown
    # 9948062311	40000	Unknown
    # 7047159800	50000	Unknown
    # 7800301315	10000	Unknown
    # 9694808381	40000	Unknown
    # 9995890418	300000	Unknown
    # 7014867838	200000	SHYAM SUNDER
    # 8170884661	100000	Unknown
    # 8460466510	120000	Unknown
    # 9454242874	40000	MANOJ SINGH
    # 8008448346	200000	Unknown
    # 9284587189	100000	Unknown
    # 8084723953	30000	Unknown
    # 7350073113	75000	Unknown
    # 9579855590	40000	Unknown
    # 8917271462	7000	Unknown
    # 9113473380	35000	Unknown
    # 9026531874	50000	Unknown
    # 9128245785	125000	Unknown
    # 8235612644	50000	Unknown
    # 9960045250	35000	Unknown
    # 9346232423	85000	UPPALA  RAJANI
    # 7873614991	100000	Unknown
    # 9816941801	60000	Unknown
    # 9594356646	250000	Unknown
    # 9096532584	30000	Unknown
    # 7096980106	40000	Unknown
    # 9908149129	25000	Unknown
    # 8955549571	300000	Unknown
    # 7020078303	50000	Unknown
    # 9749806031	50000	Unknown
    # 9515595280	40000	Unknown
    # 9662417765	150000	JETHVA YUVRAJSINH AGARSINH  JETHVA YUVRAJSINH
    # 9921879528	120000	Unknown
    # 9411944606	50000	Unknown
    # 7865989570	15000	Unknown
    # 8200487171	40000	Unknown
    # 8200487171	40000	Unknown
    # 9579384723	35000	Unknown
    # 9667344486	50000	Unknown
    # 8318984320	25000	Unknown
    # 7208166287	40000	Unknown
    # 9825543282	10000	Unknown
    # 9876104553	30000	GAUTAM GOSWAMI
    # 8824229213	45000	Unknown
    # 9325540073	150000	Unknown
    # 7507695003	35000	Unknown
    # 9877517449	7000	Unknown
    # 7092071135	10000	Unknown
    # 8074369201	200000	MOHAN  BUSHIGARI
    # 7738639955	5000	Unknown
    # 9949692100	100000	Unknown
    # 9654270951	20000	Unknown
    # 9971774622	40000	Unknown
    # 6354774065	40000	Unknown
    # 9987301250	15000	Unknown
    # 9898155704	75000	HIRA LAL DEV  LAL DEV
    # 9799260972	15000	Unknown
    # 9347905865	50000	ARIPAKA PRASHANTH
    # 9934901838	200000	Unknown
    # 7702157255	50000	Unknown
    # 8059458586	75000	Unknown
    # 7989236957	150000	Unknown
    # 7994424565	90000	Unknown
    # 8260335359	40000	Unknown
    # 9702636982	50000	Unknown
    # 9937656903	30000	Unknown
    # 9598036614	35000	Unknown
    # 9730492421	100000	SHASHIKANT MAHADEV SALUNKE
    # 8107443882	7000	Unknown
    # 7297023729	50000	Unknown
    # 7895743549	50000	Unknown
    # 9898619086	35000	ASFAK MUSA PATEL
    # 6363629003	75000	Unknown
    # 9958750594	50000	AMIT  SINGH
    # 6306901530	50000	SAMSHAD SAMSHAD
    # 8147878737	50000	Unknown
    # 9725573106	35000	Unknown
    # 9824934528	50000	Unknown
    # 9979660387	25000	Unknown
    # 9637781488	10000	Unknown
    # 7567153313	15000	Unknown
    # 8919449713	50000	Unknown
    # 8445300166	40000	Unknown
    # 9900717208	200000	Unknown
    # 7830918535	10000	Unknown
    # 7828255901	75000	Unknown
    # 8050802737	65000	Unknown
    # 9673677468	10000	Unknown
    # 9131701465	20000	Unknown
    # 8401113864	10000	Unknown
    # 9641671834	50000	Unknown
    # 9936265473	30000	Unknown
    # 9643862114	35000	Unknown
    # 9596610670	40000	Unknown
    # 8874855282	75000	AMAN KUMAR
    # 9959071213	75000	Pattan habibullah khan Karim khan
    # 8700315398	150000	Unknown
    # 7667715191	30000	Unknown
    # 8861548236	40000	Unknown
    # 9680858297	150000	Unknown
    # 9003689559	40000	NIKUNJA PARIDA
    # 8799726232	280000	Unknown
    # 7679399724	30000	Unknown
    # 8888244029	50000	RAJA THEVAR
    # 7002009336	75000	Unknown
    # 8011221645	50000	Unknown
    # 9977529985	40000	Unknown
    # 7250470601	5000	Unknown
    # 9915366749	50000	Unknown
    # 6264988475	75000	Unknown
    # 7350135734	15000	Unknown
    # 6360692089	50000	Unknown
    # 9916589104	20000	Unknown
    # 7976989885	75000	Unknown
    # 6206810399	25000	Unknown
    # 9326831146	20000	Unknown
    # 9799022955	20000	Unknown
    # 9944174301	10000	Unknown
    # 8147479076	15000	Unknown
    # 7022230620	30000	Unknown
    # 8178695475	20000	JASMEET SINGH
    # 9812922414	20000	KULDEEP KULDEEP
    # 9838503060	50000	Unknown
    # 7549917147	40000	Unknown
    # 8957636458	20000	Unknown
    # 9529700599	7000	Unknown
    # 9601045366	100000	Unknown
    # 8660347753	7000	Unknown
    # 9038520523	150000	Unknown
    # 7976200681	145000	GHANSHYAM BHADU
    # 7892052393	10000	Unknown
    # 9837558651	100000	AKRAM ALI
    # 9771390037	7000	Unknown
    # 9574567044	50000	Unknown
    # 7892921963	15000	Unknown
    # 9929773677	7000	Unknown
    # 9993703057	90000	Unknown
    # 9424093001	7000	Unknown
    # 7017385336	300000	Unknown
    # 9621806919	40000	VINAY KUMAR MAURYA
    # 8389845129	25000	Unknown
    # 7042209004	50000	Unknown
    # 6380369546	50000	Unknown
    # 9620135120	35000	Unknown
    # 9833624667	35000	Mani Raja
    # 9444448795	75000	Unknown
    # 8486717705	75000	Unknown
    # 8249593293	30000	Unknown
    # 8698651664	120000	Unknown
    # 9845554113	100000	Unknown
    # 6260705706	25000	Mahima Rahangdale
    # 9680024740	50000	Unknown
    # 9300612889	120000	Unknown
    # 9423910718	60000	GOPINATH PAWAR RAMAKANT
    # 7354290881	25000	Unknown
    # 9521581131	20000	Unknown
    # 9038886742	40000	Unknown
    # 7631764608	30000	Unknown
    # 8179913095	50000	SYED KHAISER
    # 9724009707	100000	Unknown
    # 8454895623	50000	Unknown
    # 9705480024	10000	Unknown
    # 9392276636	60000	Unknown
    # 9849351498	90000	Unknown
    # 9518355282	70000	Unknown
    # 8722937261	90000	Unknown
    # 8397846075	40000	Unknown
    # 7083691600	75000	Unknown
    # 8617890694	80000	Unknown
    # 9898302292	100000	Unknown
    # 9109255739	50000	Unknown
    # 8779865105	45000	Unknown
    # 9645493518	150000	Unknown
    # 7836822508	100000	Unknown
    # 6200909061	120000	Unknown
    # 7256018108	40000	Unknown
    # 9661618763	50000	Unknown
    # 9690653868	10000	Unknown
    # 9890355790	150000	PRASHANT SUKHDEO GEDAM
    # 9928431569	7000	Unknown
    # 9586686212	35000	Unknown
    # 6304679373	20000	Unknown
    # 6205234116	25000	CHHOTE LAL YADAV
    # 8969337415	10000	Unknown
    # 9919350215	45000	Unknown
    # 8920558768	75000	Unknown
    # 6260404960	35000	Unknown
    # 9890005258	300000	ABHIJEET  CHAMLE
    # 9199547679	200000	Unknown
    # 9738113169	150000	Aman Garg
    # 7619707303	30000	Unknown
    # 9945586166	120000	Unknown
    # 9738363969	50000	Unknown
    # 9925628805	200000	Unknown
    # 8825108171	75000	Unknown
    # 7038769584	40000	Unknown
    # 8888693804	75000	Unknown
    # 7568833647	200000	Unknown
    # 6395480223	100000	Unknown
    # 7666315632	7000	Unknown
    # 9001336871	150000	Unknown
    # 8825249795	7000	Unknown
    # 7838702613	200000	Unknown
    # 9467795520	200000	Unknown
    # 6361570050	20000	Unknown
    # 7999023202	75000	Unknown
    # 7908545953	200000	Unknown
    # 9855335594	100000	RAVINDER RAVINDER
    # 9492590727	200000	Unknown
    # 7022353095	45000	Unknown
    # 7022353095	45000	Unknown
    # 8824648340	7000	Unknown
    # 8688880141	150000	Unknown
    # 7844883737	65000	Unknown
    # 7000184296	25000	Unknown
    # 7041544423	7000	Unknown
    # 8218543288	75000	Unknown
    # 7674828626	25000	Unknown
    # 9049077379	7000	Unknown
    # 9368877362	75000	Unknown
    # 8829925149	20000	Unknown
    # 9588967739	75000	Unknown
    # 9002104866	40000	Unknown
    # 9333667251	150000	Unknown
    # 9988967626	40000	Unknown
    # 9099424827	100000	Unknown
    # 7350576283	120000	GAHININATH  AAGLAWE
    # 8292036856	150000	Unknown
    # 9199192823	25000	Unknown
    # 8132967600	75000	Unknown
    # 9056313224	15000	Unknown
    # 8754460769	45000	Unknown
    # 9065112177	20000	Unknown
    # 8799791720	150000	VIKRAM SINGH YOGI
    # 9985050605	50000	Unknown
    # 9829149783	50000	Unknown
    # 8890774323	40000	Unknown
    # 8879965658	50000	Unknown
    # 9051705969	90000	Unknown
    # 9792561251	75000	Unknown
    # 7023032911	25000	Unknown
    # 9771035555	50000	Unknown
    # 9324369991	40000	Unknown
    # 9130151429	20000	VIVEK SUDHAKARRAO GUMPHALWAR
    # 9767824063	120000	Unknown
    # 9110121470	25000	Unknown
    # 9811045834	40000	Unknown
    # 7028621122	100000	Unknown
    # 9552026886	150000	Unknown
    # 9040375938	75000	Unknown
    # 7838116253	40000	Unknown
    # 6394436094	40000	Unknown
    # 9227018892	50000	Unknown
    # 9167642543	100000	Unknown
    # 9867632821	7000	PHILIP  SWAMY
    # 8329976791	30000	Unknown
    # 7584856061	75000	Unknown
    # 8421145050	40000	Unknown
    # 9420430475	100000	Unknown
    # 9844378065	50000	Unknown
    # 8677949257	40000	Unknown
    # 6398612198	200000	Unknown
    # 7007086353	30000	Unknown
    # 9740710395	100000	Unknown
    # 9967179039	30000	Unknown
    # 7727866710	50000	Unknown
    # 9701551702	60000	Unknown
    # 7005501633	7000	Unknown
    # 7559468288	70000	Unknown
    # 8249337853	75000	Unknown
    # 9155305821	70000	Unknown
    # 9964889904	50000	Unknown
    # 9554646677	7000	Unknown
    # 9894258181	200000	Unknown
    # 9324137505	40000	Unknown
    # 9953948650	150000	SHIVSHANKAR  SHIV
    # 9819713075	75000	Unknown
    # 9630300535	75000	Unknown
    # 6352123633	40000	Unknown
    # 9980432374	75000	Unknown
    # 8104780026	50000	Unknown
    # 9886749996	40000	Unknown
    # 7486908470	35000	Unknown
    # 7411981944	75000	Unknown
    # 9224665683	50000	Unknown
    # 8160627212	50000	Unknown
    # 9748393707	150000	Unknown
    # 9556957706	50000	Kalpana Behera
    # 9880825779	150000	MD LAEEQ AHMED
    # 9769765624	60000	Unknown
    # 9747893098	75000	Unknown
    # 9421459997	50000	Unknown
    # 9993103691	50000	Unknown
    # 8427227943	50000	Unknown
    # 8319621245	50000	Unknown
    # 8872654796	10000	Unknown
    # 9762518325	75000	Unknown
    # 7278866420	40000	Tarun Kumar Dey
    # 8850299736	40000	Unknown
    # 9267964279	7000	Unknown
    # 9665539975	200000	Unknown
    # 8901594378	50000	Unknown
    # 9524225794	15000	Unknown
    # 8630926944	100000	Unknown
    # 9694657729	7000	Unknown
    # 7330856817	45000	JALASUTRAM DEXON ABHAY KUMAR  DEXON AK
    # 8921551248	40000	Unknown
    # 9011580460	50000	Unknown
    # 6203036824	10000	Unknown
    # 7359080544	100000	Unknown
    # 8076726831	30000	Unknown
    # 9771551988	20000	Unknown
    # 8140784203	40000	Unknown
    # 9867674778	40000	Unknown
    # 9649080401	35000	Unknown
    # 9915113565	50000	Unknown
    # 9917247304	50000	Unknown
    # 8320239380	300000	Unknown
    # 7301203234	50000	Unknown
    # 6266233048	25000	Unknown
    # 8263824059	25000	Unknown
    # 8873583257	50000	Unknown
    # 8873583257	50000	Unknown
    # 9928727222	50000	Unknown
    # 8409131737	75000	Unknown
    # 9518567271	25000	Unknown
    # 9798243277	130000	Unknown
    # 9866985851	25000	Unknown
    # 9639756786	60000	Unknown
    # 8952056502	20000	SHER MOHAMMED
    # 8857817967	35000	Unknown
    # 9430990023	40000	Unknown
    # 7499704773	100000	Unknown
    # 7798181150	100000	Unknown
    # 8899244710	20000	Unknown
    # 7667726502	25000	MUNSI THAKUR
    # 7621831027	50000	Unknown
    # 7878378963	15000	Unknown
    # 9714907104	50000	Unknown
    # 9923460437	75000	Unknown
    # 9711905956	40000	Unknown
    # 9824051867	50000	Unknown
    # 9742127967	20000	Unknown
    # 7092786145	45000	Unknown
    # 9948149894	7000	Unknown
    # 7400350963	50000	CHANDAN SATENDRA SINGH
    # 9058948384	105000	Unknown
    # 7377639303	50000	KAMALAKANTA SAHOO
    # 7493006358	20000	Unknown
    # 9117985663	40000	Unknown
    # 7317293915	65000	Unknown
    # 8587970193	10000	RANJIT KUMAR RAY
    # 6354749472	50000	Unknown
    # 9703580671	25000	Unknown
    # 9627888432	15000	Unknown
    # 8792671315	20000	Unknown
    # 9988967626	40000	Unknown
    # 9675037612	150000	Unknown
    # 8660994540	7000	MUKRRAM MUKRRAM
    # 9301193401	50000	Unknown
    # 8178860804	55000	Unknown
    # 8509354299	35000	Unknown
    # 7521853980	35000	Unknown
    # 7065911964	50000	RAVENDRA SINGH
    # 9767196219	150000	VITHTHAL RAMESHWAR BHOGIL
    # 7995198898	200000	GOHEL BHIKHABHAI
    # 7772902922	7000	Unknown
    # 9315710962	5000	Unknown
    # 9074078097	100000	Unknown
    # 9967674771	50000	Unknown
    # 8343881078	65000	Unknown
    # 7730099377	40000	Unknown
    # 9022826502	7000	Unknown
    # 7618592134	60000	Unknown
    # 9581891131	50000	Unknown
    # 9901205258	45000	Unknown
    # 8700959132	30000	Unknown
    # 9728004770	20000	Unknown
    # 7249580179	50000	Unknown
    # 9971840896	45000	Unknown
    # 8876754951	50000	Unknown
    # 7802928176	35000	Unknown
    # 9571205722	75000	Unknown
    # 9821228919	40000	Unknown
    # 8077513426	15000	Unknown
    # 9571239054	150000	Unknown
    # 8217624241	200000	PRAKASH CHANDRA  PANDEY
    # 8317444470	40000	Unknown
    # 9799594991	35000	Unknown
    # 7719834155	7000	GOPAL DEVCHAND UIKEY
    # 7899087765	10000	JAFAR SHARIFF
    # 8104722823	50000	Unknown
    # 9611885630	35000	Unknown
    # 8268850451	75000	Unknown
    # 9027782095	40000	Unknown
    # 9582148693	120000	Unknown
    # 9930680957	100000	Unknown
    # 8178590833	150000	Unknown
    # 7000866134	100000	Unknown
    # 8360860350	5000	Unknown
    # 8709742010	20000	Unknown
    # 7599990053	100000	Unknown
    # 9405110165	50000	Unknown
    # 9205052189	50000	Unknown
    # 9550909126	30000	Unknown
    # 9662922590	50000	Unknown
    # 9207458006	40000	Unknown
    # 8249055403	35000	Unknown
    # 7275311350	7000	Unknown
    # 8374249247	45000	Unknown
    # 9166327243	115000	Unknown
    # 9051517456	50000	Unknown
    # 6301134917	50000	Unknown
    # 9261030786	120000	Unknown
    # 9099876371	10000	Unknown
    # 8271804301	50000	Unknown
    # 9623323856	100000	Unknown
    # 7814550241	45000	Unknown
    # 9591116436	100000	Unknown
    # 7563885611	75000	Unknown
    # 9412492107	40000	Unknown
    # 9636740686	50000	Unknown
    # 9670016336	150000	PRADEEP KUMAR SINGH
    # 9997855306	75000	Unknown
    # 9887408040	50000	Unknown
    # 8318347375	20000	Unknown
    # 7439837325	40000	JUGAL MAHATO
    # 6201863200	10000	Unknown
    # 9582363766	100000	Unknown
    # 7504882784	25000	Unknown
    # 8099545512	85000	Unknown
    # 7086719991	50000	Unknown
    # 7762850509	300000	Unknown
    # 9329518902	100000	Unknown
    # 9511834597	100000	Unknown
    # 9373907391	60000	Unknown
    # 9731919933	75000	Unknown
    # 9759957424	100000	Unknown
    # 9162004024	35000	Unknown
    # 9081026222	55000	DEVANAND PRASAD
    # 9691019476	45000	Unknown
    # 8122117192	120000	Unknown
    # 8050979943	50000	Unknown
    # 9997964542	40000	Unknown
    # 9888324432	35000	Unknown
    # 7507901174	10000	Unknown
    # 7622075002	35000	Unknown
    # 8015325275	15000	Unknown
    # 8432308917	7000	Unknown
    # 6260011353	75000	Unknown
    # 8653495276	30000	Unknown
    # 7977211049	50000	Unknown
    # 7497083083	75000	Unknown
    # 9910992099	100000	Unknown
    # 9015249330	65000	Unknown
    # 9558341145	60000	Unknown
    # 8755029273	10000	Unknown
    # 9041055974	70000	Unknown
    # 8822882613	25000	Unknown
    # 9812832195	45000	Unknown
    # 8853041758	30000	Unknown
    # 6301722527	50000	Unknown
    # 8329198997	75000	SAHADEV GANAGARAM
    # 9623834379	50000	Unknown
    # 9313242054	10000	Unknown
    # 9168435339	40000	Unknown
    # 9594251102	20000	Unknown
    # 9660620992	10000	Unknown
    # 9873216717	7000	Unknown
    # 9967954793	50000	Unknown
    # 9813202524	75000	MAJID MAJID
    # 8600446921	15000	Unknown
    # 9830464403	155000	FULLARA DHAR
    # 9433393347	100000	Unknown
    # 9247199883	200000	Unknown
    # 9315383091	60000	Unknown
    # 9164420044	150000	Unknown
    # 7895717356	50000	DHARMENDRA DHARMENDRA
    # 9904490363	200000	Unknown
    # 9704830312	5000	Unknown
    # 9736746143	50000	Unknown
    # 9623759695	7000	Unknown
    # 7418384088	15000	Unknown
    # 8290907431	40000	Unknown
    # 9591324272	7000	Unknown
    # 8017795486	40000	Unknown
    # 9916423529	7000	Unknown
    # 8483088937	50000	Unknown
    # 7418867910	30000	SURESH  KUMAR
    # 9988365641	75000	Unknown
    # 8837475855	10000	Unknown
    # 9975156327	20000	Unknown
    # 8800955180	35000	SANTOSH CHOUDHARY
    # 9882826161	75000	raj singh
    # 7509934103	150000	Unknown
    # 7876452319	120000	Unknown
    # 9850521818	200000	Unknown
    # 9315239521	25000	Unknown
    # 8766899858	150000	Unknown
    # 6392159506	10000	Unknown
    # 9811843267	50000	Unknown
    # 7448127648	40000	Akshita Jadhav
    # 6306841828	15000	Unknown
    # 8000411384	15000	Unknown
    # 7477716567	15000	Unknown
    # 9029375690	40000	Unknown
    # 8452031382	70000	Unknown
    # 9701115111	200000	Unknown
    # 9315009026	300000	Unknown
    # 8390534286	150000	KRUSHNATH  MALI
    # 8975747308	120000	Satish Digambar Mathankar
    # 9737330716	75000	Unknown
    # 9741448186	10000	Unknown
    # 9542933379	40000	Unknown
    # 9654319874	40000	NARESH NARESH
    # 8121076696	75000	Unknown
    # 7384106733	300000	Unknown
    # 7549798757	155000	Unknown
    # 8340482887	150000	UMESH NAG
    # 9741538612	150000	Unknown
    # 9767429263	40000	Unknown
    # 9130236003	60000	Unknown
    # 6202821706	7000	Unknown
    # 9019268633	40000	Unknown
    # 8521600644	15000	Unknown
    # 7488732097	40000	Unknown
    # 9866868845	150000	Unknown
    # 9845437706	150000	Unknown
    # 7001624499	300000	Unknown
    # 8006441903	75000	Unknown
    # 9699673471	30000	Unknown
    # 7324966194	50000	Unknown
    # 9140569795	150000	Unknown
    # 9905626533	5000	VIKAS  KUMAR
    # 9563441511	7000	Unknown
    # 7483190710	50000	Unknown
    # 6206788691	50000	Unknown
    # 8590804719	30000	Unknown
    # 9861176474	150000	Unknown
    # 9352465152	7000	Unknown
    # 9849939736	55000	Unknown
    # 9662596706	50000	Unknown
    # 8097799086	55000	Unknown
    # 9472399114	50000	Unknown
    # 8692921081	25000	Unknown
    # 7348835561	7000	JOY DEEP ROY
    # 9545215381	150000	Unknown
    # 9888468105	60000	SANT RAM MAHAJAN
    # 9689113636	7000	Unknown
    # 9725713630	30000	SIMABAHEN RAJKUMAR YADAV
    # 9716789626	105000	Unknown
    # 9636664384	50000	Unknown
    # 8530661389	75000	Unknown
    # 9155090735	55000	ALI ANSARI
    # 8240565640	35000	Unknown
    # 7038505454	40000	Unknown
    # 8638146976	105000	Unknown
    # 9635567922	40000	PRADIP SARKAR
    # 9656551100	100000	Unknown
    # 8920362430	50000	Unknown
    # 8864009899	45000	Unknown
    # 9172763972	70000	Unknown
    # 7401458865	30000	Unknown
    # 7038487090	45000	Unknown
    # 9621105091	40000	Unknown
    # 9923392754	150000	Unknown
    # 9451158889	60000	Unknown
    # 9119648125	30000	Unknown
    # 8923152388	35000	Unknown
    # 9588207448	50000	Unknown
    # 6398875275	40000	Unknown
    # 9503477552	125000	Unknown
    # 9507266837	75000	Unknown
    # 9848827706	150000	Unknown
    # 9967503760	200000	ADESH RAMESH PARAB
    # 7696794261	30000	Unknown
    # 8607988128	20000	Unknown
    # 7980157989	50000	Unknown
    # 9630083483	75000	Unknown
    # 9068075249	100000	Unknown
    # 8733868250	100000	Unknown
    # 9720765652	15000	Unknown
    # 8310176492	20000	Unknown
    # 9413789260	40000	Unknown
    # 8511511489	40000	Unknown
    # 9370580453	300000	RICHA POTDAR
    # 9653788036	30000	HANUMAN BAIRWA
    # 9823214760	50000	Unknown
    # 8897424955	60000	Unknown
    # 8178313973	35000	Unknown
    # 9380834500	10000	Unknown
    # 9784585761	50000	Unknown
    # 7002070288	40000	Unknown
    # 8605944053	200000	Unknown
    # 9725709196	150000	Unknown
    # 9811815964	40000	Unknown
    # 9686493351	50000	Unknown
    # 8225879437	50000	Unknown
    # 7457836805	40000	Unknown
    # 7087709851	7000	Unknown
    # 9766243585	50000	Unknown
    # 7259757579	100000	Unknown
    # 8126031593	50000	Unknown
    # 9895428236	100000	Unknown
    # 9506246002	40000	Unknown
    # 9552754536	7000	Unknown
    # 9620958563	50000	Unknown
    # 9938116366	40000	Unknown
    # 8171387806	50000	Unknown
    # 8160994784	50000	Unknown
    # 9758706793	15000	Unknown
    # 8745917842	200000	 ANJU
    # 8954586253	75000	Unknown
    # 9708421810	20000	Unknown
    # 9309333104	30000	Unknown
    # 9311982118	7000	Unknown
    # 9628680625	7000	Unknown
    # 9131793388	35000	Unknown
    # 8780725703	35000	Unknown
    # 9038271220	50000	Unknown
    # 7974871566	90000	Unknown
    # 8528333224	40000	Unknown
    # 9411944606	5000	Unknown
    # 9035583401	100000	Unknown
    # 6006238430	35000	Unknown
    # 6239036459	75000	Unknown
    # 8837792990	35000	Unknown
    # 9726019749	20000	Unknown
    # 9955466825	7000	Unknown
    # 9840502562	100000	Unknown
    # 9483381905	70000	Unknown
    # 9137474610	100000	Unknown
    # 9415469281	40000	Unknown
    # 9974367484	30000	Unknown
    # 9873940520	100000	Unknown
    # 9058833918	300000	Unknown
    # 9031167008	50000	Unknown
    # 7359515983	20000	Unknown
    # 7042209004	150000	Unknown
    # 9036493217	45000	Unknown
    # 7351638516	60000	RIJWAN
    # 9564943693	50000	Unknown
    # 7988737441	150000	Unknown
    # 9337755710	35000	Unknown
    # 8459005255	15000	Unknown
    # 9493679084	40000	MD MORJAN
    # 8617757503	30000	Unknown
    # 9321044574	100000	Unknown
    # 9848351453	50000	Unknown
    # 7002866911	40000	Unknown
    # 6395102967	100000	Unknown
    # 9967106152	15000	Unknown
    # 7866955458	40000	MD SUFI MONSURI
    # 7351244473	45000	Unknown
    # 7324086267	200000	Unknown
    # 9748596492	60000	Unknown
    # 7606949550	60000	Unknown
    # 9676196524	120000	Unknown
    # 8709931982	75000	Unknown
    # 9588648428	75000	Unknown
    # 9526525912	30000	SAJESH M V
    # 7978041699	40000	Unknown
    # 9971145964	40000	Unknown
    # 9309823190	75000	Unknown
    # 7485981042	200000	Unknown
    # 9372737108	200000	Mir Amjad Ali
    # 7982901299	150000	Unknown
    # 9975297255	40000	Unknown
    # 7983585601	25000	Hari prakash
    # 8985497657	50000	Unknown
    # 7062583490	50000	GURPREET SINGH
    # 7709698084	100000	Unknown
    # 8077541035	75000	Unknown
    # 9889447824	40000	Unknown
    # 9501533236	75000	Unknown
    # 9749296227	50000	Unknown
    # 8126588004	15000	Unknown
    # 9540851149	30000	Unknown
    # 9867203746	10000	Unknown
    # 8809594242	25000	Unknown
    # 8090045648	50000	Unknown
    # 9540603280	70000	Unknown
    # 9901366934	45000	Abdul Asif
    # 9898961103	75000	Unknown
    # 9742538689	105000	Unknown
    # 7974767952	30000	NARENDRA YADAV
    # 9804359948	200000	Unknown
    # 7678685278	200000	Unknown
    # 7061126327	75000	Unknown
    # 9438159934	50000	Unknown
    # 9511161509	100000	Unknown
    # 9662454464	100000	Unknown
    # 9902219037	75000	Unknown
    # 9740428682	80000	Unknown
    # 9873790276	20000	Unknown
    # 9123327857	15000	Unknown
    # 9642045404	30000	Unknown
    # 8106135758	20000	Unknown
    # 7899171346	25000	Unknown
    # 6398828110	7000	Unknown
    # 8928786549	300000	Unknown
    # 9797662357	75000	Unknown
    # 9758620751	75000	Unknown
    # 7979932861	100000	Unknown
    # 7841959058	40000	Unknown
    # 9867557402	50000	Unknown
    # 9159940013	45000	Unknown
    # 9612489900	50000	Unknown
    # 9529323389	5000	Unknown
    # 9123025931	45000	Unknown
    # 7088289051	50000	Unknown
    # 8003524582	30000	Unknown
    # 8448748336	150000	Unknown
    # 9637363937	30000	Unknown
    # 8459896237	25000	Unknown
    # 6200849452	130000	Unknown
    # 9284209012	50000	Unknown
    # 8095759162	40000	Unknown
    # 8051341741	30000	Unknown
    # 9904152092	190000	Unknown
    # 9920609190	30000	Unknown
    # 8320437403	50000	Unknown
    # 9927262402	5000	Unknown
    # 7057549570	40000	Unknown
    # 9881020863	50000	Unknown
    # 9689113636	40000	Unknown
    # 9772695212	60000	Unknown
    # 9536003620	65000	PHOOL SINGH
    # 9106736680	7000	Unknown
    # 9521524044	40000	Unknown
    # 9518388914	75000	Unknown
    # 8000567178	40000	Unknown
    # 8650588801	35000	YOGESH KUMAR
    # 9673042315	40000	Unknown
    # 9915853192	40000	Unknown
    # 9559680999	30000	Unknown
    # 9636369736	45000	Unknown
    # 9600422911	100000	Unknown
    # 9966392824	60000	Unknown
    # 9026541939	40000	Unknown
    # 7679989840	50000	Unknown
    # 8729957493	40000	Unknown
    # 8894102674	150000	Unknown
    # 8210094602	50000	Unknown
    # 7434878725	100000	PRASHANT  KUMAR
    # 7015679653	50000	Unknown
    # 9771349682	20000	RANJIT KUMAR
    # 8374892525	35000	Unknown
    # 7488391314	55000	Unknown
    # 7907322978	75000	Unknown
    # 9829718638	15000	Unknown
    # 9657960721	100000	Unknown
    # 9161282592	50000	Unknown
    # 8686885974	100000	Unknown
    # 9582363766	30000	Unknown
    # 9034806178	125000	Unknown
    # 7736311473	50000	Unknown
    # 9945555915	30000	Unknown
    # 9661725018	50000	KUSHANU KUMAR
    # 9101757795	60000	Unknown
    # 9792335878	200000	Unknown
    # 7209818610	5000	Unknown
    # 8950725387	75000	Unknown
    # 9343548664	15000	Unknown
    # 8802428214	25000	Unknown
    # 9079850040	20000	Unknown
    # 7327869894	150000	Unknown
    # 8008450333	75000	MANGALI  SANDEEP
    # 9166037888	5000	Unknown
    # 9899440017	50000	Unknown
    # 8534980378	75000	Unknown
    # 6361971836	40000	Unknown]
    # 8582813766	75000	Unknown
    # 7354473760	40000	Unknown
    # 9819713075	75000	Unknown
    # 7564872527	150000	Unknown
    # 8290976812	150000	Unknown
    # 6307984953	40000	Unknown
    # 9324748546	300000	Unknown
    # 7338899542	50000	Unknown
    # 6001526213	35000	Unknown
    # 6289588943	35000	Unknown
    # 7007684426	50000	Unknown
    # 9953538858	75000	Unknown
    # 8286321776	20000	Unknown
    # 9270226282	40000	Unknown
    # 8866068685	50000	Unknown
    # 7089166240	20000	MANVENDRA LODHI
    # 6205050215	40000	Unknown
    # 9673953691	75000	Unknown
    # 8793505023	75000	Unknown
    # 7894899964	20000	Unknown
    # 8700668507	50000	Unknown
    # 9634544087	50000	Unknown
    # 8102365400	60000	Unknown


     return 

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
