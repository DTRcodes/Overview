import requests, json
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36","Accept":"*/*","Accept-Language":"en-US,en;q=0.9"}
s=requests.Session(); s.headers.update(UA)
# 1 niftyindices constituent csv
r=s.get("https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",timeout=30)
print("csv",r.status_code,len(r.content)); print(r.text[:600])
# 2 NSE equity-stockIndices
r=s.get("https://www.nseindia.com/all-reports",timeout=30); print("cookie",r.status_code)
r=s.get("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050",timeout=30,headers={"Referer":"https://www.nseindia.com/market-data/live-equity-market"})
print("nse",r.status_code,len(r.content))
if r.ok:
    j=r.json(); json.dump(j,open("nse_n50.json","w",encoding="utf-8"),indent=1)
    print(list(j.keys())); d=j["data"]; print(len(d)); print(json.dumps(d[0],indent=1)[:1500]); print(json.dumps(d[1],indent=1)[:2500])
