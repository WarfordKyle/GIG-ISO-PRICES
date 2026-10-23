#!/usr/bin/env python3
"""GIG Energy ISO price collector. Pulls real-time prices from each operator's
official data feed and writes prices.json. Each source is isolated: one failure
never stops the others. Status per ISO is recorded so the briefing can say
exactly what was and was not available.
Env vars (set as repo secrets): PJM_KEY, MISO_KEY, ISONE_USER, ISONE_PASS, SPP_USER, SPP_PASS
"""
import os, io, csv, json, time, zipfile, datetime as dt
import requests

UA = {"User-Agent": "GIG-Energy-Collector/1.0 (kyle.warford@gigenergycorp.com)"}
T = 30
out = {"generated_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "units": "$/MWh", "isos": {}}

def ok(iso, ts, prices, note=""):
    out["isos"][iso] = {"status": "ok", "as_of": ts, "prices": prices, "note": note}
def fail(iso, why):
    out["isos"][iso] = {"status": "unavailable", "reason": str(why)[:200]}

def nyiso():
    r = requests.get("https://mis.nyiso.com/public/realtime/realtime_zone_lbmp.csv", headers=UA, timeout=T); r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    ts = rows[-1]["Time Stamp"] if "Time Stamp" in rows[0] else rows[0].get("Time Stamp", "")
    ok("NYISO", ts + " ET", {x["Name"]: float(x["LBMP ($/MWHr)"]) for x in rows if x.get("Time Stamp") == ts})

def caiso():
    now = dt.datetime.utcnow().replace(second=0, microsecond=0)
    f = lambda d: d.strftime("%Y%m%dT%H:%M-0000")
    prices, ts = {}, ""
    for node in ("TH_SP15_GEN-APND", "TH_NP15_GEN-APND", "TH_ZP26_GEN-APND"):
        url = ("https://oasis.caiso.com/oasisapi/SingleZip?queryname=PRC_INTVL_LMP&version=1&market_run_id=RTM"
               f"&startdatetime={f(now - dt.timedelta(hours=1))}&enddatetime={f(now)}&node={node}&resultformat=6")
        for attempt in range(4):
            time.sleep(6)  # OASIS allows roughly one request per 5 seconds
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code != 429: break
            time.sleep(15 * (attempt + 1))
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content)); rows = list(csv.DictReader(io.StringIO(z.read(z.namelist()[0]).decode())))
        rows = [x for x in rows if x.get("LMP_TYPE") == "LMP"]
        rows.sort(key=lambda x: x["INTERVALSTARTTIME_GMT"])
        last = rows[-1]; ts = last["INTERVALENDTIME_GMT"] if "INTERVALENDTIME_GMT" in last else last["INTERVALSTARTTIME_GMT"]
        prices[node.replace("_GEN-APND", "")] = float(last.get("MW") or last.get("VALUE"))
    ok("CAISO", ts + " UTC", prices)

def ercot():
    # Public ERCOT real-time settlement point price table. Columns: Oper Day | Interval Ending | HB_... | LZ_...
    import re
    r = requests.get("https://www.ercot.com/content/cdr/html/real_time_spp.html", headers=UA, timeout=T); r.raise_for_status()
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, flags=re.S | re.I):
        cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S | re.I)]
        if cells: rows.append(cells)
    head = next(r_ for r_ in rows if any(c.startswith("HB_") for c in r_))
    data = [r_ for r_ in rows if re.fullmatch(r"\d{2}/\d{2}/\d{4}", r_[0]) and len(r_) == len(head)]
    if not data: raise ValueError("ERCOT table layout changed (no data rows)")
    last = data[-1]; hhmm = last[1].zfill(4)
    prices = {h: float(v) for h, v in zip(head, last) if h.startswith("HB_")}
    ok("ERCOT", f"{last[0]} {hhmm[:2]}:{hhmm[2:]} CT (interval ending)", prices, "includes reliability adders")

def pjm():
    key = os.environ["PJM_KEY"]
    r = requests.get("https://api.pjm.com/api/v1/rt_unverified_fivemin_lmps",
        params={"rowCount": 12, "sort": "datetime_beginning_ept", "order": "Desc", "pnode_id": "1", "fields": "datetime_beginning_ept,pnode_name,total_lmp_rt", "format": "json"},
        headers={**UA, "Ocp-Apim-Subscription-Key": key}, timeout=T); r.raise_for_status()
    it = r.json()["items"][0]
    ok("PJM", it["datetime_beginning_ept"] + " ET", {it["pnode_name"]: float(it["total_lmp_rt"])}, "pnode_id 1 is a placeholder; set the PJM hub/RTO node IDs you want")

def isone():
    u, p = os.environ["ISONE_USER"], os.environ["ISONE_PASS"]
    r = requests.get("https://webservices.iso-ne.com/api/v1.1/fiveminutelmp/current/location/4000.json", auth=(u, p), headers=UA, timeout=T); r.raise_for_status()
    d = r.json()["FiveMinLmps"]["FiveMinLmp"]; d = d[0] if isinstance(d, list) else d
    ok("ISO-NE", d["BeginDate"], {"Hub": float(d["LmpTotal"])})

def miso():
    key = os.environ["MISO_KEY"]
    r = requests.get("https://apim.misoenergy.org/pricing/v1/real-time/latest/lmp", headers={**UA, "Ocp-Apim-Subscription-Key": key}, timeout=T); r.raise_for_status()
    raise NotImplementedError("Map MISO response fields after registering and viewing the live schema")

def spp():
    raise NotImplementedError("SPP: add the Marketplace file-browser API call after account setup")

for name, fn, needs in [("NYISO", nyiso, []), ("CAISO", caiso, []), ("ERCOT", ercot, []),
                        ("PJM", pjm, ["PJM_KEY"]), ("ISO-NE", isone, ["ISONE_USER", "ISONE_PASS"]),
                        ("MISO", miso, ["MISO_KEY"]), ("SPP", spp, [])]:
    miss = [k for k in needs if not os.environ.get(k)]
    if miss: fail(name, "missing credentials: " + ", ".join(miss)); continue
    try: fn()
    except Exception as e: fail(name, f"{type(e).__name__}: {e}")

json.dump(out, open("prices.json", "w"), indent=1)
print(json.dumps({k: v["status"] for k, v in out["isos"].items()}))

