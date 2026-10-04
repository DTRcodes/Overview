import requests, zipfile, io
UA={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"}
for u in ["https://www.niftyindices.com/Market_Capitalisation_Weightage_Beta_for_NIFTY_50_And_NIFTY_Next_50/mcwb_aug26.zip",
          "https://www.niftyindices.com/Market_Capitalisation_Weightage_Beta_for_NIFTY_50_And_NIFTY_Next_50/mcwb_sep26.zip",
          "https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataSep2026.zip"]:
    r=requests.get(u,headers=UA,timeout=60); print(u.split("/")[-1],r.status_code,len(r.content),r.headers.get("content-type"))
    if r.ok and r.content[:2]==b"PK":
        z=zipfile.ZipFile(io.BytesIO(r.content))
        for n in z.infolist()[:40]: print("   ",n.filename,n.file_size)
        print("   total files",len(z.infolist()))
        z.extractall(u.split("/")[-1].replace(".zip",""))
