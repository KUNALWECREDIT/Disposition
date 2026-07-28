/* Dialer Campaign Console — static renderer
 * Fetches ./data.json (produced by export_static.py) and renders the
 * campaign nav + disposition/calling tables entirely client-side.
 * No server, no DB connection — safe for GitHub Pages.
 */

const navList = document.getElementById("nav-list");
const mainPanel = document.getElementById("main-panel");

let DATA = null;

function pctClass(value) {
  if (value === null || value === undefined) return "pct-neutral";
  const n = parseFloat(String(value).replace("%", ""));
  if (Number.isNaN(n)) return "pct-neutral";
  if (n >= 60) return "pct-high";
  if (n >= 30) return "pct-mid";
  return "pct-low";
}

function pct(numer, denom) {
  const n = Number(numer) || 0;
  const d = Number(denom) || 0;
  if (!d) return "0%";
  return `${Math.round((n / d) * 10000) / 100}%`;
}

function fmtTs(ts) {
  if (!ts) return "never";
  return new Date(ts * 1000).toLocaleString();
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderNav(activeCampaign) {
  if (!DATA.campaigns.length) {
    navList.innerHTML = `<p class="nav-empty">No cached campaigns in data.json.</p>`;
    return;
  }
  navList.innerHTML = DATA.campaigns
    .map((c) => {
      const active = c === activeCampaign ? "nav-item-active" : "";
      const href = `#${encodeURIComponent(c)}`;
      return `<a href="${href}" class="nav-item ${active}"><span class="nav-item-dot"></span>${escapeHtml(c)}</a>`;
    })
    .join("");
}

function buildDispositionTable(payload) {
  if (!payload) {
    return `<p class="empty-row">No disposition data cached for this campaign.</p>`;
  }
  const { columns, rows, timestamp } = payload;
  const head = columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
  const body = rows.length
    ? rows
        .map((row) => {
          const rowClass = row["Disposition"] === "TOTAL" ? "row-total" : "";
          const cells = columns
            .map((col) => {
              let cls = "";
              if (col === "Percentage") cls = `mono ${pctClass(row[col])}`;
              else if (col === "Unique_Calls" || col === "Avg_Attempts") cls = "mono";
              return `<td class="${cls}">${escapeHtml(row[col])}</td>`;
            })
            .join("");
          return `<tr class="${rowClass}">${cells}</tr>`;
        })
        .join("")
    : `<tr><td colspan="${columns.length}" class="empty-row">No disposition data for today.</td></tr>`;

  return `
    <div class="panel-head">
      <h2>Today's Disposition Summary</h2>
      <span class="panel-sub">Unique clients reached today, by outcome &middot; cached as of <span class="mono">${fmtTs(timestamp)}</span></span>
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function buildCallingTable(payload) {
  if (!payload) {
    return `<p class="empty-row">No calling report data cached for this campaign.</p>`;
  }
  const { columns, rows, timestamp } = payload;
  const extraCols = ["Connected_%", "UNQ_Connected_%", "True_Connected_%"];
  const allCols = [...columns, ...extraCols];
  const head = allCols.map((c) => `<th>${escapeHtml(c.replace(/_/g, " "))}</th>`).join("");

  const body = rows.length
    ? rows
        .map((row) => {
          const connectedPct = pct(row.Connected, row.Attempt);
          const unqConnectedPct = pct(row.UNQ_Connected, row.UNQ_Attempt);
          const trueConnectedPct = pct(row.True_Connected, row.Attempt);
          const merged = { ...row, "Connected_%": connectedPct, "UNQ_Connected_%": unqConnectedPct, "True_Connected_%": trueConnectedPct };

          const cells = allCols
            .map((col) => {
              let cls = "";
              if (col.endsWith("_%")) cls = `mono ${pctClass(merged[col])}`;
              else if (col !== "Campaign_Name" && col !== "date") cls = "mono";
              return `<td class="${cls}">${escapeHtml(merged[col])}</td>`;
            })
            .join("");
          return `<tr>${cells}</tr>`;
        })
        .join("")
    : `<tr><td colspan="${allCols.length}" class="empty-row">No calling data cached.</td></tr>`;

  return `
    <div class="panel-head">
      <h2>Last 4 Months Calling Report</h2>
      <span class="panel-sub">Daily attempts, connects and outcomes &middot; cached as of <span class="mono">${fmtTs(timestamp)}</span></span>
    </div>
    <div class="table-scroll table-scroll-wide">
      <table class="data-table data-table-dense">
        <thead><tr>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function renderCampaign(campaign) {
  if (!campaign || !DATA.campaigns.includes(campaign)) {
    mainPanel.innerHTML = `
      <div class="empty-state">
        <h1>No campaign selected</h1>
        <p>Pick a campaign from the left, or the data.json snapshot has no campaigns in it yet.</p>
      </div>`;
    return;
  }

  renderNav(campaign);

  mainPanel.innerHTML = `
    <div class="offline-banner">
      <span class="pulse-dot pulse-dot-offline"></span>
      Static snapshot — generated ${escapeHtml(DATA.generated_at)}. This page never connects to the database.
    </div>
    <header class="main-header">
      <div>
        <div class="eyebrow">Campaign</div>
        <h1>${escapeHtml(campaign)}</h1>
      </div>
    </header>
    <section class="panel">${buildDispositionTable(DATA.disposition[campaign])}</section>
    <section class="panel">${buildCallingTable(DATA.calling[campaign])}</section>
  `;
}

function currentCampaignFromHash() {
  const raw = decodeURIComponent(window.location.hash.replace(/^#/, ""));
  return raw || (DATA.campaigns.length ? DATA.campaigns[0] : null);
}

window.addEventListener("hashchange", () => {
  if (DATA) renderCampaign(currentCampaignFromHash());
});

fetch("data.json")
  .then((r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  })
  .then((json) => {
    DATA = json;
    const initial = currentCampaignFromHash();
    renderNav(initial);
    renderCampaign(initial);
  })
  .catch((err) => {
    mainPanel.innerHTML = `
      <div class="empty-state">
        <h1>Couldn't load data.json</h1>
        <p>${escapeHtml(err.message)}</p>
        <p>Make sure data.json sits next to index.html, and that you're viewing this
        over http(s) (not a bare file:// path — run a local server like
        <code>python -m http.server</code> inside this folder to preview it).</p>
      </div>`;
    navList.innerHTML = `<p class="nav-empty">Failed to load.</p>`;
  });
