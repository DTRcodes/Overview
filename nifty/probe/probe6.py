import requests, re
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36","Accept-Language":"en-US,en;q=0.9"}
s=requests.Session(); s.headers.update(UA)
h=s.get("https://www.niftyindices.com/reports/monthly-reports",timeout=30).text
print(len(h))
for m in sorted(set(re.findall(r'(?:href|src)="([^"]+)"',h))):
    if any(k in m.lower() for k in ["zip","pdf","xls","csv","month","weight","report","factsheet"]): print(m)
for m in sorted(set(re.findall(r'["\'](/[A-Za-z]+/[A-Za-z]+\.aspx/[A-Za-z]+|/BackPage/[A-Za-z]+)["\']',h))): print("EP",m)
i=h.lower().find("monthly"); 
