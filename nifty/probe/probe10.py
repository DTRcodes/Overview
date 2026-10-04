import json, re, datetime as dt
txt=open("pdf_sep.txt",encoding="utf-8").read()
# rows: SYMBOL ... close weight+mcap ; weight has 2 decimals
pdfw={}
syms=[d["symbol"] for d in json.load(open("nse_getIndicesData.json"))["data"]["data"] if d["priority"]==0]
flat=re.sub(r"\s*\n\s*"," ",txt)
for sym in syms:
    m=re.search(r"(?:^|\s)"+re.escape(sym)+r"\s.*?\s(\d+\.\d{2})\s(\d+\.\d{2})(\d+)(?=\s|$)",flat)
    pdfw[sym]=(float(m.group(2)),int(m.group(3)),float(m.group(1))) if m else None
j=json.load(open("nse_getIndicesData.json"))["data"]["data"]
stk=[d for d in j if d["priority"]==0]
pf={d["symbol"]:d["ffmc"]*d["previousClose"]/d["lastPrice"] for d in stk}; T=sum(pf.values())
mx=0; bad=[]
for d in stk:
    s=d["symbol"]; w=pf[s]/T*100; p=pdfw[s]
    if p is None: bad.append(s); continue
    diff=w-p[0]; mx=max(mx,abs(diff))
    if abs(diff)>0.02: print(s, round(w,3), p, "close pdf",p[2],"prevClose",d["previousClose"])
print("max |diff|",round(mx,4),"unparsed",bad, "sum pdf w", sum(p[0] for p in pdfw.values() if p))
print("pdf mcap sum crores", sum(p[1] for p in pdfw.values() if p), "ffmc prev sum crores", T/1e7)
g=json.load(open("getIndexChart&&ident.json"))
for t in g["grapthData"][:2]+g["grapthData"][-2:]: print(t, dt.datetime.utcfromtimestamp(t[0]/1000))
