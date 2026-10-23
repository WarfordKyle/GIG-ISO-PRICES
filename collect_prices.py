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
    # Public ISO-NE five-minute real-time LMP CSV. Needs a browser-style session cookie from a reports page first; no account.
    sess = requests.Session()
    sess.headers.update({**UA, "Accept": "text/csv,*/*"})
    sess.get("https://www.iso-ne.com/isoexpress/web/reports/pricing/-/tree/lmp-by-node", timeout=T)
    txt = ""
    for attempt in range(3):
        r = sess.get("https://www.iso-ne.com/transform/csv/fiveminlmp/current?type=prelim", timeout=T)
        if r.status_code == 200 and "csv" in r.headers.get("Content-Type", "").lower() or (r.status_code == 200 and r.text.startswith(("C,", '"C"'))):
            txt = r.text; break
        time.sleep(3)
    if not txt: raise ValueError(f"ISO-NE: CSV not returned (last HTTP {r.status_code})")
    head, data = None, []
    for row in csv.reader(io.StringIO(txt)):
        if not row: continue
        if row[0] == "H": head = row[1:]
        elif row[0] == "D" and head: data.append(dict(zip(head, row[1:])))
    if not data: raise ValueError("ISO-NE: no data rows in CSV")
    cl = lambda c: c.strip().lower()
    price_col = next((c for c in head if cl(c) in ("lmp", "locational marginal price", "lmp total", "total lmp")), None) \
        or next((c for c in head if ("lmp" in cl(c) or "marginal price" in cl(c)) and not any(k in cl(c) for k in ("energy", "congestion", "loss"))), None)
    if not price_col: raise ValueError("ISO-NE: no price column; header=" + "|".join(head))
    time_col = next((c for c in head if "time" in cl(c)), None)
    want = [x for x in data if "INTERNAL_HUB" in x.get("Location Name", "").upper() or x.get("Location Type", "").strip().upper() == "LOAD ZONE"]
    if not want: raise ValueError("ISO-NE: hub/zone rows not found; header=" + "|".join(head) + " sample=" + str(data[0])[:120])
    prices = {x["Location Name"].strip(): float(x[price_col]) for x in want}
    ts = (want[0].get(time_col, "") if time_col else "") + " ET"
    ok("ISO-NE", ts.strip(), prices, "5-minute real-time LMP: Internal Hub and load zones")


def miso():
    # Public MISO market pricing API (no key): latest 5-minute real-time LMP by pricing node.
    r = requests.get("https://public-api.misoenergy.org/api/MarketPricing/GetLmpConsolidatedTable", headers=UA, timeout=T)
    r.raise_for_status()
    d = r.json()["LMPData"]
    nodes = d["FiveMinLMP"]["PricingNode"]
    if isinstance(nodes, dict): nodes = [nodes]
    hubs = {n["name"]: float(n["LMP"]) for n in nodes if str(n.get("name", "")).upper().endswith(".HUB")}
    if not hubs: raise ValueError("MISO: no hub nodes found")
    ok("MISO", str(d.get("RefId", "")).strip(), hubs, "5-minute real-time LMP at MISO hubs; time is MISO market time (EST)")


def spp():
    # Public SPP Marketplace file browser: latest 5-minute real-time LMP by settlement location. No key needed.
    r = requests.get("https://portal.spp.org/file-browser-api/download/rtbm-lmp-by-location",
                     params={"path": "/RTBM-LMP-SL-latestInterval.csv"}, headers=UA, timeout=T)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if not rows or "LMP" not in rows[0]: raise ValueError("SPP file layout changed")
    by = {x["Settlement Location"].strip(): x for x in rows}
    # Prefer SPP's named north/south hubs if present; otherwise a fixed shortlist of major hubs.
    pref = [n for n in by if "SPP" in n.upper() and any(k in n.upper() for k in ("NORTH", "SOUTH", "HUB"))]
    pick = pref or [n for n in ("CSWS_HUB", "KCPLHUB", "ETEC_HUB", "GRDA_HUB", "GSEC_HUB", "EDEP_SWMPEPHUB") if n in by]
    if not pick: raise ValueError("SPP: no hub locations found in file")
    prices = {n: float(by[n]["LMP"]) for n in pick}
    first = rows[0]
    ok("SPP", first["GMTIntervalEnd"] + " UTC (interval ending)", prices, "5-minute real-time LMP by settlement location")

for name, fn, needs in [("NYISO", nyiso, []), ("CAISO", caiso, []), ("ERCOT", ercot, []),
                        ("PJM", pjm, ["PJM_KEY"]), ("ISO-NE", isone, []),
                        ("MISO", miso, []), ("SPP", spp, [])]:
    miss = [k for k in needs if not os.environ.get(k)]
    if miss: fail(name, "missing credentials: " + ", ".join(miss)); continue
    try: fn()
    except Exception as e: fail(name, f"{type(e).__name__}: {e}")

json.dump(out, open("prices.json", "w"), indent=1)
print(json.dumps({k: v["status"] for k, v in out["isos"].items()}))
