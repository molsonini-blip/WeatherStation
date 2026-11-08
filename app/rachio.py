"""
Rachio sprinkler controller: status, watering history, smart rain/freeze skip from your own Tempest, and manual control.

Uses Rachio's public API (https://api.rach.io/1/public) with the API key from the Rachio app
(Account settings -> Get API key). Rachio allows 1,700 calls a day; this polls about 300.

Watering history comes from each zone's "last watered" time and duration, recorded every time they change. Water used
is estimated from the zone's nozzle rate (inches per hour) and area (square feet): gallons = in/h x hours x ft2 x 0.623.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("tempest.rachio")
API = os.environ.get("RACHIO_API", "https://api.rach.io/1/public")
GAL_PER_IN_FT2 = 0.6234

SCHEMA = """
CREATE TABLE IF NOT EXISTS rachio_runs (zone_id TEXT, start_ts INTEGER, duration_s INTEGER, zone_name TEXT,
  gallons REAL, PRIMARY KEY (zone_id, start_ts));
"""


def env(name, default=None, cast=str):
    v = os.environ.get(name)
    return default if v in (None, "") else cast(v)


class Rachio:
    def __init__(self, mon, cfg):
        self.mon, self.db, self.cfg = mon, mon.db, cfg
        self.db.c.executescript(SCHEMA)
        self.key = env("RACHIO_API_KEY")
        self.person = None
        self.devices = []
        self.schedule = {}       # device id -> current_schedule
        self.error = None
        self.last_poll = None
        self.skip = dict(enabled=env("RACHIO_SKIP", "true").lower() in ("1", "true", "yes", "on"),
                         dry_run=env("RACHIO_SKIP_DRY_RUN", "false").lower() in ("1", "true", "yes", "on"),
                         rain_in=env("RACHIO_SKIP_RAIN_IN", 0.25, float),          # rain in the last 24 h
                         forecast_in=env("RACHIO_SKIP_FORECAST_IN", 0.25, float),  # expected rain next 24 h
                         freeze_f=env("RACHIO_SKIP_FREEZE_F", 35, float),          # forecast low at or below
                         hours=env("RACHIO_SKIP_HOURS", 24, int), last=None)
        saved = self.db.kv_get("rachio_skip_enabled")
        if saved is not None:
            self.skip["enabled"] = bool(saved)

    @property
    def enabled(self):
        return bool(self.key)

    async def call(self, method, path, body=None):
        async with self.mon.http.request(method, f"{API}/{path}", json=body, headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}) as r:
            if r.status == 429:
                raise RuntimeError("Rachio rate limit reached (1,700 calls a day)")
            if r.status >= 400:
                raise RuntimeError(f"Rachio {method} {path}: HTTP {r.status} {(await r.text())[:120]}")
            return await r.json(content_type=None) if r.content_length != 0 else {}

    # ------------------------------------------------------------------ polling
    async def refresh(self):
        if not self.person:
            self.person = (await self.call("GET", "person/info"))["id"]
        me = await self.call("GET", f"person/{self.person}")
        self.devices = me.get("devices", [])
        for d in self.devices:
            try:
                self.schedule[d["id"]] = await self.call("GET", f"device/{d['id']}/current_schedule")
            except Exception:
                self.schedule[d["id"]] = {}
            self.record_runs(d)
        self.last_poll = int(time.time())
        self.error = None

    def record_runs(self, d):
        for z in d.get("zones", []):
            ts, dur = z.get("lastWateredDate"), z.get("lastWateredDuration")
            if not ts or not dur:
                continue
            gal = self.gallons(z, dur)
            self.db.x("INSERT OR IGNORE INTO rachio_runs VALUES (?,?,?,?,?)",
                      (z["id"], int(ts / 1000), int(dur), z.get("name"), gal))

    @staticmethod
    def gallons(z, seconds):
        rate = (z.get("customNozzle") or {}).get("inchesPerHour")
        area = z.get("yardAreaSquareFeet")
        if not rate or not area:
            return None
        return round(rate * seconds / 3600 * area * GAL_PER_IN_FT2, 1)

    async def loop(self):
        if not self.enabled:
            return
        await asyncio.sleep(20)
        while True:
            try:
                await self.refresh()
                await self.smart_skip()
                self.mon.push("rachio", dict(ts=self.last_poll))
            except Exception as e:
                self.error = str(e)[:200]
                log.warning("Rachio: %s", e)
            watering = any((s or {}).get("status") == "PROCESSING" for s in self.schedule.values())
            await asyncio.sleep(120 if watering else 600)

    # ------------------------------------------------------------------ smart skip
    def skip_reasons(self):
        mon, sk = self.mon, self.skip
        out = []
        r = mon.db.q("SELECT COALESCE(SUM(rain_mm),0) mm FROM obs WHERE ts>=?", (mon.since(24 * 3600),))[0]["mm"] / 25.4
        if r >= sk["rain_in"]:
            out.append(f"{r:.2f} in of rain in the last 24 h (Tempest)")
        hourly = ((mon.forecast or {}).get("forecast") or {}).get("hourly") or []
        now = time.time()
        nxt = [h for h in hourly if now <= h.get("time", 0) <= now + 24 * 3600]
        exp = sum((h.get("precip") or 0) * (h.get("precip_probability") or 0) / 100 for h in nxt)
        if exp >= sk["forecast_in"]:
            out.append(f"{exp:.2f} in of rain expected in the next 24 h")
        lows = [h.get("air_temp") for h in nxt if h.get("air_temp") is not None]
        if lows and min(lows) <= sk["freeze_f"]:
            out.append(f"forecast low {min(lows):.0f}°F (freeze risk)")
        return out, round(r, 2), round(exp, 2)

    def rain_delay_until(self, d):
        v = d.get("rainDelayExpirationDate")
        return int(v / 1000) if v and v / 1000 > time.time() else None

    async def smart_skip(self):
        sk = self.skip
        if not sk["enabled"]:
            return
        reasons, past, expected = self.skip_reasons()
        sk["last"] = dict(ts=int(time.time()), reasons=reasons, rain_24h=past, expected_24h=expected)
        if not reasons:
            return
        for d in self.devices:
            if d.get("status") != "ONLINE" or self.rain_delay_until(d):
                continue
            why = "; ".join(reasons)
            if sk["dry_run"]:
                key = f"dry:{d['id']}:{datetime.now().strftime('%Y-%m-%d')}"
                if self.db.kv_get(key):
                    continue
                self.db.kv_set(key, True)
                self.mon.log_event("rachio", f"Would set a {sk['hours']} h rain delay on {d['name']} (dry run): {why}")
                continue
            await self.call("PUT", "device/rain_delay", dict(id=d["id"], duration=min(604800, sk["hours"] * 3600)))
            self.mon.log_event("rachio", f"Rain delay {sk['hours']} h set on {d['name']}: {why}")
        await self.refresh()

    # ------------------------------------------------------------------ snapshot
    def snapshot(self):
        tz = ZoneInfo(self.cfg.tz)
        month_start = int(datetime.now(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        out = []
        for d in self.devices:
            sch = self.schedule.get(d["id"]) or {}
            zones = []
            for z in sorted(d.get("zones", []), key=lambda z: z.get("zoneNumber", 0)):
                m = self.db.q("SELECT COALESCE(SUM(duration_s),0) s, SUM(gallons) g, COUNT(*) n FROM rachio_runs "
                              "WHERE zone_id=? AND start_ts>=?", (z["id"], month_start))[0]
                zones.append(dict(id=z["id"], number=z.get("zoneNumber"), name=z.get("name"), enabled=z.get("enabled"),
                                  last_ts=int(z["lastWateredDate"] / 1000) if z.get("lastWateredDate") else None,
                                  last_min=round((z.get("lastWateredDuration") or 0) / 60),
                                  last_gal=self.gallons(z, z.get("lastWateredDuration") or 0),
                                  month_min=round(m["s"] / 60), month_gal=m["g"], month_runs=m["n"],
                                  rate_inhr=(z.get("customNozzle") or {}).get("inchesPerHour"),
                                  area_ft2=z.get("yardAreaSquareFeet"), image=z.get("imageUrl")))
            running = None
            if sch.get("status") == "PROCESSING":
                zone = next((z for z in d.get("zones", []) if z["id"] == sch.get("zoneId")), {})
                start = (sch.get("zoneStartDate") or 0) / 1000
                remain = max(0, start + (sch.get("zoneDuration") or 0) - time.time()) if start else None
                running = dict(zone=zone.get("name"), zone_id=sch.get("zoneId"), remaining_s=remain,
                               schedule=sch.get("scheduleName") or sch.get("type"))
            rules = [dict(name=r.get("name"), enabled=r.get("enabled"), summary=r.get("summary") or r.get("scheduleJobTypes"))
                     for r in (d.get("scheduleRules") or []) + (d.get("flexScheduleRules") or [])]
            out.append(dict(id=d["id"], name=d.get("name"), status=d.get("status"), on=d.get("on"),
                            paused=d.get("paused"), rain_delay_until=self.rain_delay_until(d),
                            model=d.get("model"), zones=zones, running=running, schedules=rules))
        return dict(enabled=self.enabled, error=self.error, last_poll=self.last_poll, devices=out, skip=self.skip)


def register(r, web, mon, rachio: Rachio):
    async def status(_):
        return web.json_response(rachio.snapshot())

    async def act(req):
        if not rachio.enabled:
            return web.json_response(dict(ok=False, error="RACHIO_API_KEY not set"), status=400)
        b = await req.json()
        action = req.match_info["action"]
        try:
            if action == "start":
                minutes = max(1, min(180, int(b.get("minutes", 10))))
                await rachio.call("PUT", "zone/start", dict(id=b["zone"], duration=minutes * 60))
                name = next((z["name"] for d in rachio.devices for z in d.get("zones", []) if z["id"] == b["zone"]), "zone")
                mon.log_event("rachio", f"Started {name} for {minutes} min from the dashboard")
            elif action == "stop":
                await rachio.call("PUT", "device/stop_water", dict(id=b["device"]))
                mon.log_event("rachio", "Stopped watering from the dashboard")
            elif action == "rain_delay":
                hours = max(0, min(168, int(b.get("hours", 24))))
                await rachio.call("PUT", "device/rain_delay", dict(id=b["device"], duration=hours * 3600))
                mon.log_event("rachio", f"Rain delay {'cleared' if hours == 0 else f'set for {hours} h'} from the dashboard")
            elif action == "skip":
                rachio.skip["enabled"] = bool(b.get("enabled"))
                mon.db.kv_set("rachio_skip_enabled", rachio.skip["enabled"])
                mon.log_event("rachio", f"Smart rain skip turned {'on' if rachio.skip['enabled'] else 'off'}")
            else:
                raise web.HTTPNotFound()
            await asyncio.sleep(2)
            await rachio.refresh()
        except web.HTTPException:
            raise
        except Exception as e:
            return web.json_response(dict(ok=False, error=str(e)[:200]), status=502)
        return web.json_response(dict(ok=True, status=rachio.snapshot()))

    async def history(req):
        days = min(int(req.query.get("days", 30)), 400)
        since = mon.since(days * 86400)
        rows = mon.db.q("""SELECT date(start_ts,'unixepoch','localtime') day, SUM(duration_s)/60.0 minutes, SUM(gallons) gallons
                           FROM rachio_runs WHERE start_ts>=? GROUP BY day ORDER BY day""", (since,))
        rain = {x["day"]: x["rain_in"] for x in mon.db.q("SELECT day, rain_in FROM daily WHERE source='out' AND day>=?",
                                                          (datetime.fromtimestamp(since).date().isoformat(),))}
        runs = mon.db.q("SELECT * FROM rachio_runs WHERE start_ts>=? ORDER BY start_ts DESC LIMIT 200", (since,))
        return web.json_response(dict(daily=rows, rain=rain, runs=runs))

    async def water_link(_):
        """Irrigation gallons per month next to the water bill's gallons."""
        irr = {x["month"]: x for x in mon.db.q("""SELECT strftime('%Y-%m', start_ts, 'unixepoch', 'localtime') month,
                SUM(gallons) gallons, SUM(duration_s)/60.0 minutes FROM rachio_runs GROUP BY month""")}
        bills = {x["month"]: x for x in mon.db.q("""SELECT substr(COALESCE(period_end, bill_date),1,7) month, SUM(usage) usage,
                SUM(amount) amount FROM bills WHERE utility='water' GROUP BY month""")}
        months = sorted(set(irr) | set(bills))[-24:]
        return web.json_response([dict(month=m, irrigation_gal=(irr.get(m) or {}).get("gallons"),
                                       minutes=(irr.get(m) or {}).get("minutes"),
                                       bill_gal=(bills.get(m) or {}).get("usage"), bill_amount=(bills.get(m) or {}).get("amount"))
                                  for m in months])

    r.add_get("/api/rachio", status)
    r.add_post("/api/rachio/{action}", act)
    r.add_get("/api/rachio/history", history)
    r.add_get("/api/rachio/water", water_link)
