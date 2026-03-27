"""
viewer.py — A/B test comparison viewer for sniff

Reads comparisons/*.jsonl and serves a local web UI.
Run with: uv run viewer.py
"""

import json
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

COMP_DIR = Path(os.getenv("SNIFF_COMP_DIR", "./comparisons"))
PORT = int(os.getenv("VIEWER_PORT", "8081"))

app = FastAPI(title="sniff viewer", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def read_pairs(name: str) -> list[dict]:
    path = COMP_DIR / f"{name}.jsonl"
    if not path.exists():
        return []
    pairs = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    pairs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return pairs


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/experiments")
def list_experiments() -> list[dict]:
    if not COMP_DIR.exists():
        return []
    results = []
    for jsonl_file in sorted(COMP_DIR.glob("*.jsonl")):
        name = jsonl_file.stem
        pairs = read_pairs(name)
        if not pairs:
            continue
        n = len(pairs)
        tok_d = [p["delta"].get("output_tokens", 0) for p in pairs]
        lat_d = [(p["shadow"].get("duration_ms", 0) - p["primary"].get("duration_ms", 0)) for p in pairs]
        results.append({
            "name": name,
            "calls": n,
            "avg_tok_delta": round(sum(tok_d) / n, 1),
            "avg_lat_delta_ms": round(sum(lat_d) / n, 1),
            "latest_ts": max((p.get("ts", "") for p in pairs), default=""),
        })
    return results


@app.get("/api/experiments/{name}/pairs")
def list_pairs(name: str) -> list[dict]:
    pairs = read_pairs(name)
    if not pairs:
        raise HTTPException(404, f"Experiment '{name}' not found")
    return sorted(pairs, key=lambda p: p.get("ts", ""), reverse=True)


@app.get("/api/experiments/{name}/pairs/{ab_group_id}")
def get_pair(name: str, ab_group_id: str) -> dict:
    for p in read_pairs(name):
        if p.get("ab_group_id") == ab_group_id:
            return p
    raise HTTPException(404, f"Pair '{ab_group_id}' not found in '{name}'")


@app.get("/{path:path}")
def serve_ui(path: str = "") -> HTMLResponse:
    return HTMLResponse(HTML)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    print(f"[viewer] http://localhost:{PORT}", flush=True)
    print(f"[viewer] reading from: {COMP_DIR.resolve()}", flush=True)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>sniff viewer</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #f1f5f9;
  color: #0f172a;
  font-size: 14px;
  line-height: 1.5;
}

header {
  background: #0f172a;
  color: #f8fafc;
  padding: 0 24px;
  height: 48px;
  display: flex;
  align-items: center;
  gap: 10px;
  position: sticky;
  top: 0;
  z-index: 10;
}
.logo { font-weight: 700; font-size: 15px; letter-spacing: -0.3px; }
.sep  { color: #334155; }
.crumb { color: #94a3b8; font-size: 13px; cursor: pointer; transition: color .15s; }
.crumb:hover  { color: #f8fafc; }
.crumb.active { color: #f8fafc; cursor: default; pointer-events: none; }

main { max-width: 1160px; margin: 0 auto; padding: 28px 24px; }

h2 { font-size: 18px; font-weight: 600; margin-bottom: 16px; }

.card { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }

table { width: 100%; border-collapse: collapse; }
th {
  background: #f8fafc;
  padding: 9px 16px;
  text-align: left;
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: #64748b;
  border-bottom: 1px solid #e2e8f0;
  white-space: nowrap;
}
td { padding: 11px 16px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tr.clickable { cursor: pointer; }
tr.clickable:hover td { background: #f8fafc; }

.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  font-family: monospace;
}
.b-blue   { background: #dbeafe; color: #1d4ed8; }
.b-purple { background: #ede9fe; color: #6d28d9; }
.b-gray   { background: #f1f5f9; color: #475569; }

.pos { color: #16a34a; font-weight: 600; }
.neg { color: #dc2626; font-weight: 600; }
.neu { color: #94a3b8; }

.stat-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  margin-bottom: 20px;
  overflow: hidden;
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 14px 20px;
  border-right: 1px solid #f1f5f9;
  min-width: 120px;
}
.stat:last-child { border-right: none; }
.stat-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: #94a3b8; }
.stat-value { font-size: 20px; font-weight: 700; }
.stat-value.good { color: #16a34a; }
.stat-value.bad  { color: #dc2626; }
.stat-value.sm   { font-size: 13px; }

.pair-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 20px;
}
.variant-card { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }
.variant-header {
  padding: 12px 16px;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.variant-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; }
.variant-label.ctrl  { color: #1d4ed8; }
.variant-label.treat { color: #7c3aed; }
.variant-model { font-size: 12px; color: #64748b; font-family: monospace; }
.variant-stats {
  display: flex;
  gap: 20px;
  padding: 10px 16px;
  font-size: 12px;
  color: #475569;
  border-bottom: 1px solid #f1f5f9;
}
.variant-stats span strong { color: #0f172a; }

.tool-chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 16px; }
.chip {
  padding: 3px 10px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  font-family: monospace;
  background: #f1f5f9;
  color: #475569;
  border: 1px solid #e2e8f0;
}
.chip.ctrl-only  { background: #dbeafe; color: #1d4ed8; border-color: #bfdbfe; }
.chip.treat-only { background: #ede9fe; color: #6d28d9; border-color: #ddd6fe; }

.diff-wrap { background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; margin-bottom: 20px; }
.diff-header {
  display: grid;
  grid-template-columns: 1fr 1fr;
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
}
.diff-col-head {
  padding: 8px 16px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: #64748b;
}
.diff-col-head:first-child { border-right: 1px solid #e2e8f0; }
.diff-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  max-height: 520px;
  overflow-y: auto;
}
.diff-side {
  padding: 14px 16px;
  font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
  font-size: 12px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
}
.diff-side:first-child { border-right: 1px solid #e2e8f0; }
.dl { display: block; border-radius: 2px; padding: 0 3px; margin: 0 -3px; }
.dl-rem { background: #fee2e2; color: #991b1b; }
.dl-add { background: #dcfce7; color: #166534; }

.section-head {
  padding: 9px 16px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: #64748b;
  border-bottom: 1px solid #e2e8f0;
  background: #f8fafc;
}
.legend { font-weight: 400; margin-left: 10px; text-transform: none; letter-spacing: 0; }

.empty { padding: 48px 24px; text-align: center; color: #94a3b8; }

.page { display: none; }
.page.active { display: block; }
</style>
</head>
<body>

<header>
  <span class="logo">sniff</span>
  <span class="sep">/</span>
  <span id="c-exp" class="crumb active" onclick="gotoExperiments()">experiments</span>
  <span id="c-pairs-sep" class="sep" style="display:none">/</span>
  <span id="c-pairs" class="crumb" style="display:none"></span>
  <span id="c-detail-sep" class="sep" style="display:none">/</span>
  <span id="c-detail" class="crumb" style="display:none"></span>
</header>

<main>
  <div id="page-experiments" class="page active">
    <h2>Experiments</h2>
    <div class="card">
      <table>
        <thead><tr>
          <th>Experiment</th><th>Calls</th>
          <th>Avg output tokens Δ</th><th>Avg latency Δ</th><th>Latest</th>
        </tr></thead>
        <tbody id="exp-body"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div id="page-pairs" class="page">
    <h2 id="pairs-title"></h2>
    <div id="pairs-statbar" class="stat-bar" style="display:none"></div>
    <div class="card">
      <table>
        <thead><tr>
          <th>Time</th><th>Control</th><th>Treatment</th>
          <th>Tokens Δ</th><th>Latency Δ</th><th>Tool calls Δ</th>
        </tr></thead>
        <tbody id="pairs-body"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div id="page-detail" class="page">
    <div id="detail-content"></div>
  </div>
</main>

<script>
// ── Nav ───────────────────────────────────────────────────────────────────────

let _exp = null;

function gotoExperiments() {
  show('experiments');
  crumbs([{id:'exp', label:'experiments', active:true}]);
  loadExperiments();
}

function gotoPairs(name) {
  _exp = name;
  show('pairs');
  document.getElementById('pairs-title').textContent = name;
  crumbs([
    {id:'exp',   label:'experiments', click: gotoExperiments},
    {id:'pairs', label: name, active: true},
  ]);
  loadPairs(name);
}

function gotoDetail(name, gid) {
  show('detail');
  crumbs([
    {id:'exp',    label:'experiments', click: gotoExperiments},
    {id:'pairs',  label: name,         click: () => gotoPairs(name)},
    {id:'detail', label: gid.slice(0,8), active: true},
  ]);
  loadDetail(name, gid);
}

function show(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
}

function crumbs(list) {
  ['exp','pairs','detail'].forEach(id => {
    const el = document.getElementById('c-' + id);
    const sep = document.getElementById('c-' + id + '-sep');
    if (id !== 'exp') { el.style.display = 'none'; if (sep) sep.style.display = 'none'; }
    el.className = 'crumb';
    el.onclick = null;
  });
  list.forEach((c, i) => {
    const el = document.getElementById('c-' + c.id);
    const sep = document.getElementById('c-' + c.id + '-sep');
    if (i > 0) { el.style.display = ''; if (sep) sep.style.display = ''; }
    el.textContent = c.label;
    el.className = 'crumb' + (c.active ? ' active' : '');
    if (c.click) el.onclick = c.click;
  });
}

// ── API ───────────────────────────────────────────────────────────────────────

async function api(path) {
  const r = await fetch('/api' + path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function loadExperiments() {
  const tb = document.getElementById('exp-body');
  tb.innerHTML = '<tr><td colspan="5" class="empty">Loading…</td></tr>';
  try {
    const data = await api('/experiments');
    if (!data.length) {
      tb.innerHTML = '<tr><td colspan="5" class="empty">No experiments yet. Enable one in ab.toml and run Claude Code.</td></tr>';
      return;
    }
    tb.innerHTML = data.map(e => `
      <tr class="clickable" onclick="gotoPairs('${e.name}')">
        <td><strong>${esc(e.name)}</strong></td>
        <td>${e.calls}</td>
        <td>${dc(e.avg_tok_delta, true)}</td>
        <td>${dc(e.avg_lat_delta_ms, true, 'ms')}</td>
        <td class="neu" style="font-size:12px">${ts(e.latest_ts)}</td>
      </tr>`).join('');
  } catch(e) { tb.innerHTML = `<tr><td colspan="5" class="empty">Error: ${esc(e.message)}</td></tr>`; }
}

async function loadPairs(name) {
  const tb = document.getElementById('pairs-body');
  const sb = document.getElementById('pairs-statbar');
  tb.innerHTML = '<tr><td colspan="6" class="empty">Loading…</td></tr>';
  sb.style.display = 'none';
  try {
    const data = await api(`/experiments/${enc(name)}/pairs`);
    if (!data.length) { tb.innerHTML = '<tr><td colspan="6" class="empty">No pairs yet.</td></tr>'; return; }

    const n = data.length;
    const avgTok = data.reduce((s,p) => s + (p.delta?.output_tokens ?? 0), 0) / n;
    const avgLat = data.reduce((s,p) => s + ((p.shadow?.duration_ms ?? 0) - (p.primary?.duration_ms ?? 0)), 0) / n;
    sb.style.display = 'flex';
    sb.innerHTML = `
      <div class="stat"><span class="stat-label">Pairs</span><span class="stat-value">${n}</span></div>
      <div class="stat"><span class="stat-label">Avg output tokens Δ</span><span class="stat-value ${avgTok<0?'good':avgTok>0?'bad':''}">${fmt(avgTok)}</span></div>
      <div class="stat"><span class="stat-label">Avg latency Δ</span><span class="stat-value ${avgLat<0?'good':avgLat>0?'bad':''}">${fmt(avgLat,'ms')}</span></div>
      <div class="stat"><span class="stat-label">Control</span><span class="stat-value sm">${esc(data[0]?.primary?.model??'?')}</span></div>
      <div class="stat"><span class="stat-label">Treatment</span><span class="stat-value sm">${esc(data[0]?.shadow?.model??'?')}</span></div>`;

    tb.innerHTML = data.map(p => `
      <tr class="clickable" onclick="gotoDetail('${esc(name)}','${p.ab_group_id}')">
        <td class="neu" style="font-size:12px">${ts(p.ts)}</td>
        <td><span class="badge b-blue">${esc(p.primary?.model??'?')}</span></td>
        <td><span class="badge b-purple">${esc(p.shadow?.model??'?')}</span></td>
        <td>${dc(p.delta?.output_tokens, true)}</td>
        <td>${dc((p.shadow?.duration_ms??0)-(p.primary?.duration_ms??0), true, 'ms')}</td>
        <td>${dc(p.delta?.tool_call_count, true)}</td>
      </tr>`).join('');
  } catch(e) { tb.innerHTML = `<tr><td colspan="6" class="empty">Error: ${esc(e.message)}</td></tr>`; }
}

async function loadDetail(name, gid) {
  const el = document.getElementById('detail-content');
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const p = await api(`/experiments/${enc(name)}/pairs/${gid}`);
    el.innerHTML = renderDetail(p);
  } catch(e) { el.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`; }
}

// ── Detail render ─────────────────────────────────────────────────────────────

function renderDetail(p) {
  const cTools = p.primary?.tool_calls ?? [];
  const tTools = p.shadow?.tool_calls  ?? [];
  const cSet   = new Set(cTools);
  const tSet   = new Set(tTools);
  const allTools = [...new Set([...cTools, ...tTools])];

  const toolSection = allTools.length ? `
    <div class="card" style="margin-bottom:20px">
      <div class="section-head">Tool calls
        <span class="legend">
          <span style="color:#1d4ed8">■</span> control only &nbsp;
          <span style="color:#6d28d9">■</span> treatment only &nbsp;
          <span style="color:#64748b">■</span> both
        </span>
      </div>
      <div class="tool-chips">
        ${allTools.map(t => {
          const inC = cSet.has(t), inT = tSet.has(t);
          const cls = inC && !inT ? 'chip ctrl-only' : !inC && inT ? 'chip treat-only' : 'chip';
          return `<span class="${cls}">${esc(t)}</span>`;
        }).join('')}
      </div>
    </div>` : '';

  const [leftHtml, rightHtml] = buildDiff(p.primary?.response_text ?? '', p.shadow?.response_text ?? '');

  return `
    <div class="pair-grid">
      ${variantCard('ctrl', 'Control', p.primary)}
      ${variantCard('treat', 'Treatment', p.shadow)}
    </div>
    ${toolSection}
    <div class="diff-wrap">
      <div class="diff-header">
        <div class="diff-col-head">Control response</div>
        <div class="diff-col-head">Treatment response</div>
      </div>
      <div class="diff-body">
        <div class="diff-side">${leftHtml}</div>
        <div class="diff-side">${rightHtml}</div>
      </div>
    </div>`;
}

function variantCard(cls, label, v) {
  const tools = v?.tool_calls ?? [];
  return `
    <div class="variant-card">
      <div class="variant-header">
        <span class="variant-label ${cls}">${label}</span>
        <span class="variant-model">${esc(v?.model??'?')}</span>
      </div>
      <div class="variant-stats">
        <span><strong>${v?.input_tokens??0}</strong> in</span>
        <span><strong>${v?.output_tokens??0}</strong> out</span>
        <span><strong>${Math.round(v?.duration_ms??0)}</strong> ms</span>
        <span><strong>${esc(v?.stop_reason??'?')}</strong></span>
      </div>
      ${tools.length ? `<div class="tool-chips">${tools.map(t=>`<span class="chip">${esc(t)}</span>`).join('')}</div>` : ''}
    </div>`;
}

// ── Diff ──────────────────────────────────────────────────────────────────────

function buildDiff(a, b) {
  if (!a && !b) return ['<span class="neu">(no text response)</span>', '<span class="neu">(no text response)</span>'];
  if (a === b)  return [esc(a), esc(b)];

  const la = a.split('\n'), lb = b.split('\n');
  // Skip LCS for huge texts — just show both
  if (la.length * lb.length > 60000) return [esc(a), esc(b)];

  // LCS DP
  const m = la.length, n = lb.length;
  const dp = Array.from({length: m+1}, () => new Int32Array(n+1));
  for (let i = m-1; i >= 0; i--)
    for (let j = n-1; j >= 0; j--)
      dp[i][j] = la[i] === lb[j] ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j], dp[i][j+1]);

  const L = [], R = [];
  let i = 0, j = 0;
  while (i < m || j < n) {
    if (i < m && j < n && la[i] === lb[j]) {
      L.push(`<span class="dl">${esc(la[i])}</span>`);
      R.push(`<span class="dl">${esc(lb[j])}</span>`);
      i++; j++;
    } else if (j < n && (i >= m || dp[i][j+1] >= dp[i+1][j])) {
      L.push(`<span class="dl">&nbsp;</span>`);
      R.push(`<span class="dl dl-add">${esc(lb[j])}</span>`);
      j++;
    } else {
      L.push(`<span class="dl dl-rem">${esc(la[i])}</span>`);
      R.push(`<span class="dl">&nbsp;</span>`);
      i++;
    }
  }
  return [L.join('\n'), R.join('\n')];
}

// ── Utils ─────────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function enc(s) { return encodeURIComponent(s); }

function fmt(v, sfx='') {
  const n = Math.round(v ?? 0);
  return (n > 0 ? '+' : '') + n + sfx;
}

function dc(v, lowerBetter=true, sfx='') {
  if (v == null) return '<span class="neu">—</span>';
  const n = Math.round(v);
  const cls = (lowerBetter ? n<0 : n>0) ? 'pos' : (lowerBetter ? n>0 : n<0) ? 'neg' : 'neu';
  return `<span class="${cls}">${n>0?'+':''}${n}${sfx}</span>`;
}

function ts(s) {
  if (!s) return '—';
  try { return new Date(s).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
  catch { return s; }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
gotoExperiments();
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run("viewer:app", host="0.0.0.0", port=PORT, log_level="warning")
