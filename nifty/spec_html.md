# Task: build `index.html` — NIFTY 50 Sector/Stock Allocation Dashboard (web port of a TradingView Pine indicator)

Output ONE self-contained HTML file (inline CSS + vanilla JS, no frameworks, no external
scripts or fonts). Return it in a single ```html fenced block and nothing else of substance.

Attached:
- `nifty50.json` — a REAL data file exactly as the page will receive it.
- `Main_Dashboard_50stock.txt` — the original Pine Script indicator. Use it ONLY to match the
  look/semantics (cell text format, colours, sort modes, gauge, sector-grouped layout). Where it
  conflicts with this spec, THIS SPEC WINS. Ignore everything TradingView-specific in it
  (request.security, 40-call limit, Helper/Extra-11 linking, packed decoding, table positions,
  "Shift Dashboard", plot exports).

## 1. Data loading
```js
const CFG = Object.assign({
  dataUrl: 'data/nifty50.json',
  intradayUrl: 'data/intraday.json',
  pollOpenSec: 30,       // when market.status === 'Open'
  pollClosedSec: 300
}, window.NIFTY_DASH_CONFIG || {});
```
- Fetch `dataUrl` with `cache:'no-store'`. Re-poll on the interval above (choose by `market.status`).
- `intraday.json` is fetched ONLY when basis is "Intraday 3PM" or "Custom"; re-fetch every 120 s
  while such a basis is selected. Shape:
  `{tradeDate:"2026-10-01", firstMinute:"09:15", series:{"__INDEX__":[...], "HDFCBANK":[...], ...}}`
  where `series[sym][k]` = open price of the 1-minute candle at 09:15 + k minutes
  (k = 0..375; array is shorter if that minute has not happened yet; values may be null).
- On fetch error keep showing the last good data and show a small red "stale / offline" badge.
- If `intraday.json` is missing, 3PM/Custom cells show "--".

## 2. Core maths (do it exactly like this)
For the selected basis, every stock i gets a base price `b_i`, and the index gets `B`:

| Basis (dropdown label) | stock base b_i | index base B |
|---|---|---|
| Daily %chg (DEFAULT) | `prevClose` | `index.prevClose` |
| Intraday %chg | `open` | `index.open` |
| Intraday 3PM | `series[sym][345]` (15:00) | `series.__INDEX__[345]` |
| Weekly %chg | `pwc` | `index.pwc` |
| Monthly %chg | `pmc` | `index.pmc` |
| Custom (HH, MM inputs) | `series[sym][k]`, k = HH*60+MM-555, k<0 → k=0 | same on `__INDEX__` |

A base that is missing / null / 0 / index out of range → that stock's pct is `na` (shows "--").

```
pct_i      = (last_i / b_i - 1) * 100
liveW_i    = ffmc_i / Σ_all ffmc * 100                     // displayed weight, e.g. "(10.7%)"
baseW_i    = (ffmc_i * b_i / last_i) / Σ_{stocks with valid b} (ffmc_j * b_j / last_j) * 100
                                                         // weight AT THE START of the period
idxPct     = (index.last / B - 1) * 100
idxPts     = index.last - B
pts_i      = B * baseW_i/100 * pct_i/100                  // NIFTY points this stock moved
```
(Σ pts_i ≈ idxPts exactly — this is why baseW, not liveW, is used for maths.)

Sectors are DERIVED from `stocks[].sector` (never hard-code the list; a rebalance can add a sector):
```
sectorLiveW  = Σ liveW_i            (displayed)
sectorBaseW  = Σ baseW_i over members with valid pct
sectorPct    = Σ baseW_i*pct_i / sectorBaseW    (na if no member has pct)
sectorPts    = Σ pts_i
```
Weighted change (Pine `calc_weighted_sentiment`, over SECTORS):
```
pos = Σ_{sectorPct>0} sectorBaseW/100 * sectorPct
neg = Σ_{sectorPct<0} sectorBaseW/100 * |sectorPct|
sentiment = pos+neg > 0 ? pos/(pos+neg)*100 : 50 ;  net = pos - neg
```

Sector short names (match on the sector string; fall back to the full string):
Financial Services→Finance, Oil Gas & Consumable Fuels (also "Oil, Gas & Consumable Fuels")→Oil & Gas,
Information Technology→IT, Automobile and Auto Components→Auto, Fast Moving Consumer Goods→FMCG,
Telecommunication→Telecom, Healthcare→Health, Metals & Mining→Metals, Construction→Construction,
Consumer Durables→Durables, Consumer Services→Consumer, Power→Power, Services→Services,
Construction Materials→Materials, Capital Goods→Capital, Realty→Realty, Chemicals→Chemicals,
Media Entertainment & Publication→Media, Textiles→Textiles, Diversified→Diversified,
Forest Materials→Forest, Utilities→Utilities, Unclassified→Other.

## 3. Formatting (match Pine)
- pct: 2 decimals, `"▲ 1.23%"` if >0, `"▼ -0.45%"` if <0 (keep the minus sign as Pine does), `"0.00%"` if 0, `"--"` if na.
- points (only when "Show Points" is ON): `"+40.2 pt  "` / `"-12.3 pt  "` (1 decimal, explicit +), placed before the arrow. Omit when OFF or na.
- Stock cell: line 1 `HDFCBANK (10.7%)` (liveW 1 decimal), blank line, line 3 `[pts] ▲ 1.76%`.
- Sector grid cell: `Finance (37.8%)`, blank line, `[pts] ▲ 0.42%`.
- Sector-grouped header: `✦ FINANCE SECTOR (37.8%) ✦` newline `[pts] ▲ 0.42%`.
- NIFTY header: `NIFTY50:` then `[idxPts as pts] ▲ -0.88%` coloured green if idxPct ≥ 0 else red.
  `Weighted Chg:` then `▲ {pos:.2f}%` (positive colour) and `▼ {neg:.2f}%` (negative colour).

## 4. Colours (exact Pine classifiers, thresholds on pct)
Standard bg: na #e0e0e0 | ≥1.5 #2e7d32 | ≥0.25 #4caf50 | >-0.25 #e2e8f0 | ≥-1.5 #ef9a9a | else #c62828
Standard text: na gray | ≥1.5 #d4effc | ≥0.25 white | >-0.25 #475569 | ≥-1.5 #5c0000 | else white
B&W bg: na #e0e0e0 | ≥1.5 bw5 | ≥0.25 bw4 | >-0.25 bw3 | ≥-1.5 bw2 | else bw1
  defaults bw5 #fdfaf6, bw4 #e2e8f0, bw3 #cbd5e1, bw2 #64748b, bw1 #334155
B&W text: na gray | >-0.25 black | else white
Light text (sector text in Light theme): na gray | ≥1.5 #2e7d32 | ≥0.25 #4caf50 | >-0.25 #475569 | ≥-1.5 #d32f2f | else #c62828
Theme routing (Pine f_get_bg / f_get_text_col):
- "Normal": everything standard.
- "B&W Stocks": stocks B&W; sectors standard unless "Apply B&W to sectors" is ON.
- "Light Sectors" (DEFAULT): sectors bg = lightBg setting (default #ffffff) with light text; stocks standard.

## 5. Layout
Page: title bar ("NIFTY 50 · Sector & Stock Allocation"), status line (last update `lastUpdate`
+ market status pill, stale badge), a ⚙ Settings button that opens a side drawer, then the dashboard
card, then a collapsible "Weights & data sources" section.

Dashboard card = a CSS grid of bordered cells (1px gray borders, like the Pine table), rows:
1. **Sentiment bar**: a horizontal gradient red→(transparent middle)→green spanning the card, with a
   ▼ marker positioned at `sentiment%` from the left, marker green if net ≥ 0 else red. Tooltip:
   "Up-weight share {sentiment:.0f}%".
2. **Header row**: `NIFTY50:` | value | `Weighted Chg:` | `▲ pos%` | `▼ neg%`.
   If "Weighted Chg gauge" is ON: the neg value moves to a second line, and each value gets a bar to its
   right filling `pos/(pos+neg)` and `neg/(pos+neg)` of its track (real CSS bars, track colour = gauge
   track setting, fills = gauge + / – colours, default #2962ff / red / rgba(128,128,128,.08)).
3. **Anchor banner** (only for non-Daily bases), blue #1976d2, centred:
   "⚓ Prices anchored to Today's Open" / "… the 15:00 (3 PM) candle Open" / "… the HH:MM candle Open" /
   "… the previous week's close (YYYY-MM-DD)" / "… the previous month's close (YYYY-MM-DD)"
   using `baseDates.pwc` / `baseDates.pmc`. For 3PM before 15:00 add " — available after 15:00".
4. Then the view:

### View A — "Standard Multi-Matrix" (DEFAULT)
- If "Show Sector Grid" ON: sectors in a 5-column grid (as many rows as needed), each cell
  coloured by sectorPct. Hover/tap tooltip (custom, styled, not `title=`) listing that sector's stocks,
  one per line: `NAME: [pts] ▲ 1.23%`, header `--- FINANCE STOCKS ---`, stocks ordered like the grid.
- A thin spacer row.
- If "Show Top Stocks Matrix" ON: stocks in a 5-column grid, first N stocks (N = 25/30/35/40/45/50,
  DEFAULT 50) in the current stock order. Layout "Combined With Main Table" (DEFAULT) = inside the
  same card; "Separate Standalone Table" = its own card below. Stock cells get a hover tooltip:
  company name, sector, live weight, base-period weight, official weights
  (`official.monthEndPdf`, `official.mcwb` from the stock — label them with the `official[].label`
  entries from the top-level array), and pts.

### View B — "Sector Grouped View"
Two columns of sector blocks. Each block = header cell spanning 5 cols (coloured by sectorPct) then
its stocks wrapped 5 per row; a row with k<5 stocks shares the full width equally (k cells,
each width 5/k). Place blocks greedily exactly like Pine: keep `rLeft`, `rRight` row counters
(start equal); for each sector in sector order, if rLeft ≤ rRight put it left and add
`1 + ceil(n/5)` to rLeft, else right. Header row/anchor/sentiment span the full card width.

### Sorting ("Organize Grid Order By")
- "Index Weightage" (DEFAULT): sectors by sectorLiveW desc, stocks by liveW desc.
- "Sector Performance %": sectors by sectorPct desc; stocks stay by liveW.
- "ALL Performance %": sectors by sectorPct desc AND stocks by pct desc (also inside tooltips and grouped blocks).
- "ALL Points": sectors by sectorPts desc AND stocks by pts desc.
na always sorts last. Stock-limit N is applied AFTER sorting (Pine sorts only the first N by weight;
for the web, sort all 50 then take N — simpler and better).

### Responsive
Phone width (≤ 640px): page has 16px side gutter and NO horizontal page scroll. Grids become
3 columns; grouped view becomes one column (blocks keep 5-wide internal rows but may shrink text).
Text size setting maps Tiny/Small/Normal/Large/Huge → base font 10/11/13/15/18 px for cells.

## 6. Settings drawer (persist to localStorage key `n50dash.v1`; wrap every storage access in try/catch)
Display: View Mode (Standard Multi-Matrix | Sector Grouped View) · Color Theme (Normal | B&W Stocks |
Light Sectors) · Light theme sector BG (color input) · Text Size · Show Sector Grid · Show Points
Contribution · Use Weighted Chg Gauge · Gauge colours (+, –, track)
Stocks: Show Top Stocks Matrix · Layout Style · How many stocks
Sorting: Organize Grid Order By
Timeframe: % Change Basis + HH (0–23, def 9) + MM (0–59, def 15) shown only for Custom
B&W colours: 5 colour inputs + "Apply B&W colours to sectors as well"
A "Reset to defaults" button. Every change re-renders instantly without refetching (except fetching
intraday.json the first time a 3PM/Custom basis is chosen).

## 7. "Weights & data sources" section (collapsed by default)
- Line: "Weights: {weights.method}" and "Constituents: {constituents.source}, fetched {constituents.fetched}, {count} stocks".
- **Constituent changes**: for each `constituents.changes[]`: "{date} ({source}): + ADDED … · − REMOVED …"
  green/red chips. If empty: "No change since last official list".
- **Base dates**: previous week close {baseDates.pwc}, previous month close {baseDates.pmc};
  list `baseAdjustments` if any ("SYM split/bonus ×mult ex {exDate}").
- **Weights table** of all stocks: Symbol · Sector · Live wt% (2dp) · each official column from
  `stocks[].official` (header = matching `official[].label`) · Δ live − first official (2dp, signed).
  Sortable by clicking headers. Sector subtotal rows optional.
- A "Download CSV" button exporting that table (Blob + anchor download, filename
  `nifty50_weights_{tradeDate}.csv`).

## 8. Theme / quality
- Page chrome supports light and dark (`prefers-color-scheme`), colours as CSS custom properties on
  `:root`; `body` has an explicit background. Dashboard cell colours are the fixed Pine colours above
  in both themes; header-row text that Pine draws black must use the theme foreground colour instead.
- Numbers in cells use `font-variant-numeric: tabular-nums`. Cells are centred, multi-line
  (`white-space: pre-line`).
- Tooltips: one shared absolutely-positioned div, follows the hovered cell, also opens on tap;
  stays inside the viewport.
- No console errors with the attached JSON. Handle `market` being null.
- Keep code readable: small pure functions `computeModel(data, intraday, settings)` → model, and
  `render(model, settings)`. Comment the maths with the formulas above.
