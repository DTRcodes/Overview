"""Independent reference for the dashboard maths - used to check the page's numbers."""
import json, sys
d = json.load(open("site/data/nifty50.json", encoding="utf-8"))
it = json.load(open("site/data/intraday.json", encoding="utf-8"))
S, I = d["stocks"], d["index"]
def bases(basis, hh=9, mm=15):
    if basis in ("Intraday 3PM", "Custom"):
        k = 345 if basis == "Intraday 3PM" else max(0, hh*60+mm-555)
        g = lambda s: (it["series"].get(s) or [])[k] if k < len(it["series"].get(s) or []) else None
        return {s["sym"]: g(s["sym"]) for s in S}, g("__INDEX__")
    key = {"Daily %chg": "prevClose", "Intraday %chg": "open", "Weekly %chg": "pwc", "Monthly %chg": "pmc"}[basis]
    return {s["sym"]: s[key] for s in S}, I[key]
def model(basis, **kw):
    b, B = bases(basis, **kw)
    tot = sum(s["ffmc"] for s in S)
    v = {s["sym"]: s["ffmc"]*b[s["sym"]]/s["last"] for s in S if b[s["sym"]]}
    V = sum(v.values())
    st = {}
    for s in S:
        x = s["sym"]; pct = (s["last"]/b[x]-1)*100 if b[x] else None
        bw = v[x]/V*100 if x in v else None
        st[x] = dict(sector=s["sector"], liveW=s["ffmc"]/tot*100, baseW=bw, pct=pct,
                     pts=B*bw/100*pct/100 if pct is not None else None)
    sec = {}
    for x, r in st.items():
        z = sec.setdefault(r["sector"], dict(liveW=0, baseW=0, wp=0, pts=0))
        z["liveW"] += r["liveW"]
        if r["pct"] is not None: z["baseW"] += r["baseW"]; z["wp"] += r["baseW"]*r["pct"]; z["pts"] += r["pts"]
    for z in sec.values(): z["pct"] = z["wp"]/z["baseW"] if z["baseW"] else None
    pos = sum(z["baseW"]/100*z["pct"] for z in sec.values() if z["pct"] and z["pct"] > 0)
    neg = sum(z["baseW"]/100*-z["pct"] for z in sec.values() if z["pct"] and z["pct"] < 0)
    return dict(B=B, idxPct=(I["last"]/B-1)*100, idxPts=I["last"]-B, st=st, sec=sec, pos=pos, neg=neg,
                sent=pos/(pos+neg)*100 if pos+neg else 50)
if __name__ == "__main__":
    for basis in ["Daily %chg", "Intraday %chg", "Intraday 3PM", "Weekly %chg", "Monthly %chg"]:
        m = model(basis)
        sp = sum(r["pts"] for r in m["st"].values() if r["pts"] is not None)
        print(f"{basis:15s} idx {m['idxPct']:+.3f}% {m['idxPts']:+8.2f}pt | Σstock pts {sp:+8.2f} | pos {m['pos']:.3f} neg {m['neg']:.3f} net {m['pos']-m['neg']:+.3f} sent {m['sent']:.1f}")
    m = model("Daily %chg")
    for k in ["HDFCBANK", "RELIANCE", "BSE"]: print(k, {a: round(b, 3) if isinstance(b, float) else b for a, b in m["st"][k].items()})
    print({k: (round(v["liveW"], 2), round(v["pct"], 3), round(v["pts"], 2)) for k, v in sorted(m["sec"].items(), key=lambda x: -x[1]["liveW"])})
