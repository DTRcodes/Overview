import requests, json
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36","Accept":"application/json, text/plain, */*","Accept-Language":"en-US,en;q=0.9","Referer":"https://www.nseindia.com/get-quotes/equity?symbol=HDFCBANK"}
s=requests.Session(); s.headers.update(UA)
s.get("https://www.nseindia.com/market-data/live-equity-market",timeout=30)
base="https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi?functionName=getSymbolgraphData&&identifier={}&flag={}"
for ident in ["HDFCBANKEQN","HDFCBANK","HDFCBANK%20EQ"]:
    for fl in ["1D","1"]:
        r=s.get(base.format(ident,fl),timeout=30); print(ident,fl,r.status_code,r.text[:200])
for u in ["https://www.nseindia.com/api/chart-databyindex?index=HDFCBANKEQN","https://www.nseindia.com/api/chart-databyindex?index=HDFCBANKEQN&preopen=false"]:
    r=s.get(u,timeout=30); print(u,r.status_code,r.text[:300])
