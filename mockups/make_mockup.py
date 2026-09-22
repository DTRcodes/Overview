"""Build a standalone mockup of the 6M-with-3M-emphasis treatment.

Reads the real history.json so the shapes and figures are the actual ones,
writes a single self-contained HTML file that opens from the filesystem with
no server and no fetch. This is a DESIGN mockup, not the dashboard: it is
deliberately outside docs/ so it never gets published.
"""
import json
import datetime as dt
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
rows = json.loads((ROOT / "docs" / "history.json").read_text(encoding="utf-8"))["rows"]
rows = sorted(rows, key=lambda r: r["date"])

END = max(r["date"] for r in rows)
end_d = dt.date.fromisoformat(END)
CUT3 = (end_d - dt.timedelta(days=90)).isoformat()
CUT6 = (end_d - dt.timedelta(days=180)).isoformat()
FY = "2026-04-01"


def series(key, since):
    return [{"d": r["date"], "v": r[key]} for r in rows
            if r.get(key) is not None and r["date"] >= since]


data = {
    "end": END, "cut3": CUT3, "cut6": CUT6, "fy": FY,
    "pe": series("nifty_pe", CUT6),
    "fx": series("usd_inr", CUT6),
    "y10in": series("y10_in", CUT6),
    "fii": series("fii_net", FY),
    "dii": series("dii_net", FY),
}

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mockup · 6M trend with 3M emphasis</title>
<style>
:root{
  --bg:#0f1115; --surface-1:#161a21; --line:#252b36;
  --text-primary:#e8ecf3; --text-secondary:#aab4c4; --text-muted:#6f7c90;
  --up:#3fb27f; --down:#e5604d;
  --series-1:#3987e5; --series-2:#c08a3e;
  --flow-fii:#4098c9; --flow-dii:#8fbd77;
  --radius:12px;
}
@media(prefers-color-scheme:light){
 :root{--bg:#f6f7f9;--surface-1:#fff;--line:#e3e7ee;--text-primary:#141821;
   --text-secondary:#47536a;--text-muted:#77839a;--series-1:#2a78d6;
   --flow-fii:#1f5f82;--flow-dii:#7da869;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text-primary);
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  padding:22px 18px 60px;max-width:1180px;margin:0 auto}
h1{font-size:19px;margin:0 0 4px}
.sub{color:var(--text-muted);font-size:12.5px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:1fr;gap:14px}
@media(min-width:900px){.grid{grid-template-columns:1fr 1fr}}
.card{background:var(--surface-1);border:1px solid var(--line);
  border-radius:var(--radius);padding:13px 14px 10px}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start;
  gap:12px;flex-wrap:wrap}
h2{font-size:13.5px;margin:0;letter-spacing:-.01em}
.cap{color:var(--text-muted);font-size:11.5px;margin:3px 0 8px}
/* the two-window figures beside the title */
.wins{display:flex;gap:14px;align-items:baseline}
.win{text-align:right}
.win .k{font-size:9.5px;text-transform:uppercase;letter-spacing:.07em;
  color:var(--text-muted);font-weight:600}
.win .v{font-size:14.5px;font-weight:670;font-variant-numeric:tabular-nums;
  letter-spacing:-.02em}
.win.recent .k{color:var(--text-secondary)}
.up{color:var(--up)} .down{color:var(--down)}
svg{display:block;width:100%;height:auto;overflow:visible}
.grid-l{stroke:var(--line);stroke-width:1}
.zero{stroke:var(--text-muted);stroke-width:1;opacity:.55}
.axis{fill:var(--text-muted);font-size:9.5px}
.band{fill:var(--text-secondary);opacity:.05}
.bandline{stroke:var(--text-muted);stroke-dasharray:3 3;opacity:.3}
.bandlab{fill:var(--text-muted);font-size:9px;letter-spacing:.06em;
  text-transform:uppercase}
.legend{display:flex;gap:13px;flex-wrap:wrap;color:var(--text-secondary);
  font-size:11px;margin-top:7px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;
  margin-right:5px;vertical-align:-1px}
.note{color:var(--text-muted);font-size:11px;margin-top:9px;line-height:1.45}
.sec{margin:30px 0 12px;font-size:12px;text-transform:uppercase;
  letter-spacing:.08em;color:var(--text-muted);font-weight:600;
  border-top:1px solid var(--line);padding-top:16px}
</style></head><body>
<h1>Mockup — 6 months, with the last 3 at a larger x-scale</h1>
<div class="sub">Real data from the board's history, to __END__. Nothing here is
 wired to the live dashboard; this file only exists to judge the treatment.</div>

<div class="sec" style="border:0;margin-top:0;padding-top:0">Nifty 50 P/E — the same six months, three treatments</div>
<div class="grid" id="cmp" style="grid-template-columns:1fr"></div>

<div class="sec">The rest of the board at &times;1.5</div>
<div class="grid" id="optA"></div>

<div class="sec">Flows — bars, financial year, last 3 months at &times;1.5</div>
<div class="grid" id="optC" style="grid-template-columns:1fr"></div>

<script>
const D = __DATA__;
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const nf = (v,d=2) => v==null||isNaN(v) ? '–'
  : Number(v).toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d});
const sign = v => v>0?'up':v<0?'down':'';
const arrow = v => v>0?'▲':v<0?'▼':'';
const T = s => new Date(s+'T00:00:00').getTime();
const shortDate = s => new Date(s+'T00:00:00')
  .toLocaleDateString('en-GB',{day:'2-digit',month:'short'});

/* ---------- the two-window figures ---------- */
function winFigs(pts, mode){
  const f = (since) => {
    const seg = pts.filter(p => p.d >= since);
    if(!seg.length) return null;
    if(mode === 'flow') return seg.reduce((a,p)=>a+p.v,0);
    return seg[seg.length-1].v - seg[0].v;      // level: change over window
  };
  return {six: f(D.cut6), three: f(D.cut3)};
}
function figHTML(pts, mode, unit){
  const w = winFigs(pts, mode);
  const fmt = v => v==null ? '–'
    : mode==='flow' ? `${v>0?'+':''}${nf(v/1e5,2)} L Cr`
    : `${arrow(v)} ${nf(Math.abs(v), unit==='%'?2:2)}${unit||''}`;
  return `<div class="wins">
    <div class="win"><div class="k">6 months</div>
      <div class="v ${sign(w.six)}">${fmt(w.six)}</div></div>
    <div class="win recent"><div class="k">Last 3 months</div>
      <div class="v ${sign(w.three)}">${fmt(w.three)}</div></div></div>`;
}

/* ---------- line chart, optional emphasis on the recent span ---------- */
function lineCard(el, title, cap, pts, color, opts={}){
  const W = 560, H = 190, padL = 6, padR = 48, padT = 12, padB = 22;
  const iw = W-padL-padR, ih = H-padT-padB;
  const lo0 = Math.min(...pts.map(p=>p.v)), hi0 = Math.max(...pts.map(p=>p.v));
  const pad = (hi0-lo0)*0.12 || 1, lo = lo0-pad, hi = hi0+pad;
  const t0 = T(pts[0].d), t1 = T(pts[pts.length-1].d);
  const ZOOM = opts.zoom ?? 1.5, tCut = T(D.cut3);
  const split = opts.emphasise && tCut > t0;
  const dOld = split ? tCut-t0 : t1-t0, dNew = split ? t1-tCut : 0;
  const wOld = split ? iw*dOld/(dOld + ZOOM*dNew) : iw;
  const X = d => {
    const t = T(d);
    if(!split) return padL + ((t-t0)/(t1-t0||1))*iw;
    return t <= tCut ? padL + ((t-t0)/dOld)*wOld
                     : padL + wOld + ((t-tCut)/dNew)*(iw-wOld);
  };
  const Y = v => padT + ih - ((v-lo)/(hi-lo))*ih;
  const path = seg => seg.map((p,i)=>`${i?'L':'M'}${X(p.d).toFixed(1)},${Y(p.v).toFixed(1)}`).join(' ');

  // split at the 3-month cut so the recent span can be drawn solid
  const older = pts.filter(p=>p.d <= D.cut3);
  const recent = pts.filter(p=>p.d >= D.cut3);
  const xCut = padL + wOld;

  let grid='', ylab='';
  for(let i=0;i<=3;i++){
    const v = lo + (hi-lo)*i/3, y = Y(v);
    grid += `<line class="grid-l" x1="${padL}" x2="${W-padR}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>`;
    ylab += `<text class="axis" x="${W-padR+6}" y="${(y+3.5).toFixed(1)}">${nf(v, opts.dp==null?2:opts.dp)}</text>`;
  }
  let xlab='';
  const step = Math.max(1, Math.floor(pts.length/5));
  pts.forEach((p,i)=>{ if(i%step===0) xlab +=
    `<text class="axis" x="${X(p.d).toFixed(1)}" y="${H-6}" text-anchor="middle">${shortDate(p.d)}</text>`; });

  const band = opts.emphasise ? `
    <rect class="band" x="${xCut.toFixed(1)}" y="${padT}"
      width="${(padL+iw-xCut).toFixed(1)}" height="${ih}"/>
    <line class="bandline" x1="${xCut.toFixed(1)}" x2="${xCut.toFixed(1)}"
      y1="${padT}" y2="${padT+ih}"/>
    <text class="bandlab" x="${(xCut+6).toFixed(1)}" y="${padT+11}">last 3 months &times;${ZOOM}</text>` : '';

  el.insertAdjacentHTML('beforeend', `<div class="card">
    <div class="cardhead"><h2>${title}</h2>${figHTML(pts, 'level', opts.unit)}</div>
    <div class="cap">${cap}</div>
    <svg viewBox="0 0 ${W} ${H}">
      ${grid}${band}
      <path d="${path(older)}" fill="none" stroke="${color}" stroke-width="1.8"
        opacity="${opts.emphasise?0.75:1}"/>
      <path d="${path(recent)}" fill="none" stroke="${color}" stroke-width="2"/>
      ${ylab}${xlab}
    </svg></div>`);
}

/* ---------- paired bars, recent span at full colour ---------- */
function barCard(el, title, cap){
  const W = 1100, H = 260, padL = 52, padR = 12, padT = 14, padB = 30;
  const iw = W-padL-padR, ih = H-padT-padB;
  const byDate = {};
  D.fii.forEach(p => (byDate[p.d] = byDate[p.d]||{}).fii = p.v);
  D.dii.forEach(p => (byDate[p.d] = byDate[p.d]||{}).dii = p.v);
  const days = Object.keys(byDate).sort();
  const vals = days.flatMap(d => [byDate[d].fii, byDate[d].dii].filter(v=>v!=null));
  let lo = Math.min(0,...vals), hi = Math.max(0,...vals);
  const pad=(hi-lo)*0.08; lo-=pad; hi+=pad;
  const Y = v => padT + ih - ((v-lo)/(hi-lo))*ih, y0 = Y(0);
  const ZOOM = 1.5;
  const nNew = days.filter(d=>d>=D.cut3).length, nOld = days.length-nNew;
  const unit = iw/(nOld + ZOOM*nNew);
  const slotOf = d => d >= D.cut3 ? unit*ZOOM : unit;
  const cF = cssv('--flow-fii'), cD = cssv('--flow-dii');
  const xCut = padL + unit*nOld;

  let grid='', ylab='';
  const stepv = 2000;
  for(let v=Math.ceil(lo/stepv)*stepv; v<=hi; v+=stepv){
    const y=Y(v);
    grid += `<line class="grid-l" x1="${padL}" x2="${W-padR}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>`;
    ylab += `<text class="axis" x="${padL-7}" y="${(y+3.5).toFixed(1)}" text-anchor="end">${nf(v/1000,0)}k</text>`;
  }
  let bars='', xlab='', x = padL, lastLab = -99;
  days.forEach(d=>{
    const slot = slotOf(d), bw = Math.max(Math.min(slot*0.40, 11), 1.4);
    const fresh = d >= D.cut3;
    [['fii',cF],['dii',cD]].forEach(([k,col],j)=>{
      const v = byDate[d][k]; if(v==null) return;
      const y=Y(v), top=Math.min(y,y0);
      bars += `<rect x="${(x+j*(bw+0.7)).toFixed(1)}" y="${top.toFixed(1)}"
        width="${bw.toFixed(1)}" height="${Math.max(Math.abs(y-y0),0.8).toFixed(1)}"
        fill="${col}" opacity="${fresh?1:0.55}"/>`;
    });
    if(x-lastLab > 78){ xlab += `<text class="axis" x="${(x+bw).toFixed(1)}" y="${H-8}"
      text-anchor="middle">${shortDate(d)}</text>`; lastLab = x; }
    x += slot;
  });

  const sum = (pts, since) => pts.filter(p=>p.d>=since).reduce((a,p)=>a+p.v,0);
  const fig = (label, fii, dii) => `<div class="win"><div class="k">${label}</div>
    <div class="v ${sign(fii)}" style="font-size:13px">FII ${fii>0?'+':''}${nf(fii/1e5,2)}</div>
    <div class="v ${sign(dii)}" style="font-size:13px">DII ${dii>0?'+':''}${nf(dii/1e5,2)}</div></div>`;

  el.insertAdjacentHTML('beforeend', `<div class="card">
    <div class="cardhead"><h2>${title}</h2>
      <div class="wins">
        ${fig('Since 1 Apr', sum(D.fii,D.fy), sum(D.dii,D.fy))}
        ${fig('6 months', sum(D.fii,D.cut6), sum(D.dii,D.cut6))}
        ${fig('Last 3 months', sum(D.fii,D.cut3), sum(D.dii,D.cut3))}
      </div></div>
    <div class="cap">${cap}</div>
    <svg viewBox="0 0 ${W} ${H}">
      ${grid}${
        `<rect class="band" x="${xCut.toFixed(1)}" y="${padT}" width="${(padL+iw-xCut).toFixed(1)}" height="${ih}"/>
         <line class="bandline" x1="${xCut.toFixed(1)}" x2="${xCut.toFixed(1)}" y1="${padT}" y2="${padT+ih}"/>
         <text class="bandlab" x="${(xCut+6).toFixed(1)}" y="${padT+11}">last 3 months &times;${ZOOM}</text>`
      }
      <line class="zero" x1="${padL}" x2="${W-padR}" y1="${y0.toFixed(1)}" y2="${y0.toFixed(1)}"/>
      ${bars}${ylab}${xlab}
    </svg>
    <div class="legend"><span><i style="background:${cF}"></i>FII</span>
      <span><i style="background:${cD}"></i>DII</span>
      <span style="color:var(--text-muted)">₹ crore, net · last 3 months at double width</span></div>
    <div class="note">All figures in L Cr (lakh crore). The financial year holds
      the full span; the emphasised bars are the recent quarter.</div></div>`);
}

// Like for like: one series, one height, three x-scales, stacked so the
// break lands in the same place on screen and the difference is the shape.
const C = document.getElementById('cmp');
lineCard(C, 'Nifty 50 P/E · plain 6 months',
  'What the board did before — one scale throughout, every day the same width.',
  D.pe, cssv('--series-1'), {emphasise:false, dp:2});
lineCard(C, 'Nifty 50 P/E · last 3 months at ×1.5',
  'The new default. Equal 90-day spans split the width 40 / 60.',
  D.pe, cssv('--series-1'), {emphasise:true, dp:2, zoom:1.5});
lineCard(C, 'Nifty 50 P/E · last 3 months at ×2',
  'The previous proposal, for comparison — 33 / 67, the older span squeezed harder.',
  D.pe, cssv('--series-1'), {emphasise:true, dp:2, zoom:2});

const A = document.getElementById('optA');
lineCard(A, 'USD / INR', 'Traded spot — 6 months, last quarter at ×1.5.',
  D.fx, cssv('--series-2'), {emphasise:true, dp:2});
lineCard(A, 'India 10Y yield', 'FBIL par yield — 6 months, last quarter at ×1.5.',
  D.y10in, cssv('--series-1'), {emphasise:true, dp:2, unit:'%'});

barCard(document.getElementById('optC'), 'Institutional flows',
  'Daily net cash-market activity, ₹ crore — financial year to date.');
</script></body></html>
"""

out = ROOT / "mockups" / "range-emphasis.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    HTML.replace("__DATA__", json.dumps(data, separators=(",", ":")))
        .replace("__END__", END),
    encoding="utf-8")
print("wrote", out, out.stat().st_size, "bytes")
print("points: pe=%d fx=%d y10in=%d fii=%d dii=%d"
      % (len(data["pe"]), len(data["fx"]), len(data["y10in"]),
         len(data["fii"]), len(data["dii"])))
print("windows: 6M from %s, 3M from %s, FY from %s, end %s"
      % (CUT6, CUT3, FY, END))
