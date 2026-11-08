"""
Properties: the homestead and rentals - mortgages, escrow, property taxes, insurance and (for a rental that has them)
utility bills.

PDFs are read from the Properties folder on ReadySHARE (PROPERTIES_SMB_PATH, default the Properties folder next to the
backups), one sub-folder per property, or uploaded on the dashboard. Each PDF is sorted into:

  * a mortgage statement   -> balance, rate, payment split (principal / interest / escrow), escrow balance, year-to-date
                              interest and escrow disbursements (taxes / insurance paid)
  * an escrow analysis     -> each tax and insurance payment out of escrow, by month (actual or projected)
  * a utility bill         -> handed to the Bills reader and filed under that property (it doesn't show on the Bills tab)

Anything else (contracts, tax returns, IDs...) is ignored and not kept.

Payoff dates are worked out from the latest statement of each loan: balance, rate and the principal + interest part of
the payment give the number of payments left (standard amortization), plus what extra principal each month would do.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

import bills as bills_mod

log = logging.getLogger("tempest.properties")

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, name TEXT, address TEXT,
  kind TEXT DEFAULT 'rental', occupancy TEXT DEFAULT 'rented', purchase_date TEXT, purchase_price REAL, value REAL, rent REAL, notes TEXT, archived INTEGER DEFAULT 0,
  created INTEGER);
CREATE TABLE IF NOT EXISTS loan_statements (id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER, servicer TEXT, loan TEXT,
  statement_date TEXT, due_date TEXT, balance REAL, rate REAL, escrow_balance REAL, payment REAL, principal REAL,
  interest REAL, escrow REAL, tax_monthly REAL, ins_monthly REAL, maturity TEXT, interest_ytd REAL, taxes_ytd REAL,
  insurance_ytd REAL, file TEXT, status TEXT, source TEXT, notes TEXT, created INTEGER, edited INTEGER);
CREATE TABLE IF NOT EXISTS escrow_items (id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER, month TEXT, kind TEXT,
  description TEXT, amount REAL, estimate INTEGER, source_date TEXT, file TEXT, UNIQUE(property_id, month, kind));
CREATE TABLE IF NOT EXISTS property_costs (id INTEGER PRIMARY KEY AUTOINCREMENT, property_id INTEGER, year INTEGER, kind TEXT,
  amount REAL, provider TEXT, notes TEXT, created INTEGER);
"""

SERVICERS = [(r"rocket ?mortgage", "Rocket Mortgage"), (r"regions ?mortgage|regionsmortgage", "Regions Mortgage"),
             (r"truist|bb&t|branch banking", "Truist"), (r"united ?wholesale|myuwmloan|\buwm\b", "UWM"),
             (r"mr\.? ?cooper|mrcooper", "Mr. Cooper"), (r"newrez|shellpoint", "NewRez"), (r"pennymac", "PennyMac"),
             (r"wells ?fargo", "Wells Fargo"), (r"loandepot", "loanDepot"), (r"freedom mortgage", "Freedom Mortgage"),
             (r"lakeview", "Lakeview"), (r"cenlar", "Cenlar"), (r"flagstar", "Flagstar"), (r"pnc", "PNC"),
             (r"communityamerica|cacu", "CommunityAmerica CU"), (r"chase", "Chase")]
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
M = r"\$?(-?[\d,]+\.\d{2})"


def env(name, default=None):
    v = os.environ.get(name)
    return default if v in (None, "") else v


def money(s):
    return None if s is None else float(s.replace(",", "").replace("$", ""))


def squash(text):
    return re.sub(r"\s+", "", text.lower())


def prop_key(num, street):
    return f"{num} {street[:4].lower()}"


def key_from_name(name):
    """'11528 Rustic Rock Dr' -> '11528 rust', '111 E Main Street' -> '111 main'"""
    m = re.search(r"(\d{2,6})\s+(?:[NSEW]\.?\s+)?([A-Za-z0-9]{4})", name or "")
    return prop_key(m.group(1), m.group(2)) if m else None


def address_key(sq):
    """Property address on a statement (not the mailing address): (house number, the letters after it)."""
    m = re.search(r"propertyaddress:?(?:\d\d/\d\d/\d{4})?(\d{2,6})([a-z]{4,14})", sq)
    return (m.group(1), m.group(2)) if m else None


def parse_seed(spec):
    """PROPERTIES=16609 Malaga Hills Dr, Round Rock:homestead; 740 N 14th St, Temple:commercial,vacant"""
    out = []
    for part in (spec or "").split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, tags = part.rpartition(":") if ":" in part else (part, "", "")
        tags = [t.strip().lower() for t in tags.split(",") if t.strip()]
        kind = next((t for t in tags if t in ("homestead", "rental", "commercial")), "rental")
        occ = "vacant" if any(t in ("vacant", "not rented", "empty") for t in tags) else "owner" if kind == "homestead" else "rented"
        street = name.split(",")[0].strip()
        out.append(dict(name=street, address=name.strip(), kind=kind, occupancy=occ))
    return out


def pdate(s):
    if not s:
        return None
    s = s.strip()
    m = re.fullmatch(r"([a-z]+),?(\d{4})", s)  # 'october2044' -> first of the month
    if m and m.group(1)[:3] in MONTHS:
        return date(int(m.group(2)), MONTHS[m.group(1)[:3]], 1).isoformat()
    return bills_mod.parse_date(s)


def kind_of(sq, path=""):
    p = path.lower()
    if re.search(r"utilit", p):
        return "bill"
    stmt = re.search(r"principalbalance", sq) and re.search(r"amountdue|regular(?:monthly)?payment", sq)
    if stmt and re.search(r"escrowbalance|interestrate", sq):
        return "statement"
    if re.search(r"escrow(?:account)?(?:review|analysis|disclosure)|escrowaccounthistory|yourescrowaccount|annualescrow"
                 r"|escrowaccountprojections|escrowaccountactivity", sq):
        return "escrow"
    return None


# ---------------------------------------------------------------------------------------------- statement reader
def parse_statement(text: str) -> dict:
    sq = squash(text)
    out = dict(servicer=None, loan=None, statement_date=None, due_date=None, balance=None, rate=None, escrow_balance=None,
               payment=None, principal=None, interest=None, escrow=None, tax_monthly=None, ins_monthly=None, maturity=None,
               interest_ytd=None, taxes_ytd=None, insurance_ytd=None)
    for pat, name in SERVICERS:
        if re.search(pat, text.lower()) or re.search(pat.replace(" ?", "").replace(" ", ""), sq):
            out["servicer"] = name
            break
    m = re.search(r"loannumber:?(\d{6,})", sq)
    if m:
        out["loan"] = "…" + m.group(1)[-4:]
    D = r"(\d{1,2}/\d{1,2}/\d{2,4})"
    m = re.search(rf"statementdate:?{D}", sq) or re.search(rf"mortgagestatement{D}", sq) or re.search(rf"statementdate.{{0,60}}?{D}", sq)
    if m:
        out["statement_date"] = pdate(m.group(1))
    m = re.search(rf"(?:paymentduedate|duedate):?{D}", sq)
    if m:
        out["due_date"] = pdate(m.group(1))
    m = re.search(r"(?:interestbearingprincipalbalance|outstandingprincipalbalance|unpaidprincipalbalance|principalbalance)"
                  r"\d?:?" + M, sq)
    if m:
        out["balance"] = money(m.group(1))
    m = re.search(r"interestrate:?(\d{1,2}\.\d{2,4})%", sq)
    if m:
        out["rate"] = float(m.group(1))
    m = re.search(r"escrowbalance:?" + M, sq)
    if m:
        out["escrow_balance"] = money(m.group(1))
    m = re.search(r"maturitydate:?([a-z]+\d{4}|\d{1,2}/\d{1,2}/\d{4})", sq)
    if m:
        out["maturity"] = pdate(m.group(1))
    for m in re.finditer(r"(?:totalregularmonthlypayment|regularmonthlypayment|regularpayment)(?:amount)?:?" + M, sq):
        out["payment"] = out["payment"] or money(m.group(1))
        p = m.start()
        win = sq[max(0, p - 600):p + 300]
        # interest due next = balance x rate / 12: pick the matching figure, then the principal next to it
        ints = [(mm.start(), money(mm.group(1))) for mm in re.finditer(r"(?<![a-z])interest:?" + M, win)]
        pris = [(mm.start(), money(mm.group(1))) for mm in re.finditer(r"(?<!additional)(?<!bearing)principal:?" + M, win)]
        if not (ints and pris):
            continue
        if out["balance"] and out["rate"]:
            want = out["balance"] * out["rate"] / 1200
            ip, iv = min(ints, key=lambda x: abs(x[1] - want))
            if abs(iv - want) > max(5, want * 0.1):
                continue
        else:
            ip, iv = min(ints, key=lambda x: abs(x[0] - (len(win) - 300)))
        pp, pv = min(pris, key=lambda x: abs(x[0] - ip))
        pay = money(m.group(1))
        if pv + iv <= pay + 0.01:
            out["payment"], out["principal"], out["interest"] = pay, pv, iv
            out["escrow"] = round(pay - pv - iv, 2)
            break
    m = re.search(r"escrowamount:taxes:" + M + r"insurance:" + M, sq)
    if m:
        out["tax_monthly"], out["ins_monthly"] = money(m.group(1)), money(m.group(2))
    m = re.search(r"escrowdisbursementsonyourbehalftaxespaid:" + M + r"insurancepaid:" + M, sq)
    if m:
        out["taxes_ytd"], out["insurance_ytd"] = money(m.group(1)), money(m.group(2))
    m = re.search(r"paidyeartodateprincipal:" + M + r"interest:" + M, sq)
    if m:
        out["interest_ytd"] = money(m.group(2))
    else:  # table: "Interest $last-payment $year-to-date"
        m = re.search(r"(?<![a-z])interest" + M + r"\$([\d,]+\.\d{2})", sq)
        if m and money(m.group(2)) >= money(m.group(1)):
            out["interest_ytd"] = money(m.group(2))
    return out


# ---------------------------------------------------------------------------------------- escrow analysis reader
ITEM = (r"(county ?tax|school ?tax|city ?tax|property ?tax(?:es)?|real ?estate ?tax|\btax\b|hazard(?: ?sfr| ?insurance)?"
        r"|homeowners?(?: ?insurance)?|property ?insurance|flood(?: ?insurance)?|wind(?:storm)?(?: ?insurance)?|\bhoa\b|mud ?tax)")
MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,? ?(20\d\d)"


def parse_escrow(text: str):
    """Tax and insurance payments out of escrow: [(YYYY-MM, kind, description, amount)]."""
    low = text.lower()
    low = re.sub(r"(?<=[a-z])(?=20\d\d)", " ", low)  # 'dec2024' -> 'dec 2024'
    months = [(m.start(), f"{m.group(2)}-{MONTHS[m.group(1)]:02d}") for m in re.finditer(MONTH_RE, low)]
    items = {}
    for m in re.finditer(r"(-?\$?[\d,]+\.\d{2})\s*[a-z]?\*?\s*" + ITEM, low):
        amt = abs(money(m.group(1).replace("$", "")))
        if not amt:
            continue
        desc = m.group(2)
        if re.search(r"mortgage insurance|pmi", low[max(0, m.start() - 20):m.end() + 5]):
            continue
        before = [mo for pos, mo in months if pos < m.start() and m.start() - pos < 260]
        if not before:
            continue
        kind = "tax" if "tax" in desc else "hoa" if "hoa" in desc else "insurance"
        items[(before[-1], kind)] = (before[-1], kind, desc.strip().title(), amt)
    return list(items.values())


def statement_date_escrow(text):
    sq = squash(text)
    m = (re.search(r"statement(?:prepared)?date:?(\d\d/\d\d/\d{4})", sq) or re.search(r"statementdatepropertyaddress(\d\d/\d\d/\d{4})", sq)
         or re.search(r"(\d\d/\d\d/\d{4})", sq))
    return pdate(m.group(1)) if m else None


# ------------------------------------------------------------------------------------------------- payoff maths
def payoff(balance, rate, pi, extra=0.0):
    """Months left and total interest for a fixed-rate loan; None if the payment doesn't cover the interest."""
    if not balance or balance <= 0:
        return 0, 0.0
    if not pi:
        return None, None
    r = (rate or 0) / 1200
    pay = pi + (extra or 0)
    if r == 0:
        n = math.ceil(balance / pay)
        return n, 0.0
    if pay <= balance * r:
        return None, None
    n = -math.log(1 - r * balance / pay) / math.log(1 + r)
    n = math.ceil(n - 1e-9)
    # walk it for the exact interest (last payment is smaller)
    b, tot = balance, 0.0
    for _ in range(n):
        i = b * r
        tot += i
        b = b + i - pay
        if b <= 0:
            break
    return n, round(tot, 2)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, min(d.day, 28))


class Properties:
    def __init__(self, mon, cfg, resolve_host):
        self.mon, self.db, self.cfg, self.resolve_host = mon, mon.db, cfg, resolve_host
        self.db.c.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.q("PRAGMA table_info(bills)")}
        if "property_id" not in cols:
            self.db.x("ALTER TABLE bills ADD COLUMN property_id INTEGER")
        self.dir = Path(cfg.db_path).parent / "properties"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.smb_path = env("PROPERTIES_SMB_PATH") or (cfg.backup_smb_path + "/Properties" if cfg.backup_smb_path else None)
        self.status = dict(inbox=self.smb_path, last_scan=None, error=None)
        cols = {r["name"] for r in self.db.q("PRAGMA table_info(properties)")}
        if "occupancy" not in cols:
            self.db.x("ALTER TABLE properties ADD COLUMN occupancy TEXT DEFAULT 'rented'")
        # the list in the settings file (PROPERTIES) sets up properties that have no statements yet (commercial, paid off)
        for p in parse_seed(env("PROPERTIES")):
            k = key_from_name(p["name"])
            if k and not self.db.q("SELECT id FROM properties WHERE key=?", (k,)):
                self.db.x("INSERT INTO properties (key, name, address, kind, occupancy, created) VALUES (?,?,?,?,?,?)",
                          (k, p["name"], p["address"], p["kind"], p["occupancy"], int(time.time())))

    # -------------------------------------------------------------------------------------------- properties
    def property_for(self, key, name=None):
        """key: '11528 rust' (from a folder name) or ('11528', 'rusticrockdr') from a statement's property address."""
        if not key:
            return None
        if isinstance(key, tuple):
            num, letters = key
            for row in self.db.q("SELECT id, key FROM properties WHERE key LIKE ?", (num + " %",)):
                if row["key"].split(" ", 1)[1] in letters[:8]:  # '111 main' matches 'emainst', '9111 unit' 'unitedkingdom'
                    return row["id"]
            key = prop_key(num, letters)
        row = self.db.q("SELECT id FROM properties WHERE key=?", (key,))
        if row:
            if name:
                self.db.x("UPDATE properties SET name=? WHERE id=? AND (name IS NULL OR name=key)", (name, row[0]["id"]))
            return row[0]["id"]
        return self.db.x("INSERT INTO properties (key, name, kind, occupancy, created) VALUES (?,?,'rental','rented',?)",
                         (key, name or key.title(), int(time.time())))

    # ---------------------------------------------------------------------------------------------- ingest
    async def ingest_pdf(self, rel: str, data: bytes, source: str, property_id=None):
        """rel: path inside the Properties folder, e.g. '11528 Rustic Rock Dr/Mortgage/Sept 26.pdf'."""
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, bills_mod.pdf_text, data)
        except Exception as e:
            log.warning("could not read %s: %s", rel, e)
            return None
        sq = squash(text)
        kind = kind_of(sq, rel)
        folder = rel.replace("\\", "/").split("/")[0] if "/" in rel.replace("\\", "/") else None
        key = address_key(sq) if kind in ("statement", "escrow") else None
        fkey = key_from_name(folder)
        if not property_id:
            # the statement's own property address wins; the folder name names it and covers bills
            same = key and fkey and fkey.split(" ")[0] == key[0]
            property_id = self.property_for(key or fkey, folder if folder and (same or not key) else None)
        if kind == "bill" or (kind is None and property_id and bills_mod.extract(text).get("utility")):
            return await self.mon.bills.ingest_pdf(rel, data, source, property_id=property_id)
        if kind is None or not property_id:
            log.info("properties: skipping %s (not a mortgage statement, escrow analysis or bill)", rel)
            return None
        safe = re.sub(r"[^\w.\- ]", "_", rel.replace("/", " - "))[-120:]
        fname = f"{int(time.time())}-{safe}"
        if kind == "statement":
            f = parse_statement(text)
            dup = self.db.q("SELECT id FROM loan_statements WHERE property_id=? AND statement_date=? AND COALESCE(loan,'')=?",
                            (property_id, f["statement_date"], f["loan"] or ""))
            if dup and f["statement_date"]:
                return dup[0]["id"]
            (self.dir / fname).write_bytes(data)
            ok = all(f.get(k) is not None for k in ("statement_date", "balance", "rate", "payment"))
            sid = self.db.x(f"""INSERT INTO loan_statements (property_id, {', '.join(f)}, file, status, source, created)
                               VALUES (?, {', '.join('?' * len(f))}, ?, ?, ?, ?)""",
                            (property_id, *f.values(), fname, "ok" if ok else "review", source, int(time.time())))
            self.mon.log_event("properties", f"Mortgage statement added: {self.name(property_id)} "
                                             f"{f['statement_date'] or ''} balance ${f['balance'] or 0:,.2f}")
            self.mon.push("properties", {})
            return sid
        if kind == "escrow":
            items = parse_escrow(text)
            sdate = statement_date_escrow(text)
            (self.dir / fname).write_bytes(data)
            self.store_escrow(property_id, items, sdate, fname)
            self.mon.log_event("properties", f"Escrow statement added: {self.name(property_id)} ({len(items)} tax/insurance payments)")
            self.mon.push("properties", {})
            return property_id

    def store_escrow(self, pid, items, sdate, fname):
        for month, kind, desc, amt in items:
            est = 1 if sdate and month > sdate[:7] else 0
            cur = self.db.q("SELECT id, estimate, source_date FROM escrow_items WHERE property_id=? AND month=? AND kind=?",
                            (pid, month, kind))
            if cur:
                c = cur[0]
                # an actual payment beats a projection; a newer statement beats an older one
                if (c["estimate"] and not est) or (c["estimate"] == est and (sdate or "") >= (c["source_date"] or "")):
                    self.db.x("UPDATE escrow_items SET amount=?, estimate=?, description=?, source_date=?, file=? WHERE id=?",
                              (amt, est, desc, sdate, fname, c["id"]))
            else:
                self.db.x("INSERT INTO escrow_items (property_id, month, kind, description, amount, estimate, source_date, file) "
                          "VALUES (?,?,?,?,?,?,?,?)", (pid, month, kind, desc, amt, est, sdate, fname))

    def name(self, pid):
        r = self.db.q("SELECT name FROM properties WHERE id=?", (pid,))
        return r[0]["name"] if r else "?"

    async def reparse(self):
        """Read stored statements again (after the reader improves). Hand-edited statements are left alone."""
        loop = asyncio.get_running_loop()
        n = 0
        for row in self.db.q("SELECT * FROM loan_statements WHERE file IS NOT NULL AND COALESCE(edited,0)=0"):
            p = self.dir / row["file"]
            if not p.is_file():
                continue
            text = await loop.run_in_executor(None, bills_mod.pdf_text, p.read_bytes())
            f = parse_statement(text)
            ok = all(f.get(k) is not None for k in ("statement_date", "balance", "rate", "payment"))
            self.db.x(f"UPDATE loan_statements SET {', '.join(k + '=?' for k in f)}, status=? WHERE id=?",
                      (*f.values(), "ok" if ok else "review", row["id"]))
            n += 1
        for r in self.db.q("SELECT DISTINCT file, property_id FROM escrow_items"):
            p = self.dir / r["file"]
            if p.is_file():
                text = await loop.run_in_executor(None, bills_mod.pdf_text, p.read_bytes())
                self.store_escrow(r["property_id"], parse_escrow(text), statement_date_escrow(text), r["file"])
                n += 1
        return n

    # --------------------------------------------------------------------------------------- network folder
    def scan_inbox(self):
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
        seen = set(self.db.kv_get("properties_seen", []) or [])
        new = []
        for dirpath, _dirs, files in smbclient.walk(root, port=port):
            rel_dir = dirpath[len(root):].strip("\\").replace("\\", "/")
            for name in files:
                if not name.lower().endswith(".pdf"):
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                try:
                    st = smbclient.stat(f"{dirpath}\\{name}", port=port)
                    key = f"{rel}|{st.st_size}"
                    if key in seen:
                        continue
                    with smbclient.open_file(f"{dirpath}\\{name}", mode="rb", port=port) as fh:
                        new.append((key, rel, fh.read()))
                except Exception as e:
                    log.info("properties: skipping %s for now: %s", rel, str(e).splitlines()[0][:120])
                if len(new) >= 40:
                    return new
        return new

    async def scan_once(self):
        new = await asyncio.get_running_loop().run_in_executor(None, self.scan_inbox)
        seen = set(self.db.kv_get("properties_seen", []) or [])
        for key, rel, data in new:
            try:
                await self.ingest_pdf(rel, data, "network folder")
            except Exception as e:
                log.warning("properties: %s failed: %s", rel, e)
            seen.add(key)
        self.db.kv_set("properties_seen", sorted(seen))
        self.status.update(last_scan=int(time.time()), error=None)
        return len(new)

    async def inbox_loop(self):
        if not self.smb_path:
            return
        await asyncio.sleep(60)
        while True:
            try:
                n = await self.scan_once()
                if n >= 40:
                    continue
            except Exception as e:
                self.status.update(last_scan=int(time.time()), error=str(e).splitlines()[0][:200])
                log.warning("properties folder: %s", e)
            await asyncio.sleep(1800)

    # --------------------------------------------------------------------------------------------- summary
    def summary(self):
        props = self.db.q("SELECT * FROM properties WHERE COALESCE(archived,0)=0 "
                          "ORDER BY kind='homestead' DESC, kind='rental' DESC, name")
        stm = self.db.q("SELECT * FROM loan_statements ORDER BY statement_date")
        today = date.today()
        out = []
        for p in props:
            mine = [s for s in stm if s["property_id"] == p["id"]]
            loans = {}
            for s in mine:
                loans.setdefault(s["loan"] or "loan", []).append(s)
            cur_loans = []
            for ln, ss in loans.items():
                last = ss[-1]
                active = last["statement_date"] and (today - date.fromisoformat(last["statement_date"])).days < 120
                pi = (last["principal"] or 0) + (last["interest"] or 0) or None
                n, tot = payoff(last["balance"], last["rate"], pi)
                start = date.fromisoformat(last["due_date"]) if last["due_date"] else today
                cur_loans.append(dict(loan=ln, servicer=last["servicer"], active=bool(active), statement=last,
                                      pi=pi, months_left=n, interest_left=tot,
                                      payoff_date=add_months(start, n - 1).isoformat() if n else None))
            # most recent loan of a servicing transfer (UWM -> Rocket) is the active one
            active = [l for l in cur_loans if l["active"]] or sorted(cur_loans, key=lambda l: l["statement"]["statement_date"] or "")[-1:]
            out.append(dict(p, loans=cur_loans, current=active,
                            balance=sum(l["statement"]["balance"] or 0 for l in active),
                            payment=sum(l["statement"]["payment"] or 0 for l in active),
                            history=[dict(d=s["statement_date"], loan=s["loan"], balance=s["balance"],
                                          escrow_balance=s["escrow_balance"]) for s in mine if s["statement_date"]]))
        return dict(properties=out, taxins=self.taxins(), status=self.status,
                    statements=self.db.q("SELECT s.*, p.name pname FROM loan_statements s LEFT JOIN properties p "
                                         "ON p.id=s.property_id ORDER BY statement_date DESC"))

    def taxins(self):
        """Property tax and insurance per property per year: hand entries > escrow payments > statement year-to-date."""
        res = {}
        for r in self.db.q("SELECT property_id, substr(month,1,4) y, kind, SUM(amount) amt, MAX(estimate) est "
                           "FROM escrow_items WHERE kind IN ('tax','insurance') GROUP BY property_id, y, kind"):
            res[(r["property_id"], int(r["y"]), r["kind"])] = dict(amount=round(r["amt"], 2), source="escrow", estimate=bool(r["est"]))
        for s in self.db.q("SELECT property_id, statement_date, taxes_ytd, insurance_ytd FROM loan_statements "
                           "WHERE statement_date IS NOT NULL AND (taxes_ytd IS NOT NULL OR insurance_ytd IS NOT NULL)"):
            d = date.fromisoformat(s["statement_date"])
            y = date.fromordinal(d.toordinal() - 5).year  # a Jan 2 statement's year-to-date is last year
            for kind, v in (("tax", s["taxes_ytd"]), ("insurance", s["insurance_ytd"])):
                k = (s["property_id"], y, kind)
                if v and (k not in res or (res[k]["source"] == "statement" and v > res[k]["amount"])):
                    res[k] = dict(amount=v, source="statement", estimate=False)
        for c in self.db.q("SELECT property_id, year, kind, SUM(amount) amt FROM property_costs GROUP BY property_id, year, kind"):
            res[(c["property_id"], c["year"], c["kind"])] = dict(amount=c["amt"], source="manual", estimate=False)
        return [dict(property_id=k[0], year=k[1], kind=k[2], **v) for k, v in sorted(res.items())]


def register(r, web, mon, props: Properties):
    async def summary(_):
        return web.json_response(props.summary())

    async def upload(req):
        reader = await req.multipart()
        pid, ids = None, []
        async for part in reader:
            if part.name == "property_id":
                v = (await part.text()).strip()
                pid = int(v) if v.isdigit() else None
            elif part.filename and part.filename.lower().endswith(".pdf"):
                data = bytes(await part.read(decode=False))
                ids.append(await props.ingest_pdf(part.filename, data, "upload", property_id=pid))
        ok = any(ids)
        return web.json_response(dict(ok=ok, ids=ids, error=None if ok else
                                      "couldn't tell what this PDF is (expected a mortgage statement, escrow statement or bill)"))

    async def save_property(req):
        b = await req.json()
        fields = {k: b.get(k) for k in ("name", "address", "kind", "occupancy", "purchase_date", "purchase_price", "value", "rent",
                                         "notes")}
        for k in ("purchase_price", "value", "rent"):
            fields[k] = None if fields[k] in (None, "") else float(fields[k])
        if fields["kind"] not in ("homestead", "rental", "commercial"):
            fields["kind"] = "rental"
        if fields["occupancy"] not in ("rented", "vacant", "owner"):
            fields["occupancy"] = "owner" if fields["kind"] == "homestead" else "rented"
        if b.get("id"):
            props.db.x(f"UPDATE properties SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), int(b["id"])))
            pid = int(b["id"])
        else:
            key = key_from_name(fields["name"]) or f"p{int(time.time())}"
            pid = props.property_for(key, fields["name"])
            props.db.x(f"UPDATE properties SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), pid))
        if fields["kind"] == "homestead":
            props.db.x("UPDATE properties SET kind='rental' WHERE kind='homestead' AND id<>?", (pid,))
        return web.json_response(dict(ok=True, id=pid))

    async def save_statement(req):
        b = await req.json()
        keys = ("property_id", "servicer", "loan", "statement_date", "due_date", "balance", "rate", "escrow_balance", "payment",
                "principal", "interest", "escrow", "maturity", "interest_ytd", "taxes_ytd", "insurance_ytd", "notes")
        f = {k: b.get(k) for k in keys}
        for k in ("balance", "rate", "escrow_balance", "payment", "principal", "interest", "escrow", "interest_ytd",
                  "taxes_ytd", "insurance_ytd"):
            f[k] = None if f[k] in (None, "") else float(f[k])
        if b.get("id"):
            props.db.x(f"UPDATE loan_statements SET {', '.join(k + '=?' for k in f)}, status='ok', edited=1 WHERE id=?",
                       (*f.values(), int(b["id"])))
            sid = int(b["id"])
        else:
            sid = props.db.x(f"INSERT INTO loan_statements ({', '.join(f)}, status, source, created, edited) VALUES "
                             f"({', '.join('?' * len(f))}, 'ok', 'manual', ?, 1)", (*f.values(), int(time.time())))
        return web.json_response(dict(ok=True, id=sid))

    async def del_statement(req):
        props.db.x("DELETE FROM loan_statements WHERE id=?", (int(req.match_info["id"]),))
        return web.json_response(dict(ok=True))

    async def save_cost(req):
        b = await req.json()
        if b.get("kind") not in ("tax", "insurance", "hoa", "other") or not b.get("year") or b.get("amount") in (None, ""):
            return web.json_response(dict(ok=False, error="pick a kind, year and amount"), status=400)
        props.db.x("DELETE FROM property_costs WHERE property_id=? AND year=? AND kind=?",
                   (int(b["property_id"]), int(b["year"]), b["kind"]))
        props.db.x("INSERT INTO property_costs (property_id, year, kind, amount, provider, notes, created) VALUES (?,?,?,?,?,?,?)",
                   (int(b["property_id"]), int(b["year"]), b["kind"], float(b["amount"]), b.get("provider"), b.get("notes"),
                    int(time.time())))
        return web.json_response(dict(ok=True))

    async def bills_for(req):
        pid = int(req.match_info["id"])
        rows = props.db.q("SELECT * FROM bills WHERE property_id=? ORDER BY COALESCE(period_end, bill_date) DESC", (pid,))
        months = {}
        for b in rows:
            mth = bills_mod.bill_month(b)
            if mth and b["amount"] is not None:
                months.setdefault(mth, {}).setdefault(b["utility"] or "other", 0)
                months[mth][b["utility"] or "other"] = round(months[mth][b["utility"] or "other"] + b["amount"], 2)
        return web.json_response(dict(bills=rows, monthly=[dict(month=k, v=v) for k, v in sorted(months.items())]))

    async def stmt_pdf(req):
        row = props.db.q("SELECT file FROM loan_statements WHERE id=?", (int(req.match_info["id"]),))
        path = (props.dir / (row[0]["file"] if row and row[0]["file"] else "x")).resolve()
        if props.dir.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "application/pdf"})

    async def scan(_):
        if not props.smb_path:
            return web.json_response(dict(ok=False, error="no Properties folder configured"), status=400)
        try:
            n = await props.scan_once()
        except Exception as e:
            return web.json_response(dict(ok=False, error=str(e).splitlines()[0][:200]), status=502)
        return web.json_response(dict(ok=True, added=n))

    async def reparse(_):
        return web.json_response(dict(ok=True, updated=await props.reparse()))

    r.add_get("/api/properties", summary)
    r.add_post("/api/properties/upload", upload)
    r.add_post("/api/properties/save", save_property)
    r.add_post("/api/properties/statement", save_statement)
    r.add_delete("/api/properties/statement/{id}", del_statement)
    r.add_get("/api/properties/statement/{id}/pdf", stmt_pdf)
    r.add_post("/api/properties/cost", save_cost)
    r.add_get("/api/properties/{id}/bills", bills_for)
    r.add_post("/api/properties/scan", scan)
    r.add_post("/api/properties/reparse", reparse)
