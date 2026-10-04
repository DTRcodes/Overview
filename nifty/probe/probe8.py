import requests, json, re, pypdf
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36","Accept":"application/json, text/plain, */*","Accept-Language":"en-US,en;q=0.9","Referer":"https://www.nseindia.com/market-data/live-equity-market"}
s=requests.Session(); s.headers.update(UA)
s.get("https://www.nseindia.com/market-data/live-equity-market",timeout=30)
for u in ["https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi?functionName=getSymbolgraphData&&identifier=HDFCBANKEQN&flag=1",
          "https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi?functionName=getIndexChart&&identifier=NIFTY%2050&flag=1D"]:
    r=s.get(u,timeout=30); print(r.status_code,len(r.content)); t=r.text; print(t[:700]); 
    try:
        j=r.json(); json.dump(j,open(u.split("functionName=")[1][:20]+".json","w"),indent=0)
    except Exception as e: print(e)
# PDF parse
txt="\n".join(p.extract_text() for p in pypdf.PdfReader("indices_dataSep2026/NIFTY_50_Sep2026.pdf").pages)
open("pdf_sep.txt","w",encoding="utf-8").write(txt)
