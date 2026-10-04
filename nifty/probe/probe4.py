import requests, json
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36","Accept":"application/json, text/plain, */*","Accept-Language":"en-US,en;q=0.9","Referer":"https://www.nseindia.com/market-data/live-equity-market"}
s=requests.Session(); s.headers.update(UA)
s.get("https://www.nseindia.com/market-data/live-equity-market",timeout=30)
r=s.get("https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi?functionName=getIndicesData&symbol=NIFTY%2050",timeout=30)
print(r.status_code,len(r.content))
j=r.json(); json.dump(j,open("nse_getIndicesData.json","w",encoding="utf-8"),indent=1)
print(type(j), list(j.keys()) if isinstance(j,dict) else len(j))
txt=json.dumps(j)[:3000]; print(txt)
