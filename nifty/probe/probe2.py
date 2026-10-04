import requests, json
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36","Accept":"application/json, text/plain, */*","Accept-Language":"en-US,en;q=0.9"}
s=requests.Session(); s.headers.update(UA)
r=s.get("https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",timeout=30)
open("ind_nifty50list.csv","w",encoding="utf-8").write(r.text)
print(r.text)
for u in ["https://www.nseindia.com/","https://www.nseindia.com/market-data/live-equity-market"]:
    print(u, s.get(u,timeout=30).status_code)
for u in ["https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050",
          "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY+50",
          "https://www.nseindia.com/api/allIndices",
          "https://www.nseindia.com/api/marketStatus"]:
    r=s.get(u,timeout=30); print(u,r.status_code,len(r.content),r.text[:200])
