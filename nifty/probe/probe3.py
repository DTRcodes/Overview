import requests, re
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36","Accept-Language":"en-US,en;q=0.9"}
s=requests.Session(); s.headers.update(UA)
h=s.get("https://www.nseindia.com/market-data/live-equity-market",timeout=30).text
print(len(h))
print(sorted(set(re.findall(r'/api/[A-Za-z0-9_\-/]+',h)))[:80])
js=sorted(set(re.findall(r'src="([^"]+\.js[^"]*)"',h)))
print(js)
found=set()
for j in js:
    u=j if j.startswith("http") else "https://www.nseindia.com"+j
    try:
        t=s.get(u,timeout=30).text
    except Exception as e: print(e); continue
    for m in re.findall(r'["\'`](/?api/[^"\'`]{3,120})',t):
        if 'ndic' in m or 'Index' in m or 'index' in m or 'NextApi' in m: found.add(m)
for f in sorted(found): print(f)
