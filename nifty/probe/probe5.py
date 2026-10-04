import json, csv
j=json.load(open("nse_getIndicesData.json",encoding="utf-8"))["data"]["data"]
stk=[d for d in j if d["priority"]==0]
tot=sum(d["ffmc"] for d in stk)
sec={r["Symbol"]:r["Industry"] for r in csv.DictReader(open("ind_nifty50list.csv",encoding="utf-8"))}
print(len(stk), "missing from csv:", [d["symbol"] for d in stk if d["symbol"] not in sec], "csv not in nse:", set(sec)-{d["symbol"] for d in stk})
rows=sorted(((d["ffmc"]/tot*100,d["symbol"],sec.get(d["symbol"])) for d in stk),reverse=True)
for w,s,se in rows: print(f"{s:12s} {w:6.2f}  {se}")
agg={}
for w,s,se in rows: agg[se]=agg.get(se,0)+w
print()
for k,v in sorted(agg.items(),key=lambda x:-x[1]): print(f"{v:6.2f} {k}")
# attribution check: sum w*pChange vs index pChange
idx=[d for d in j if d["priority"]==1][0]
print("index pChange",idx["pChange"], "sum w*p (eod weights)", sum(d["ffmc"]/tot*d["pChange"] for d in stk))
# weights at prev close
prevff={d["symbol"]:d["ffmc"]*d["previousClose"]/d["lastPrice"] for d in stk}
pt=sum(prevff.values())
print("sum prevw*p", sum(prevff[d["symbol"]]/pt*d["pChange"] for d in stk))
print("keys", list(j[1].keys()))
