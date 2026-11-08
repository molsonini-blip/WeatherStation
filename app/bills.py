"""
Monthly bills: electric, water, gas, internet.

Bills arrive as PDFs, either dropped in the network folder (BILLS_SMB_PATH, default the Bills folder next to the
backups on ReadySHARE) or uploaded on the dashboard. Text is pulled out of the PDF and the amount, usage and billing
period are found with provider-aware patterns. If a local Ollama model is reachable (OLLAMA_URL), it fills in anything
the patterns missed. Every bill is shown for review and can be corrected on the dashboard.

Electric usage also comes straight from Smart Meter Texas (SMT_USERNAME / SMT_PASSWORD): 15-minute readings, stored
per interval and per day.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("tempest.bills")

UTILITIES = ("electric", "water", "gas", "internet")
UNITS = {"electric": "kWh", "water": "gal", "gas": "therms", "internet": ""}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (id INTEGER PRIMARY KEY AUTOINCREMENT, utility TEXT, provider TEXT,
  period_start TEXT, period_end TEXT, bill_date TEXT, due_date TEXT, amount REAL, usage REAL, unit TEXT,
  status TEXT, file TEXT, source TEXT, notes TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS smt_interval (ts INTEGER PRIMARY KEY, kwh REAL);
CREATE TABLE IF NOT EXISTS smt_daily (day TEXT PRIMARY KEY, kwh REAL, n INTEGER);
"""

PROVIDERS = [  # (pattern, provider name, utility)
    (r"\brhythm\b", "Rhythm", "electric"),
    (r"\btxu\b", "TXU Energy", "electric"),
    (r"reliant", "Reliant", "electric"),
    (r"oncor|pedernales|\bpec\b", None, "electric"),
    (r"at&t|att\.com|at ?& ?t fiber", "AT&T Fiber", "internet"),
    (r"spectrum|xfinity|comcast|google fiber|astound|grande", None, "internet"),
    (r"atmos", "Atmos Energy", "gas"),
    (r"texas gas service|centerpoint", None, "gas"),
    (r"city of round rock|round rock utility", "City of Round Rock", "water"),
    (r"brushy creek mud|municipal utility district|\bmud\b", None, "water"),
]

FOLDER_HINTS = [  # sub-folder name in the Bills folder -> (provider, utility)
    (r"rhythm", "Rhythm", "electric"), (r"txu", "TXU Energy", "electric"), (r"atmos", "Atmos Energy", "gas"),
    (r"\batt\b|at&t|at ?& ?t", "AT&T Fiber", "internet"), (r"brushy creek", "Brushy Creek MUD", "water"),
    (r"round rock", "City of Round Rock", "water"), (r"water|mud", None, "water"), (r"gas", None, "gas"),
    (r"electric|power|energy", None, "electric"), (r"internet|fiber", None, "internet"),
]
NOT_A_BILL = r"electricity facts label|\befl\b|terms of service|your rights as a customer"


def folder_hint(path: str):
    """(provider, utility) suggested by the folder a PDF was found in, e.g. 'TXU/TXU - 26-05.pdf'."""
    parts = path.replace("\\", "/").lower().split("/")[:-1]
    for part in reversed(parts):
        for pat, prov, util in FOLDER_HINTS:
            if re.search(pat, part):
                return prov, util
    return None, None


MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RE = (rf"(?:\d{{1,2}}/\d{{1,2}}/\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}|"
           rf"(?:{MONTHS})[a-z]*\.? \d{{1,2}},? \d{{4}})")


def env(name, default=None):
    v = os.environ.get(name)
    return default if v in (None, "") else v


def parse_date(s):
    s = s.strip().replace(".", "").replace(",", "")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d %Y", "%B %d %Y", "%b %d %y"):
        try:
            return datetime.strptime(s.title() if fmt.startswith("%b") or fmt.startswith("%B") else s, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"(sept)\w* (\d{1,2}) (\d{4})", s.lower())
    if m:
        return date(int(m.group(3)), 9, int(m.group(2))).isoformat()
    return None


def money(s):
    return float(s.replace(",", "").replace("$", ""))


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages[:6])


def extract(text: str) -> dict:
    """Find utility, provider, amount, usage and billing period in a bill's text."""
    t = re.sub(r"[ \t]+", " ", text)
    low = t.lower()
    out = dict(utility=None, provider=None, amount=None, usage=None, unit=None,
               period_start=None, period_end=None, bill_date=None, due_date=None)
    for pat, name, util in PROVIDERS:
        if re.search(pat, low):
            out["utility"] = out["utility"] or util
            out["provider"] = out["provider"] or name
            if name:
                break
    if not out["utility"]:
        if re.search(r"\bkwh\b", low):
            out["utility"] = "electric"
        elif re.search(r"\bgallons?\b|\bkgal\b", low):
            out["utility"] = "water"
        elif re.search(r"\btherms?\b|\bccf\b|\bmcf\b", low):
            out["utility"] = "gas"
        elif re.search(r"internet|fiber|broadband|mbps|gbps", low):
            out["utility"] = "internet"

    # amount: the clearest "amount due" style label wins
    # actual charges first: with budget / average billing (Atmos, TXU) the "amount due" is a fixed monthly figure
    for label in (r"current gas charge total", r"service address charges subtotal", r"total current charges",
                  r"total amount due", r"amount due", r"total due", r"please pay", r"balance due",
                  r"current charges", r"new charges", r"total charges"):
        m = re.search(label + r"[^$\d\n]{0,40}\$?\s*(-?[\d,]+\.\d{2})", low)
        if m:
            out["amount"] = money(m.group(1))
            break
    if out["amount"] is None:
        vals = [money(x) for x in re.findall(r"\$\s*([\d,]+\.\d{2})", t)]
        if vals:
            out["amount"] = max(vals)

    # usage
    u = out["utility"]
    pats = {"electric": [(r"([\d,]+(?:\.\d+)?)\s*kwh", "kWh")],
            "water": [(r"([\d,]+(?:\.\d+)?)\s*(?:gallons|gal)\b", "gal"), (r"([\d,]+(?:\.\d+)?)\s*kgal", "kgal")],
            "gas": [(r"([\d,]+(?:\.\d+)?)\s*therms?", "therms"), (r"([\d,]+(?:\.\d+)?)\s*ccf", "ccf"),
                    (r"([\d,]+(?:\.\d+)?)\s*mcf", "mcf")]}.get(u, [])
    reads = (re.search(r"current meter read[\s\S]{0,40}?([\d,]+)\s*kwh", low),
             re.search(r"previous meter read[\s\S]{0,40}?([\d,]+)\s*kwh", low))
    m = re.search(r"usage in (ccf|therms?|mcf|gallons|kwh):?\s*([\d,]+(?:\.\d+)?)", low)
    if u == "electric" and all(reads):
        pats = []
        out["usage"], out["unit"] = float(reads[0].group(1).replace(",", "")) - float(reads[1].group(1).replace(",", "")), "kWh"
    elif m:
        pats = []
        unit = {"gallons": "gal", "therm": "therms"}.get(m.group(1), m.group(1))
        out["usage"], out["unit"] = float(m.group(2).replace(",", "")), "kWh" if unit == "kwh" else unit
    for pat, unit in pats:
        cands = []  # (is_labeled_total, value) from lines that aren't per-day averages
        for line in low.splitlines():
            if re.search(r"average|avg|per day|daily|previous|last year|meter read|renewable|equivalent", line):
                continue
            for x in re.findall(pat, line):
                v = float(x.replace(",", ""))
                if v > 0:
                    cands.append((bool(re.search(r"total|billed|usage|consumption|energy charge", line)), v))
        if cands:
            labeled = [v for lab, v in cands if lab]
            val = max(labeled) if labeled else max(v for _, v in cands)
            if unit == "kgal":
                val, unit = val * 1000, "gal"
            out["usage"], out["unit"] = val, unit
            break
    if u == "internet":
        out["unit"] = ""

    # dates: billing period, most specific layout first
    SEP = r"\s*(?:--|-|–|to|through|thru)\s*"
    D = r"(\d{1,2}/\d{1,2}/\d{2,4})"
    m = (re.search(rf"service period:?\s*({DATE_RE}){SEP}({DATE_RE})", low)
         or re.search(rf"{D}\s+\d{{3,7}}\s+{D}\s+\d{{3,7}}\s+\d", low)          # TXU: date read date read usage
         or re.search(rf"{D}\s+{D}\s+\d{{3,7}}\s+\d{{3,7}}", low)                # Atmos: meter, from, to, reads
         or re.search(rf"(?:service|billing|statement)? ?(?:period|dates?|from)[^\n]{{0,25}}?({DATE_RE}){SEP}({DATE_RE})", low))
    if not m:
        for mm in re.finditer(rf"({DATE_RE}){SEP}({DATE_RE})", low):
            if not re.search(r"contract|term|plan", low[max(0, mm.start() - 60):mm.start()]):
                m = mm
                break
    if m:
        out["period_start"], out["period_end"] = parse_date(m.group(1)), parse_date(m.group(2))
    m = (re.search(rf"amount due\s*on\s*({DATE_RE})", low)
         or re.search(rf"(?:due date|payment due|pay by|due by|due on)[\s\S]{{0,60}}?({DATE_RE})", low))
    if m:
        out["due_date"] = parse_date(m.group(1))
    m = re.search(rf"(?:bill|billing|statement|invoice) date:?\s*({DATE_RE})", low)
    if m:
        out["bill_date"] = parse_date(m.group(1))
    return out


async def ollama_extract(http, text: str, have: dict) -> dict:
    """Ask a local model for the fields the patterns couldn't find. Optional; skipped if Ollama isn't there."""
    url = env("OLLAMA_URL", "http://127.0.0.1:11434")
    try:
        async with http.get(f"{url}/api/tags", timeout=3) as r:
            models = [m["name"] for m in (await r.json()).get("models", [])]
    except Exception:
        return {}
    model = env("OLLAMA_MODEL") or next((m for m in models if any(k in m for k in ("qwen", "llama", "mistral", "gemma"))),
                                        models[0] if models else None)
    if not model:
        return {}
    prompt = ("Extract these fields from the utility bill text below and reply with JSON only: "
              "utility (one of electric, water, gas, internet), provider, amount (total amount due, number), "
              "usage (number, for the billing period), unit (kWh, gal, therms, ccf or empty), period_start, period_end, "
              "due_date (dates as YYYY-MM-DD). Use null when unknown.\n\nBILL:\n" + text[:6000])
    try:
        async with http.post(f"{url}/api/generate", json=dict(model=model, prompt=prompt, stream=False, format="json",
                                                               options=dict(temperature=0)), timeout=120) as r:
            res = json.loads((await r.json()).get("response") or "{}")
        log.info("Ollama (%s) read bill fields: %s", model, {k: v for k, v in res.items() if v is not None})
        return {k: v for k, v in res.items() if k in have and v not in (None, "", "null")}
    except Exception as e:
        log.warning("Ollama bill extraction failed: %s", e)
        return {}


class Bills:
    def __init__(self, mon, cfg, resolve_host):
        self.mon, self.db, self.cfg, self.resolve_host = mon, mon.db, cfg, resolve_host
        self.db.c.executescript(SCHEMA)
        self.dir = Path(cfg.db_path).parent / "bills"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.smb_path = env("BILLS_SMB_PATH") or (cfg.backup_smb_path + "/Bills" if cfg.backup_smb_path else None)
        self.status = dict(inbox=self.smb_path, last_scan=None, error=None, smt=None)

    # ------------------------------------------------------------------ ingest
    async def ingest_pdf(self, name: str, data: bytes, source: str):
        if re.search(NOT_A_BILL, name.lower()):
            log.info("skipping %s (not a bill)", name)
            return None
        safe = re.sub(r"[^\w.\- ]", "_", name.replace("/", " - "))[-120:] or "bill.pdf"
        path = self.dir / f"{int(time.time())}-{safe}"
        path.write_bytes(data)
        try:
            text = await asyncio.get_running_loop().run_in_executor(None, pdf_text, data)
        except Exception as e:
            text = ""
            log.warning("could not read %s: %s", name, e)
        if text and re.search(NOT_A_BILL, text[:1500].lower()) and not re.search(r"amount due|total due", text.lower()):
            log.info("skipping %s (looks like a facts label, not a bill)", name)
            path.unlink(missing_ok=True)
            return None
        fields = extract(text)
        hp, hu = folder_hint(name)
        if hu and (fields["utility"] is None or fields["utility"] != hu and hp):
            fields["utility"] = hu
        if hp and not fields["provider"]:
            fields["provider"] = hp
        missing = [k for k in ("utility", "amount", "usage", "period_end") if fields.get(k) is None
                   and not (k == "usage" and fields.get("utility") == "internet")]
        if missing and text and self.mon.http:
            for k, v in (await ollama_extract(self.mon.http, text, fields)).items():
                if fields.get(k) is None:
                    fields[k] = v
        for k in ("amount", "usage"):
            try:
                fields[k] = None if fields[k] is None else float(str(fields[k]).replace(",", "").replace("$", ""))
            except ValueError:
                fields[k] = None
        if fields.get("utility") not in UTILITIES:
            fields["utility"] = None
        fields["unit"] = fields.get("unit") or UNITS.get(fields.get("utility") or "", "")
        ok = all(fields.get(k) is not None for k in ("utility", "amount", "period_end"))
        if fields.get("period_end") and fields.get("amount") is not None:
            dup = self.db.q("SELECT id FROM bills WHERE utility=? AND period_end=? AND ABS(amount-?)<0.01",
                            (fields["utility"], fields["period_end"], fields["amount"]))
            if dup:
                path.unlink(missing_ok=True)
                log.info("%s is already in the list (bill %s)", name, dup[0]["id"])
                return dup[0]["id"]
        status = "ok" if ok else "review"
        bid = self.db.x("""INSERT INTO bills (utility, provider, period_start, period_end, bill_date, due_date, amount, usage,
                           unit, status, file, source, notes, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (fields["utility"], fields["provider"], fields["period_start"], fields["period_end"],
                         fields["bill_date"], fields["due_date"], fields["amount"], fields["usage"], fields["unit"],
                         status, path.name, source, None if text else "no text found in PDF (scanned image?)", int(time.time())))
        label = f"{(fields['provider'] or fields['utility'] or 'Bill')}"
        amt = f"${fields['amount']:.2f}" if fields["amount"] is not None else "amount not found"
        self.mon.log_event("bills", f"Bill added from {source}: {label} {amt}" + (" · needs review" if status == "review" else ""))
        self.mon.push("bills", dict(id=bid))
        return bid

    def scan_inbox(self):
        """Copy new PDFs from the network Bills folder (runs in a thread)."""
        import smbclient
        parts = self.smb_path.replace("\\", "/").strip("/").split("/")
        host, share, folder = parts[0], parts[1], "/".join(parts[2:])
        ip = self.cfg.backup_smb_host or self.resolve_host(host)
        port = self.cfg.backup_smb_port
        smbclient.ClientConfig(require_secure_negotiate=bool(self.cfg.backup_smb_password))
        smbclient.register_session(ip, username=self.cfg.backup_smb_user or "guest",
                                   password=self.cfg.backup_smb_password or "", port=port, require_signing=False)
        root = "\\".join(["", "", ip, share] + [x for x in folder.split("/") if x])
        smbclient.makedirs(root, exist_ok=True, port=port)
        seen = set(self.db.kv_get("bills_seen", []) or [])
        new = []
        for dirpath, _dirs, files in smbclient.walk(root, port=port):
            rel_dir = dirpath[len(root):].strip("\\").replace("\\", "/")
            for name in files:
                if not name.lower().endswith(".pdf"):
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                st = smbclient.stat(f"{dirpath}\\{name}", port=port)
                key = f"{rel}|{st.st_size}"
                if key in seen:
                    continue
                with smbclient.open_file(f"{dirpath}\\{name}", mode="rb", port=port) as fh:
                    new.append((key, rel, fh.read()))
                if len(new) >= 40:  # big first import: take it in batches
                    return new
        return new

    async def inbox_loop(self):
        if not self.smb_path:
            return
        await asyncio.sleep(30)
        while True:
            try:
                new = await asyncio.get_running_loop().run_in_executor(None, self.scan_inbox)
                seen = set(self.db.kv_get("bills_seen", []) or [])
                for key, name, data in new:
                    await self.ingest_pdf(name, data, "network folder")
                    seen.add(key)
                self.db.kv_set("bills_seen", sorted(seen))
                self.status.update(last_scan=int(time.time()), error=None)
                if len(new) >= 40:
                    continue  # more waiting (first big import): keep going
            except Exception as e:
                self.status.update(last_scan=int(time.time()), error=str(e).splitlines()[0][:200])
                log.warning("bills inbox %s: %s", self.smb_path, e)
            await asyncio.sleep(float(env("BILLS_SCAN_MIN", 30)) * 60)

    # ------------------------------------------------------------------ Smart Meter Texas
    async def smt_loop(self):
        user, pw = env("SMT_USERNAME"), env("SMT_PASSWORD")
        if not (user and pw):
            return
        from smart_meter_texas import Account, Client, ClientSSLContext
        from smart_meter_texas.const import INTERVAL_SYNCH
        await asyncio.sleep(45)
        tz = ZoneInfo(self.cfg.tz)
        while True:
            try:
                ssl_ctx = await ClientSSLContext().get_ssl_context()
                account = Account(user, pw)
                client = Client(self.mon.http, account, ssl_ctx)
                await client.authenticate()
                meters = await account.fetch_meters(client)
                esiid = env("SMT_ESIID") or (meters[0].esiid if meters else None)
                if not esiid:
                    raise RuntimeError("no meter found on the Smart Meter Texas account")
                have = {r["day"] for r in self.db.q("SELECT day FROM smt_daily WHERE n>=90")}
                today = datetime.now(tz).date()
                back = int(env("SMT_BACKFILL_DAYS", 60))
                got = 0
                for i in range(1, back + 1):
                    d = today - timedelta(days=i)
                    if d.isoformat() in have:
                        continue
                    res = await client.request(INTERVAL_SYNCH, json=dict(
                        startDate=d.strftime("%m/%d/%Y"), endDate=d.strftime("%m/%d/%Y"), reportFormat="JSON",
                        ESIID=[esiid], versionDate=None, readDate=None, versionNum=None, dataType=None))
                    energy = ((res or {}).get("data") or {}).get("energyData") or []
                    vals = []
                    for entry in energy:
                        if entry.get("RT") == "C":
                            vals += [float(x.split("-")[0]) for x in entry.get("RD", "").split(",") if x.strip()]
                    if not vals:
                        continue
                    start = datetime(d.year, d.month, d.day, tzinfo=tz)
                    for k, v in enumerate(vals[:100]):
                        self.db.c.execute("INSERT OR REPLACE INTO smt_interval VALUES (?,?)",
                                          (int((start + timedelta(minutes=15 * k)).timestamp()), v))
                    self.db.x("INSERT OR REPLACE INTO smt_daily VALUES (?,?,?)", (d.isoformat(), sum(vals), len(vals)))
                    got += 1
                    await asyncio.sleep(3)
                self.status["smt"] = dict(ok=True, ts=int(time.time()), esiid=esiid[-6:], days_added=got)
                if got:
                    log.info("Smart Meter Texas: %d days of 15-minute data added", got)
            except Exception as e:
                self.status["smt"] = dict(ok=False, ts=int(time.time()), error=str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
                log.warning("Smart Meter Texas: %s", e)
            await asyncio.sleep(6 * 3600)

    # ------------------------------------------------------------------ queries
    def summary(self):
        bills = self.db.q("SELECT * FROM bills ORDER BY COALESCE(period_end, bill_date, date(created,'unixepoch')) DESC, id DESC")
        latest = {}
        for u in UTILITIES:
            rows = [b for b in bills if b["utility"] == u and b["amount"] is not None]
            if not rows:
                continue
            cur = rows[0]
            prev = rows[1] if len(rows) > 1 else None
            ly = None
            if cur["period_end"]:
                y = str(int(cur["period_end"][:4]) - 1) + cur["period_end"][4:7]
                ly = next((b for b in rows if (b["period_end"] or "")[:7] == y), None)
            latest[u] = dict(cur=cur, prev_amount=prev["amount"] if prev else None,
                             ly_amount=ly["amount"] if ly else None, ly_usage=ly["usage"] if ly else None)
        return dict(bills=bills, latest=latest, status=self.status)

    def monthly(self):
        """Cost per utility per month (by billing period end), plus weather for the same months."""
        rows = self.db.q("""SELECT substr(COALESCE(period_end, bill_date),1,7) month, utility, SUM(amount) amount, SUM(usage) usage
                            FROM bills WHERE amount IS NOT NULL AND COALESCE(period_end, bill_date) IS NOT NULL
                            GROUP BY month, utility ORDER BY month""")
        wx = {r["month"]: r for r in self.db.q("""SELECT substr(day,1,7) month, AVG(t_avg) t_avg, MAX(t_hi) t_hi,
                 SUM(CASE WHEN t_avg>65 THEN t_avg-65 ELSE 0 END) cdd, SUM(CASE WHEN t_avg<65 THEN 65-t_avg ELSE 0 END) hdd,
                 SUM(rain_in) rain FROM daily WHERE source='out' GROUP BY month""")}
        return dict(rows=rows, weather=wx)


def register(app_router, web, mon, bills: Bills):
    r = app_router

    async def b_summary(_):
        return web.json_response(bills.summary())

    async def b_monthly(_):
        return web.json_response(bills.monthly())

    async def b_upload(req):
        reader = await req.multipart()
        ids = []
        async for part in reader:
            if part.filename and part.filename.lower().endswith(".pdf"):
                data = await part.read(decode=False)
                if len(data) > 25 * 1024 * 1024:
                    return web.json_response(dict(ok=False, error="PDF larger than 25 MB"), status=400)
                ids.append(await bills.ingest_pdf(part.filename, bytes(data), "upload"))
        return web.json_response(dict(ok=bool(ids), ids=ids, error=None if ids else "no PDF in upload"))

    async def b_save(req):
        """Create (no id) or correct (id) a bill by hand."""
        b = await req.json()
        fields = {k: b.get(k) for k in ("utility", "provider", "period_start", "period_end", "bill_date", "due_date",
                                         "amount", "usage", "unit", "notes")}
        for k in ("amount", "usage"):
            fields[k] = None if fields[k] in (None, "") else float(fields[k])
        if fields["utility"] not in UTILITIES:
            return web.json_response(dict(ok=False, error="pick electric, water, gas or internet"), status=400)
        fields["unit"] = fields["unit"] or UNITS[fields["utility"]]
        if b.get("id"):
            sets = ", ".join(f"{k}=?" for k in fields)
            bills.db.x(f"UPDATE bills SET {sets}, status='ok' WHERE id=?", (*fields.values(), int(b["id"])))
            bid = int(b["id"])
        else:
            bid = bills.db.x(f"INSERT INTO bills ({', '.join(fields)}, status, source, created) VALUES "
                             f"({', '.join('?' * len(fields))}, 'ok', 'manual', ?)", (*fields.values(), int(time.time())))
        return web.json_response(dict(ok=True, id=bid))

    async def b_delete(req):
        bills.db.x("DELETE FROM bills WHERE id=?", (int(req.match_info["id"]),))
        return web.json_response(dict(ok=True))

    async def b_pdf(req):
        row = bills.db.q("SELECT file FROM bills WHERE id=?", (int(req.match_info["id"]),))
        if not row or not row[0]["file"]:
            raise web.HTTPNotFound()
        path = (bills.dir / row[0]["file"]).resolve()
        if bills.dir.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "application/pdf"})

    async def b_scan(_):
        if not bills.smb_path:
            return web.json_response(dict(ok=False, error="no Bills folder configured"), status=400)
        try:
            new = await asyncio.get_running_loop().run_in_executor(None, bills.scan_inbox)
        except Exception as e:
            return web.json_response(dict(ok=False, error=str(e).splitlines()[0][:200]), status=502)
        seen = set(bills.db.kv_get("bills_seen", []) or [])
        for key, name, data in new:
            await bills.ingest_pdf(name, data, "network folder")
            seen.add(key)
        bills.db.kv_set("bills_seen", sorted(seen))
        bills.status.update(last_scan=int(time.time()), error=None)
        return web.json_response(dict(ok=True, added=len(new)))

    async def smt_usage(req):
        days = min(int(req.query.get("days", 30)), 400)
        since = (date.today() - timedelta(days=days)).isoformat()
        d = bills.db.q("SELECT day, kwh FROM smt_daily WHERE day>=? ORDER BY day", (since,))
        wx = {x["day"]: x for x in bills.db.q("SELECT day, t_avg, t_hi, t_lo FROM daily WHERE source='out' AND day>=?", (since,))}
        return web.json_response([dict(x, t_avg=(wx.get(x["day"]) or {}).get("t_avg"), t_hi=(wx.get(x["day"]) or {}).get("t_hi"))
                                  for x in d])

    async def smt_interval(req):
        day = req.query.get("day")
        tz = ZoneInfo(mon_cfg_tz())
        d = datetime.fromisoformat(day).replace(tzinfo=tz) if day else datetime.now(tz) - timedelta(days=1)
        s = int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
        return web.json_response(bills.db.q("SELECT ts, kwh FROM smt_interval WHERE ts>=? AND ts<? ORDER BY ts", (s, s + 86400)))

    def mon_cfg_tz():
        return bills.cfg.tz

    r.add_get("/api/bills", b_summary)
    r.add_get("/api/bills/monthly", b_monthly)
    r.add_post("/api/bills/upload", b_upload)
    r.add_post("/api/bills/save", b_save)
    r.add_post("/api/bills/scan", b_scan)
    r.add_delete("/api/bills/{id}", b_delete)
    r.add_get("/api/bills/{id}/pdf", b_pdf)
    r.add_get("/api/smt/daily", smt_usage)
    r.add_get("/api/smt/interval", smt_interval)
