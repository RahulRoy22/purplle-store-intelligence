"""
routers/dashboard.py — Live web dashboard served at GET /dashboard

A self-contained HTML page (Bootstrap 5 + vanilla JS) that polls the
analytics API every 5 seconds and renders:
  - KPI cards  (visitors, conversion, dwell, abandonment)
  - Conversion funnel (% bars)
  - Zone heatmap     (heat-coloured bars + data_confidence badge)
  - Anomaly panel    (severity-coded cards with suggested_action)

No external build step, no React, no webpack — just one HTML response.
The JS calls the same origin's REST endpoints directly.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from core.config import get_settings

router = APIRouter()

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Purplle Store Intelligence</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  :root {
    --purple: #7c3aed;
    --purple-light: #a78bfa;
    --dark-bg: #0f0f1a;
    --card-bg: #1a1a2e;
    --card-border: #2d2d4e;
    --muted: #6b7280;
  }
  body { background: var(--dark-bg); color: #e2e8f0; font-family: 'Segoe UI', sans-serif; }
  .navbar { background: linear-gradient(90deg, #1a003a 0%, #0f0f1a 100%); border-bottom: 1px solid var(--card-border); }
  .navbar-brand { color: var(--purple-light) !important; font-weight: 700; letter-spacing: .5px; }
  .badge-store { background: #2d1b69; color: var(--purple-light); padding: 4px 10px; border-radius: 20px; font-size: .8rem; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .status-dot.ok { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
  .status-dot.bad { background: #ef4444; box-shadow: 0 0 6px #ef4444; }
  .kpi-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 20px 24px; height: 100%; transition: transform .15s; }
  .kpi-card:hover { transform: translateY(-2px); }
  .kpi-label { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .6px; margin-bottom: 8px; }
  .kpi-value { font-size: 2.2rem; font-weight: 700; line-height: 1; }
  .kpi-sub { color: var(--muted); font-size: .78rem; margin-top: 6px; }
  .section-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 20px; height: 100%; }
  .section-title { font-size: .78rem; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); margin-bottom: 16px; }
  .funnel-bar-track { background: #0f0f1a; border-radius: 4px; height: 18px; position: relative; overflow: hidden; }
  .funnel-bar-fill { height: 100%; border-radius: 4px; transition: width .4s ease; }
  .funnel-row { margin-bottom: 12px; }
  .funnel-label { font-size: .82rem; color: #cbd5e1; margin-bottom: 4px; display: flex; justify-content: space-between; }
  .heat-bar-track { background: #0f0f1a; border-radius: 4px; height: 16px; overflow: hidden; }
  .heat-bar-fill { height: 100%; border-radius: 4px; transition: width .4s ease; }
  .heat-row { margin-bottom: 10px; }
  .heat-label { font-size: .82rem; color: #cbd5e1; margin-bottom: 3px; display: flex; justify-content: space-between; align-items: center; }
  .anomaly-item { border-radius: 8px; padding: 12px 16px; margin-bottom: 10px; border-left: 3px solid; }
  .anomaly-critical { background: #2d0a0a; border-color: #ef4444; }
  .anomaly-warn     { background: #1f1700; border-color: #f59e0b; }
  .anomaly-info     { background: #0a1429; border-color: #3b82f6; }
  .anomaly-type { font-size: .75rem; font-weight: 700; letter-spacing: .5px; text-transform: uppercase; }
  .anomaly-msg  { font-size: .83rem; color: #94a3b8; margin-top: 3px; line-height: 1.4; }
  .anomaly-action { font-size: .78rem; color: #64748b; margin-top: 4px; font-style: italic; }
  .no-anomaly { text-align: center; color: #22c55e; padding: 24px 0; font-size: .9rem; }
  .confidence-badge { font-size: .7rem; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
  .confidence-high { background: #052e16; color: #4ade80; }
  .confidence-low  { background: #1c1000; color: #fbbf24; }
  .refresh-info { font-size: .75rem; color: var(--muted); }
  .spinner-sm { width: 12px; height: 12px; border-width: 2px; }
  #last-updated { font-size: .78rem; color: var(--muted); }
  .color-purple { color: var(--purple-light); }
  .color-green  { color: #4ade80; }
  .color-yellow { color: #fbbf24; }
  .color-red    { color: #f87171; }
  .color-cyan   { color: #67e8f9; }
</style>
</head>
<body>

<!-- Navbar -->
<nav class="navbar navbar-dark px-4 py-3 mb-4">
  <span class="navbar-brand">&#9679; Purplle Store Intelligence</span>
  <div class="d-flex align-items-center gap-3">
    <span class="badge-store" id="store-badge">store: —</span>
    <span id="api-status"><span class="status-dot bad" id="status-dot"></span><span id="status-text" class="small">connecting…</span></span>
    <span id="last-updated">—</span>
    <div class="spinner-border spinner-sm text-secondary d-none" id="spinner" role="status"></div>
  </div>
</nav>

<div class="container-fluid px-4">

  <!-- KPI Cards -->
  <div class="row g-3 mb-4" id="kpi-row">
    <div class="col-6 col-md-3">
      <div class="kpi-card">
        <div class="kpi-label">Unique Visitors</div>
        <div class="kpi-value color-purple" id="kpi-visitors">—</div>
        <div class="kpi-sub">customers (excl. staff)</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="kpi-card">
        <div class="kpi-label">Conversion Rate</div>
        <div class="kpi-value color-green" id="kpi-conversion">—</div>
        <div class="kpi-sub">billing → purchase</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="kpi-card">
        <div class="kpi-label">Avg Dwell Time</div>
        <div class="kpi-value color-cyan" id="kpi-dwell">—</div>
        <div class="kpi-sub">per zone visit</div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="kpi-card">
        <div class="kpi-label">Billing Abandonment</div>
        <div class="kpi-value color-yellow" id="kpi-abandon">—</div>
        <div class="kpi-sub">queue drop-off rate</div>
      </div>
    </div>
  </div>

  <!-- Second row: Funnel | Heatmap | Anomalies -->
  <div class="row g-3">

    <!-- Funnel -->
    <div class="col-12 col-md-4">
      <div class="section-card">
        <div class="section-title">Conversion Funnel</div>
        <div id="funnel-body">
          <p class="text-secondary small">Loading…</p>
        </div>
      </div>
    </div>

    <!-- Heatmap -->
    <div class="col-12 col-md-4">
      <div class="section-card">
        <div class="d-flex justify-content-between align-items-center mb-3">
          <div class="section-title mb-0">Zone Heatmap</div>
          <span class="confidence-badge d-none" id="confidence-badge">—</span>
        </div>
        <div id="heatmap-body">
          <p class="text-secondary small">Loading…</p>
        </div>
      </div>
    </div>

    <!-- Anomalies -->
    <div class="col-12 col-md-4">
      <div class="section-card">
        <div class="section-title">Anomaly Alerts</div>
        <div id="anomaly-body">
          <p class="text-secondary small">Loading…</p>
        </div>
      </div>
    </div>

  </div>

  <div class="mt-3 mb-4 refresh-info text-end">
    Real-time via SSE &nbsp;&bull;&nbsp; <a href="/docs" class="text-secondary">API docs</a>
  </div>
</div>

<script>
const STORE_ID = "STORE_ID_PLACEHOLDER";

document.getElementById("store-badge").textContent = "store: " + STORE_ID;

function fmtMs(ms) {
  if (ms == null) return "—";
  const s = ms / 1000;
  if (s >= 60) return (s / 60).toFixed(1) + " min";
  return s.toFixed(0) + " s";
}
function fmtPct(v) {
  if (v == null) return "—";
  return (v * 100).toFixed(1) + "%";
}
function heatColour(score) {
  if (score >= 70) return "#ef4444";
  if (score >= 40) return "#f59e0b";
  return "#22c55e";
}

async function fetchJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(r.status + " " + r.statusText);
  return r.json();
}

function renderKPI(m) {
  document.getElementById("kpi-visitors").textContent   = m.unique_visitors ?? "—";
  document.getElementById("kpi-conversion").textContent = fmtPct(m.conversion_rate);
  document.getElementById("kpi-dwell").textContent      = fmtMs(m.avg_dwell_ms);
  const ar = m.billing_abandonment_rate;
  const aEl = document.getElementById("kpi-abandon");
  aEl.textContent = fmtPct(ar);
  aEl.className = "kpi-value " + (ar != null && ar > 0.5 ? "color-red" : "color-yellow");
}

function renderFunnel(f) {
  const stages = f.stages || [];
  if (!stages.length) { document.getElementById("funnel-body").innerHTML = '<p class="text-secondary small">No data yet</p>'; return; }
  const colours = ["#7c3aed","#3b82f6","#06b6d4","#10b981"];
  let html = "";
  stages.forEach((s, i) => {
    const pct = s.pct_of_entry ?? 0;
    const col = colours[i % colours.length];
    html += `<div class="funnel-row">
      <div class="funnel-label">
        <span>${s.stage.replace(/_/g," ")}</span>
        <span style="color:${col};font-weight:600">${s.visitors} <span style="color:#6b7280;font-weight:400">(${pct.toFixed(0)}%)</span></span>
      </div>
      <div class="funnel-bar-track">
        <div class="funnel-bar-fill" style="width:${pct}%;background:${col}"></div>
      </div>
    </div>`;
  });
  document.getElementById("funnel-body").innerHTML = html;
}

function renderHeatmap(h) {
  const zones = h.zones || [];
  const conf  = h.data_confidence || "low";
  const badge = document.getElementById("confidence-badge");
  badge.textContent = "confidence: " + conf;
  badge.className = "confidence-badge " + (conf === "high" ? "confidence-high" : "confidence-low");
  badge.classList.remove("d-none");

  if (!zones.length) { document.getElementById("heatmap-body").innerHTML = '<p class="text-secondary small">No data yet</p>'; return; }
  let html = "";
  zones.forEach(z => {
    const score = z.heat_score ?? 0;
    const col   = heatColour(score);
    const dwell = fmtMs(z.avg_dwell_ms);
    html += `<div class="heat-row">
      <div class="heat-label">
        <span>${z.zone_id.replace("zone_","")}</span>
        <span style="color:${col};font-weight:600">${score.toFixed(0)} <span style="color:#6b7280;font-weight:400">&bull; ${dwell}</span></span>
      </div>
      <div class="heat-bar-track">
        <div class="heat-bar-fill" style="width:${score}%;background:${col}"></div>
      </div>
    </div>`;
  });
  document.getElementById("heatmap-body").innerHTML = html;
}

function renderAnomalies(a) {
  const list = a.anomalies || [];
  if (!list.length) {
    document.getElementById("anomaly-body").innerHTML = '<div class="no-anomaly">&#10003;&nbsp; No anomalies detected</div>';
    return;
  }
  let html = "";
  list.forEach(x => {
    const sev = (x.severity || "INFO").toLowerCase();
    const sevColour = sev === "critical" ? "#ef4444" : sev === "warn" ? "#f59e0b" : "#3b82f6";
    const action = x.suggested_action ? `<div class="anomaly-action">&#8618; ${x.suggested_action}</div>` : "";
    html += `<div class="anomaly-item anomaly-${sev}">
      <div class="anomaly-type" style="color:${sevColour}">${x.severity} &bull; ${x.type}</div>
      <div class="anomaly-msg">${x.message}</div>
      ${action}
    </div>`;
  });
  document.getElementById("anomaly-body").innerHTML = html;
}

async function refresh() {
  const spinner = document.getElementById("spinner");
  spinner.classList.remove("d-none");
  try {
    const [m, f, h, a, health] = await Promise.all([
      fetchJSON(`/stores/${STORE_ID}/metrics`),
      fetchJSON(`/stores/${STORE_ID}/funnel`),
      fetchJSON(`/stores/${STORE_ID}/heatmap`),
      fetchJSON(`/stores/${STORE_ID}/anomalies`),
      fetchJSON("/health"),
    ]);

    renderKPI(m);
    renderFunnel(f);
    renderHeatmap(h);
    renderAnomalies(a);

    const dot = document.getElementById("status-dot");
    const txt = document.getElementById("status-text");
    const ok  = health.status === "ok";
    dot.className = "status-dot " + (ok ? "ok" : "bad");
    txt.textContent = ok ? "live" : "degraded";

  } catch(e) {
    document.getElementById("status-dot").className = "status-dot bad";
    document.getElementById("status-text").textContent = "API error";
  } finally {
    spinner.classList.add("d-none");
    const now = new Date();
    document.getElementById("last-updated").textContent =
      "updated " + now.toTimeString().slice(0,8);
  }
}

refresh();

// Real-time updates via Server-Sent Events — no polling interval needed
const evtSource = new EventSource(`/stores/${STORE_ID}/stream`);
evtSource.onmessage = (e) => {
  const payload = JSON.parse(e.data);
  if (payload.new_events > 0) {
    refresh();
  }
};
evtSource.onerror = () => {
  console.warn("SSE stream closed, reconnecting in 5s…");
  setTimeout(() => location.reload(), 5000);
};
</script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    """Live web dashboard — auto-refreshing analytics UI."""
    store_id = get_settings().store_id
    html = _HTML.replace("STORE_ID_PLACEHOLDER", store_id)
    return HTMLResponse(content=html)
