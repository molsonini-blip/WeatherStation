"""
Tempest Monitor — always-on collector, storm detector, dashboard server and IFTTT bridge
for a WeatherFlow Tempest station.

Data sources (in priority order):
  1. UDP broadcast from the Tempest hub on the LAN (port 50222) — free, unlimited, instant.
  2. Tempest cloud WebSocket — automatic fallback when no UDP packets arrive.
  3. Tempest REST API — forecast, station metadata, gap backfill after restarts.
  4. NWS (api.weather.gov) — official watches/warnings for the station's location.

Modes:
  normal  quiet weather: obs stored once a minute, forecast checked every few hours,
          NWS alerts every 15 min, one daily summary.
  watch   "right before": distant lightning, falling pressure, high rain odds, NWS watch.
          Polls fast, records 3-second wind, dashboard goes live.
  storm   rain, nearby lightning, strong gusts, or an NWS warning. Everything at max rate
          until conditions clear, then a storm summary is logged and sent to IFTTT.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

log = logging.getLogger("tempest")

# ----------------------------------------------------------------------------- config


def env(name, default=None, cast=str):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return cast(v)


@dataclass
class Config:
    token: str | None = env("TEMPEST_TOKEN")
    station_id: int = env("TEMPEST_STATION_ID", 0, int)
    serial: str | None = env("TEMPEST_SERIAL")  # optional: only accept UDP from this ST-xxxx
    ifttt_key: str | None = env("IFTTT_KEY")
    ifttt_prefix: str = env("IFTTT_EVENT_PREFIX", "tempest_")
    ifttt_base: str = env("IFTTT_BASE", "https://maker.ifttt.com")
    http_port: int = env("HTTP_PORT", 8088, int)
    udp_port: int = env("UDP_PORT", 50222, int)
    db_path: str = env("DB_PATH", "/data/tempest.db")
    tz: str = env("TZ", "America/Chicago")
    lat: float | None = env("LAT", None, float)
    lon: float | None = env("LON", None, float)
    nws_user_agent: str = env("NWS_USER_AGENT", "tempest-monitor/1.0 (home weather station)")

    # storm triggers
    storm_lightning_mi: float = env("STORM_LIGHTNING_MI", 15, float)
    storm_gust_mph: float = env("STORM_GUST_MPH", 30, float)
    storm_clear_min: float = env("STORM_CLEAR_MIN", 45, float)
    storm_lookback_min: float = env("STORM_LOOKBACK_MIN", 15, float)  # rain/lightning/gust window
    # watch ("right before") triggers
    watch_lightning_mi: float = env("WATCH_LIGHTNING_MI", 40, float)
    watch_pressure_drop_mb: float = env("WATCH_PRESSURE_DROP_MB", 2.0, float)  # over 3 h
    watch_pop: float = env("WATCH_POP", 60, float)  # % rain chance within next 3 h
    watch_clear_min: float = env("WATCH_CLEAR_MIN", 30, float)
    watch_lookback_min: float = env("WATCH_LOOKBACK_MIN", 30, float)
    # alert thresholds
    alert_lightning_mi: float = env("ALERT_LIGHTNING_MI", 8, float)
    alert_gust_mph: float = env("ALERT_GUST_MPH", 40, float)
    alert_rain_inhr: float = env("ALERT_RAIN_INHR", 1.0, float)
    alert_cooldown_min: float = env("ALERT_COOLDOWN_MIN", 30, float)
    # polling
    normal_forecast_hours: float = env("NORMAL_FORECAST_HOURS", 3, float)
    normal_nws_min: float = env("NORMAL_NWS_MIN", 5, float)
    alert_hail_min_in: float = env("ALERT_HAIL_MIN_IN", 0.0, float)  # NWS hail size that triggers an alert
    hail_cooldown_min: float = env("HAIL_COOLDOWN_MIN", 15, float)
    # hail shield (smart plug powering an inflatable car cover)
    shield_enabled: bool = env("HAIL_SHIELD", "true").lower() in ("1", "true", "yes", "on")
    shield_on_watch: bool = env("HAIL_SHIELD_ON_WATCH", "true").lower() in ("1", "true", "yes", "on")
    shield_min_in: float = env("HAIL_SHIELD_MIN_IN", 0.0, float)
    shield_off_delay_min: float = env("HAIL_SHIELD_OFF_DELAY_MIN", 60, float)
    shield_max_hours: float = env("HAIL_SHIELD_MAX_HOURS", 0, float)  # 0 = no limit
    shield_resend_min: float = env("HAIL_SHIELD_RESEND_MIN", 0, float)  # re-send "on" while active; 0 = off
    shield_on_url: str | None = env("HAIL_SHIELD_ON_URL")   # optional direct webhook (Home Assistant, plug API)
    shield_off_url: str | None = env("HAIL_SHIELD_OFF_URL")
    live_forecast_min: float = env("LIVE_FORECAST_MIN", 15, float)
    live_nws_min: float = env("LIVE_NWS_MIN", 2, float)
    daily_summary_time: str = env("DAILY_SUMMARY_TIME", "07:00")
    udp_stale_s: float = env("UDP_STALE_S", 150, float)
    offline_alert_min: float = env("OFFLINE_ALERT_MIN", 15, float)
    rapid_keep_hours: float = env("RAPID_KEEP_HOURS", 48, float)


CFG = Config()
REST = "https://swd.weatherflow.com/swd/rest"
WS_URL = "wss://ws.weatherflow.com/swd/data"
STATIC = Path(__file__).parent / "static"

ALERT_LABELS = dict(hail_shield_on="Hail shield ON", hail_shield_off="Hail shield OFF", hail="Hail at station", hail_warning="Hail warning", watch_start="Storm watch", storm_start="Storm started", storm_end="Storm ended",
                    all_clear="All clear", lightning_close="Close lightning", high_wind="High wind",
                    heavy_rain="Heavy rain", nws_alert="NWS", daily="Daily summary", offline="Station offline",
                    test="Test")
NWS_STORM_EVENTS = ("Tornado Warning", "Severe Thunderstorm Warning", "Flash Flood Warning",
                    "Extreme Wind Warning", "Special Marine Warning")
NWS_WATCH_WORDS = ("Thunderstorm", "Tornado", "Flood", "Wind", "Hail", "Winter Storm", "Ice")

# ----------------------------------------------------------------------------- units

c_to_f = lambda c: None if c is None else c * 9 / 5 + 32
ms_to_mph = lambda v: None if v is None else v * 2.23694
mm_to_in = lambda v: None if v is None else v / 25.4
km_to_mi = lambda v: None if v is None else v * 0.621371
mb_to_inhg = lambda v: None if v is None else v * 0.0295300


def sea_level_mb(station_mb, elev_m, temp_c):
    if station_mb is None or not elev_m:
        return station_mb
    t = (temp_c if temp_c is not None else 15) + 273.15
    return station_mb * (1 - (0.0065 * elev_m) / (t + 0.0065 * elev_m)) ** -5.257


def dew_point_c(t, rh):
    if t is None or not rh:
        return None
    import math
    a, b = 17.625, 243.04
    g = math.log(rh / 100) + a * t / (b + t)
    return b * g / (a - g)


def cardinal(deg):
    if deg is None:
        return ""
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W",
            "WNW", "NW", "NNW"][int((deg % 360) / 22.5 + 0.5) % 16]


def hail_size(props):
    """Hail size in inches from an NWS alert: the maxHailSize parameter, or the text of a watch."""
    import re
    v = ((props.get("parameters") or {}).get("maxHailSize") or [None])[0]
    try:
        if v is not None and float(v) > 0:
            return float(v)
    except ValueError:
        pass
    text = f"{props.get('headline') or ''} {props.get('description') or ''}".lower()
    if "hail" not in text:
        return None
    m = re.search(r"hail[^.]{0,60}?(\d+(?:\.\d+)?)\s*inch", text) or re.search(r"(\d+(?:\.\d+)?)\s*inch[^.]{0,30}hail", text)
    return float(m.group(1)) if m else 0.0


def hail_name(inches):
    if not inches:
        return "size not given"
    for size, name in ((4.5, "softball"), (2.75, "baseball"), (2.5, "tennis ball"), (1.75, "golf ball"),
                       (1.5, "ping pong ball"), (1.25, "half dollar"), (1.0, "quarter"), (0.88, "nickel"),
                       (0.75, "penny")):
        if inches >= size:
            return name
    return "pea"


# ----------------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs (ts INTEGER PRIMARY KEY, temp_c REAL, rh REAL, pressure_mb REAL,
  wind_lull REAL, wind_avg REAL, wind_gust REAL, wind_dir REAL, uv REAL, solar REAL, illum REAL,
  rain_mm REAL, precip_type INTEGER, strike_dist_km REAL, strike_count INTEGER, battery REAL,
  source TEXT);
CREATE TABLE IF NOT EXISTS rapid (ts INTEGER PRIMARY KEY, speed REAL, dir REAL);
CREATE TABLE IF NOT EXISTS strikes (ts INTEGER PRIMARY KEY, dist_km REAL, energy REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT,
  message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS storms (id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts INTEGER,
  end_ts INTEGER, reasons TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        self.c.execute("PRAGMA journal_mode=WAL")

    def q(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        cur = self.c.execute(sql, args)
        self.c.commit()
        return cur.lastrowid

    def kv_get(self, k, default=None):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r[0]["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))


# ----------------------------------------------------------------------------- monitor


@dataclass
class Monitor:
    db: DB
    http: ClientSession | None = None
    station: dict = field(default_factory=dict)
    device_id: int | None = None
    elevation: float = 0.0
    latest: dict | None = None
    rapid: dict | None = None
    mode: str = "normal"
    reasons: list = field(default_factory=list)
    watch_last: float = 0
    storm_last: float = 0
    storm_id: int | None = None
    storm_start: float | None = None
    last_udp: float = 0
    last_packet: float = 0
    source: str = "none"
    forecast: dict | None = None
    nws: list = field(default_factory=list)
    last_precip_evt: float = 0
    cooldowns: dict = field(default_factory=dict)
    clients: set = field(default_factory=set)
    offline_alerted: bool = False
    _mode_changed: asyncio.Event = field(default_factory=asyncio.Event)
    shield: dict = field(default_factory=lambda: dict(on=False, mode="auto", since=None, clear_since=None,
                                                      reason="", last_sent=0))

    @property
    def live(self):
        return self.mode in ("watch", "storm")

    # --------------------------------------------------------------- broadcast to dashboards
    def push(self, kind, data):
        msg = f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # --------------------------------------------------------------- ingest
    def ingest_obs(self, o, source):
        """o: Tempest obs_st array (UDP, WS and REST share the first 18 fields)."""
        if not o or o[0] is None:
            return
        row = dict(ts=int(o[0]), wind_lull=o[1], wind_avg=o[2], wind_gust=o[3], wind_dir=o[4],
                   pressure_mb=o[6], temp_c=o[7], rh=o[8], illum=o[9], uv=o[10], solar=o[11],
                   rain_mm=o[12] or 0, precip_type=o[13], strike_dist_km=o[14],
                   strike_count=o[15] or 0, battery=o[16], source=source)
        self.db.x(f"INSERT OR REPLACE INTO obs ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                  tuple(row.values()))
        if source == "rest":
            return
        self.latest = row
        self.mark_packet(source)
        snap = self.snapshot()
        self.push("obs", snap)
        g = ms_to_mph(row["wind_gust"]) or 0
        if g >= CFG.alert_gust_mph:
            self.alert("high_wind", f"Gust {g:.0f} mph", f"{cardinal(row['wind_dir'])}",
                       f"avg {ms_to_mph(row['wind_avg']):.0f} mph")
        if row["precip_type"] in (2, 3):
            self.on_station_hail(row)
        rate = self.rain_rate_inhr()
        if rate >= CFG.alert_rain_inhr:
            self.alert("heavy_rain", f"{rate:.2f} in/hr", f"{self.rain_today_in():.2f} in today", "")

    def on_station_hail(self, row):
        """Tempest's haptic sensor classified the last minute's precipitation as hail."""
        now = time.time()
        if now - self.cooldowns.get("hail", 0) < CFG.hail_cooldown_min * 60:
            return
        self.cooldowns["hail"] = now
        kind = "Hail with rain" if row["precip_type"] == 3 else "Hail"
        when = datetime.fromtimestamp(row["ts"], ZoneInfo(CFG.tz)).strftime("%-I:%M %p")
        self.log_event("alert:hail", f"Hail at station: {kind} detected · {when}")
        asyncio.get_event_loop().create_task(self.send_ifttt(
            "hail", f"{kind} detected at {self.station.get('name') or 'your station'}", when,
            f"rain {self.rain_rate_inhr():.2f} in/hr, gust {ms_to_mph(row['wind_gust']) or 0:.0f} mph"))
        self.push("hail", dict(ts=row["ts"], kind=kind))
        asyncio.get_event_loop().call_soon(self.evaluate)

    def ingest_rapid(self, ob, source):
        ts, spd, d = int(ob[0]), ob[1], ob[2]
        self.rapid = dict(ts=ts, mph=ms_to_mph(spd), dir=d, card=cardinal(d))
        self.mark_packet(source)
        if self.live:
            self.db.x("INSERT OR REPLACE INTO rapid VALUES (?,?,?)", (ts, spd, d))
        self.push("rapid", self.rapid)

    def ingest_strike(self, evt, source):
        ts, km, energy = int(evt[0]), evt[1], evt[2] if len(evt) > 2 else None
        self.db.x("INSERT OR REPLACE INTO strikes VALUES (?,?,?)", (ts, km, energy))
        self.mark_packet(source)
        mi = km_to_mi(km)
        self.push("strike", dict(ts=ts, mi=mi))
        if mi is not None and mi <= CFG.alert_lightning_mi:
            self.alert("lightning_close", f"Lightning {mi:.0f} mi away",
                       datetime.fromtimestamp(ts, ZoneInfo(CFG.tz)).strftime("%-I:%M %p"), "")
        asyncio.get_event_loop().call_soon(self.evaluate)

    def ingest_precip(self, evt, source):
        self.last_precip_evt = int(evt[0])
        self.mark_packet(source)
        self.log_event("rain_start", "Rain started")
        self.push("precip", dict(ts=self.last_precip_evt))
        asyncio.get_event_loop().call_soon(self.evaluate)

    def mark_packet(self, source):
        now = time.time()
        self.last_packet = now
        if source == "udp":
            self.last_udp = now
        self.source = source
        if self.offline_alerted:
            self.offline_alerted = False
            self.log_event("online", f"Station data back ({source})")

    def handle_message(self, m, source):
        t = m.get("type")
        if source == "udp" and CFG.serial and m.get("serial_number") not in (CFG.serial, None) \
                and t != "hub_status":
            return
        try:
            if t == "obs_st":
                for o in m.get("obs", []):
                    self.ingest_obs(o, source)
            elif t == "rapid_wind":
                self.ingest_rapid(m["ob"], source)
            elif t == "evt_strike":
                self.ingest_strike(m["evt"], source)
            elif t == "evt_precip":
                self.ingest_precip(m["evt"], source)
        except Exception:  # never let one bad packet kill the listener
            log.exception("bad %s message: %s", source, m)

    # --------------------------------------------------------------- derived values
    def since(self, seconds):
        return int(time.time() - seconds)

    def rain_rate_inhr(self):
        r = self.db.q("SELECT COALESCE(SUM(rain_mm),0) s FROM obs WHERE ts>=?", (self.since(600),))
        return mm_to_in(r[0]["s"]) * 6

    def local_midnight(self):
        now = datetime.now(ZoneInfo(CFG.tz))
        return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def rain_today_in(self):
        r = self.db.q("SELECT COALESCE(SUM(rain_mm),0) s FROM obs WHERE ts>=?", (self.local_midnight(),))
        return mm_to_in(r[0]["s"])

    def pressure_change_3h(self):
        now = self.db.q("SELECT pressure_mb p FROM obs WHERE ts>=? AND pressure_mb IS NOT NULL ORDER BY ts DESC LIMIT 1",
                        (self.since(900),))
        then = self.db.q("SELECT pressure_mb p FROM obs WHERE ts BETWEEN ? AND ? AND pressure_mb IS NOT NULL ORDER BY ts DESC LIMIT 1",
                         (self.since(3 * 3600 + 900), self.since(3 * 3600 - 900)))
        if now and then:
            return now[0]["p"] - then[0]["p"]
        return None

    def nearest_strike_mi(self, seconds):
        r = self.db.q("SELECT MIN(dist_km) d, COUNT(*) n FROM strikes WHERE ts>=? AND dist_km>0", (self.since(seconds),))
        return (km_to_mi(r[0]["d"]), r[0]["n"]) if r and r[0]["n"] else (None, 0)

    def max_gust_mph(self, seconds):
        r = self.db.q("SELECT MAX(wind_gust) g FROM obs WHERE ts>=?", (self.since(seconds),))
        g = r[0]["g"] if r else None
        if self.live:
            r2 = self.db.q("SELECT MAX(speed) g FROM rapid WHERE ts>=?", (self.since(seconds),))
            if r2 and r2[0]["g"] is not None:
                g = max(g or 0, r2[0]["g"])
        return ms_to_mph(g)

    def max_pop_next(self, hours):
        try:
            hourly = self.forecast["forecast"]["hourly"]
        except (TypeError, KeyError):
            return None
        end = time.time() + hours * 3600
        pops = [h.get("precip_probability", 0) for h in hourly if time.time() - 3600 <= h["time"] <= end]
        return max(pops) if pops else None

    # --------------------------------------------------------------- mode state machine
    def evaluate(self):
        now = time.time()
        storm, watch = [], []
        sl, wl = CFG.storm_lookback_min * 60, CFG.watch_lookback_min * 60
        rained = self.db.q("SELECT COUNT(*) n FROM obs WHERE ts>=? AND rain_mm>0", (self.since(sl),))[0]["n"]
        if rained or now - self.last_precip_evt < sl:
            storm.append("rain falling")
        if self.db.q("SELECT COUNT(*) n FROM obs WHERE ts>=? AND precip_type IN (2,3)", (self.since(sl),))[0]["n"]:
            storm.insert(0, "hail detected")
        d15, n15 = self.nearest_strike_mi(sl)
        if d15 is not None and d15 <= CFG.storm_lightning_mi:
            storm.append(f"lightning {d15:.0f} mi")
        g = self.max_gust_mph(sl)
        if g and g >= CFG.storm_gust_mph:
            storm.append(f"gusts {g:.0f} mph")
        for a in self.nws:
            if a["event"] in NWS_STORM_EVENTS:
                storm.append(f"NWS {a['event']}")

        d30, n30 = self.nearest_strike_mi(wl)
        if d30 is not None and d30 <= CFG.watch_lightning_mi:
            watch.append(f"lightning {d30:.0f} mi ({n30} strikes/{CFG.watch_lookback_min:g} min)")
        dp = self.pressure_change_3h()
        if dp is not None and dp <= -CFG.watch_pressure_drop_mb:
            watch.append(f"pressure falling {dp:+.1f} mb/3 h")
        pop = self.max_pop_next(3)
        if pop is not None and pop >= CFG.watch_pop:
            watch.append(f"{pop:.0f}% rain chance within 3 h")
        for a in self.nws:
            if a["event"] not in NWS_STORM_EVENTS and any(w in a["event"] for w in NWS_WATCH_WORDS):
                watch.append(f"NWS {a['event']}")

        if storm:
            self.storm_last = now
        if storm or watch:
            self.watch_last = now

        if now - self.storm_last < CFG.storm_clear_min * 60 and self.storm_last:
            new = "storm"
        elif now - self.watch_last < CFG.watch_clear_min * 60 and self.watch_last:
            new = "watch"
        else:
            new = "normal"
        if storm or watch:
            dup = any(r.startswith("lightning") for r in storm)
            self.reasons = storm + [w for w in watch if not (dup and w.startswith("lightning"))]
        elif new == "normal":
            self.reasons = []
        # otherwise keep the last reasons while the clear-out timer runs
        if new != self.mode:
            self.transition(self.mode, new)
        self.update_shield()

    # --------------------------------------------------------------- hail shield (smart plug)
    def hail_threats(self):
        """Reasons hail is expected or happening right now (empty list = none)."""
        out = []
        for a in self.nws:
            h = a.get("hail_in")
            if h is None or (h and h < CFG.shield_min_in):
                continue
            if "Warning" in a["event"] or (CFG.shield_on_watch and "Watch" in a["event"]):
                out.append(f"{a['event']}" + (f" ({h:.2f} in hail)" if h else " (hail)"))
        if self.db.q("SELECT COUNT(*) n FROM obs WHERE ts>=? AND precip_type IN (2,3)",
                     (self.since(max(CFG.storm_lookback_min, 15) * 60),))[0]["n"]:
            out.append("hail detected at station")
        return out

    def update_shield(self):
        sh, now = self.shield, time.time()
        if not CFG.shield_enabled:
            return
        if sh["mode"] in ("on", "off"):  # manual override from the dashboard
            want, why = sh["mode"] == "on", f"manual {sh['mode']}"
        else:
            threats = self.hail_threats()
            if threats:
                sh["clear_since"] = None
                want, why = True, "; ".join(threats)
            elif sh["on"]:
                # keep it up through the rest of the storm, then wait the off-delay
                if self.mode == "storm":
                    sh["clear_since"] = None
                elif not sh["clear_since"]:
                    sh["clear_since"] = now
                    self.log_event("shield", f"No hail expected; cover stays up {CFG.shield_off_delay_min:.0f} more min")
                want = not (sh["clear_since"] and now - sh["clear_since"] >= CFG.shield_off_delay_min * 60)
                why = "storm over, no more hail expected" if not want else sh["reason"]
                if CFG.shield_max_hours and sh["since"] and now - sh["since"] > CFG.shield_max_hours * 3600:
                    want, why = False, f"safety limit {CFG.shield_max_hours:g} h reached"
            else:
                want, why = False, ""
        if want != sh["on"]:
            sh.update(on=want, since=now if want else None, clear_since=None, reason=why if want else "")
            self.send_shield(want, why)
        elif want and CFG.shield_resend_min and now - sh["last_sent"] > CFG.shield_resend_min * 60:
            self.send_shield(True, sh["reason"], resend=True)
        self.db.kv_set("shield", sh)

    def send_shield(self, on, why, resend=False):
        self.shield["last_sent"] = time.time()
        name = "hail_shield_on" if on else "hail_shield_off"
        if not resend:
            self.log_event(f"alert:{name}", f"{ALERT_LABELS[name]}: {why}")
            self.push("shield", self.shield_state())
        loop = asyncio.get_event_loop()
        loop.create_task(self.send_ifttt(name, "Inflating car cover" if on else "Deflating car cover", why,
                                         self.station.get("name") or ""))
        url = CFG.shield_on_url if on else CFG.shield_off_url
        if url and self.http:
            async def hit():
                try:
                    async with self.http.post(url) as r:
                        log.info("shield %s webhook -> HTTP %s", "on" if on else "off", r.status)
                except Exception as e:
                    log.warning("shield webhook failed: %s", e)
            loop.create_task(hit())

    def shield_state(self):
        sh = self.shield
        left = None
        if sh["on"] and sh["clear_since"]:
            left = max(0, CFG.shield_off_delay_min * 60 - (time.time() - sh["clear_since"]))
        return dict(enabled=CFG.shield_enabled, on=sh["on"], mode=sh["mode"], since=sh["since"],
                    reason=sh["reason"], off_in_s=left, threats=self.hail_threats())

    def transition(self, old, new):
        now = time.time()
        self.mode = new
        why = "; ".join(self.reasons) or "conditions cleared"
        log.info("mode %s -> %s (%s)", old, new, why)
        self.log_event("mode", f"{old} → {new}: {why}", dict(old=old, new=new))
        if new == "storm" and old != "storm":
            self.storm_start = now
            self.storm_id = self.db.x("INSERT INTO storms (start_ts, reasons) VALUES (?,?)",
                                      (int(now), json.dumps(self.reasons)))
            self.db.kv_set("open_storm", dict(id=self.storm_id, start=now))
            snap = self.snapshot()
            self.alert("storm_start", why, f"{snap['temp_f']:.0f}°F" if snap.get("temp_f") is not None else "",
                       f"wind {snap.get('wind_avg_mph') or 0:.0f} mph", cooldown=False)
        elif old == "storm":
            self.close_storm()
        if new == "watch" and old == "normal":
            self.alert("watch_start", why, "", "", cooldown=False)
        if new == "normal":
            self.alert("all_clear", "Back to normal monitoring", "", "", cooldown=False)
        self.push("mode", dict(mode=new, reasons=self.reasons))
        self._mode_changed.set()

    def close_storm(self):
        if not self.storm_id:
            return
        s, e = int(self.storm_start), int(time.time())
        rain = self.db.q("SELECT COALESCE(SUM(rain_mm),0) r, MAX(wind_gust) g, MIN(temp_c) tmin, MAX(temp_c) tmax "
                         "FROM obs WHERE ts BETWEEN ? AND ?", (s, e))[0]
        rg = self.db.q("SELECT MAX(speed) g FROM rapid WHERE ts BETWEEN ? AND ?", (s, e))[0]["g"]
        st = self.db.q("SELECT COUNT(*) n, MIN(dist_km) d FROM strikes WHERE ts BETWEEN ? AND ?", (s, e))[0]
        summary = dict(
            duration_min=round((e - s) / 60), rain_in=round(mm_to_in(rain["r"]), 2),
            max_gust_mph=round(ms_to_mph(max(rain["g"] or 0, rg or 0)), 1),
            strikes=st["n"], closest_strike_mi=round(km_to_mi(st["d"]), 1) if st["d"] else None,
            temp_drop_f=round((rain["tmax"] - rain["tmin"]) * 9 / 5, 1) if rain["tmax"] is not None else None)
        self.db.x("UPDATE storms SET end_ts=?, summary=? WHERE id=?", (e, json.dumps(summary), self.storm_id))
        self.db.kv_set("open_storm", None)
        hrs, mins = divmod(summary["duration_min"], 60)
        self.alert("storm_end", f"Storm over after {hrs}h {mins}m" if hrs else f"Storm over after {mins} min",
                   f"{summary['rain_in']:.2f} in rain, max gust {summary['max_gust_mph']:.0f} mph",
                   f"{summary['strikes']} strikes" + (f", closest {summary['closest_strike_mi']} mi"
                                                       if summary["closest_strike_mi"] else ""),
                   cooldown=False)
        self.storm_id = self.storm_start = None

    # --------------------------------------------------------------- events & IFTTT
    def log_event(self, kind, message, data=None):
        ts = int(time.time())
        self.db.x("INSERT INTO events (ts, kind, message, data) VALUES (?,?,?,?)",
                  (ts, kind, message, json.dumps(data or {})))
        self.push("event", dict(ts=ts, kind=kind, message=message))

    def alert(self, name, v1, v2="", v3="", cooldown=True):
        now = time.time()
        if cooldown and now - self.cooldowns.get(name, 0) < CFG.alert_cooldown_min * 60:
            return
        self.cooldowns[name] = now
        label = ALERT_LABELS.get(name, name)
        self.log_event(f"alert:{name}", f"{label}: " + " · ".join(x for x in (v1, v2, v3) if x))
        asyncio.get_event_loop().create_task(self.send_ifttt(name, v1, v2, v3))

    async def send_ifttt(self, name, v1, v2, v3):
        if not CFG.ifttt_key or not self.http:
            return
        event = CFG.ifttt_prefix + name
        url = f"{CFG.ifttt_base}/trigger/{event}/with/key/{CFG.ifttt_key}"
        try:
            async with self.http.post(url, json=dict(value1=v1, value2=v2, value3=v3)) as r:
                if r.status >= 300:
                    log.warning("IFTTT %s -> HTTP %s", event, r.status)
                else:
                    log.info("IFTTT %s sent", event)
        except Exception as e:
            log.warning("IFTTT %s failed: %s", event, e)

    # --------------------------------------------------------------- snapshot for API/UI
    def snapshot(self):
        o = self.latest or {}
        t = o.get("temp_c")
        slp = sea_level_mb(o.get("pressure_mb"), self.elevation, t)
        d1h, n1h = self.nearest_strike_mi(3600)
        last = self.db.q("SELECT ts, dist_km FROM strikes ORDER BY ts DESC LIMIT 1")
        dp = self.pressure_change_3h()
        return dict(
            ts=o.get("ts"), mode=self.mode, reasons=self.reasons, source=self.source,
            station=self.station.get("name"), storm_start=self.storm_start,
            temp_f=c_to_f(t), rh=o.get("rh"), dew_f=c_to_f(dew_point_c(t, o.get("rh"))),
            pressure_inhg=mb_to_inhg(slp), pressure_trend_mb3h=dp,
            wind_avg_mph=ms_to_mph(o.get("wind_avg")), wind_gust_mph=ms_to_mph(o.get("wind_gust")),
            wind_lull_mph=ms_to_mph(o.get("wind_lull")), wind_dir=o.get("wind_dir"),
            wind_card=cardinal(o.get("wind_dir")), uv=o.get("uv"), solar=o.get("solar"),
            rain_today_in=self.rain_today_in(), rain_rate_inhr=self.rain_rate_inhr(),
            strikes_1h=n1h, nearest_strike_1h_mi=d1h,
            last_strike=dict(ts=last[0]["ts"], mi=km_to_mi(last[0]["dist_km"])) if last else None,
            battery=o.get("battery"), rapid=self.rapid, precip_type=o.get("precip_type"),
            last_packet=self.last_packet or None, ifttt=bool(CFG.ifttt_key), shield=self.shield_state(),
        )

    # --------------------------------------------------------------- REST helpers
    def auth(self):
        return {"Authorization": f"Bearer {CFG.token}"} if CFG.token else {}

    async def rest(self, path, **params):
        for attempt in range(4):
            async with self.http.get(f"{REST}{path}", params=params, headers=self.auth()) as r:
                if r.status == 429:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError(f"rate limited: {path}")

    async def load_station(self):
        if not CFG.token:
            log.warning("No TEMPEST_TOKEN: UDP only, no forecast/backfill/WebSocket fallback")
            return
        try:
            data = await self.rest("/stations")
            sts = data.get("stations", [])
            st = next((s for s in sts if s["station_id"] == CFG.station_id), sts[0] if sts else None)
            if not st:
                return
            self.station = st
            CFG.station_id = st["station_id"]
            self.elevation = (st.get("station_meta") or {}).get("elevation") or 0
            CFG.lat = CFG.lat or st.get("latitude")
            CFG.lon = CFG.lon or st.get("longitude")
            for d in st.get("devices", []):
                if d.get("device_type") == "ST":
                    self.device_id = d["device_id"]
                    CFG.serial = CFG.serial or d.get("serial_number")
            log.info("station %s (%s), device %s, serial %s", st.get("name"), CFG.station_id,
                     self.device_id, CFG.serial)
        except Exception as e:
            log.warning("station lookup failed: %s", e)

    async def backfill(self):
        """After a restart, fill any gap (up to 24 h) from the REST API."""
        if not (CFG.token and self.device_id):
            return
        last = self.db.q("SELECT MAX(ts) t FROM obs")[0]["t"] or 0
        start = max(last + 1, int(time.time()) - 86400)
        if time.time() - start < 600:
            return
        try:
            data = await self.rest(f"/observations/device/{self.device_id}",
                                   time_start=start, time_end=int(time.time()))
            n = 0
            for o in data.get("obs") or []:
                self.ingest_obs(o, "rest")
                n += 1
            log.info("backfilled %d observations", n)
        except Exception as e:
            log.warning("backfill failed: %s", e)

    async def fetch_forecast(self):
        if not CFG.token:
            return
        try:
            self.forecast = await self.rest(
                "/better_forecast", station_id=CFG.station_id, units_temp="f", units_wind="mph",
                units_pressure="inhg", units_precip="in", units_distance="mi")
            self.push("forecast", self.forecast_brief())
        except Exception as e:
            log.warning("forecast failed: %s", e)

    def forecast_brief(self):
        f = (self.forecast or {}).get("forecast") or {}
        return dict(daily=[{k: d.get(k) for k in ("day_start_local", "conditions", "icon", "air_temp_high",
                                                    "air_temp_low", "precip_probability")}
                           for d in (f.get("daily") or [])[:7]],
                    hourly=[{k: h.get(k) for k in ("time", "air_temp", "precip_probability", "conditions")}
                            for h in (f.get("hourly") or [])[:24]])

    async def fetch_nws(self):
        if CFG.lat is None:
            return
        try:
            async with self.http.get("https://api.weather.gov/alerts/active",
                                     params={"point": f"{CFG.lat:.4f},{CFG.lon:.4f}"},
                                     headers={"User-Agent": CFG.nws_user_agent,
                                              "Accept": "application/geo+json"}) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
            alerts = [dict(id=f["properties"]["id"], event=f["properties"]["event"],
                           headline=f["properties"].get("headline"), severity=f["properties"].get("severity"),
                           expires=f["properties"].get("expires"), hail_in=hail_size(f["properties"]),
                           hail_threat=((f["properties"].get("parameters") or {}).get("hailThreat") or [None])[0])
                      for f in data.get("features", [])]
            seen = set(self.db.kv_get("nws_seen", []))
            for a in alerts:
                if a["id"] not in seen:
                    self.alert("nws_alert", a["event"], a["headline"] or "", a["severity"] or "", cooldown=False)
                    if a["hail_in"] is not None and a["hail_in"] >= CFG.alert_hail_min_in:
                        exp = ""
                        try:
                            exp = "until " + datetime.fromisoformat(a["expires"]).astimezone(ZoneInfo(CFG.tz)).strftime("%-I:%M %p")
                        except Exception:
                            pass
                        size = f"{a['hail_in']:.2f} in hail" if a["hail_in"] else "Hail"
                        self.alert("hail_warning", f"{size} possible ({hail_name(a['hail_in'])})",
                                   f"{a['event']} {exp}".strip(),
                                   (a["hail_threat"] or "").title(), cooldown=False)
                    seen.add(a["id"])
            self.db.kv_set("nws_seen", list(seen)[-200:])
            self.nws = alerts
            self.push("nws", alerts)
            self.evaluate()
        except Exception as e:
            log.warning("NWS alerts failed: %s", e)

    # --------------------------------------------------------------- daily summary
    def daily_summary(self, day_start):
        s, e = day_start, day_start + 86400
        r = self.db.q("SELECT MIN(temp_c) lo, MAX(temp_c) hi, COALESCE(SUM(rain_mm),0) rain, MAX(wind_gust) g, "
                      "MAX(uv) uv, COUNT(*) n FROM obs WHERE ts>=? AND ts<?", (s, e))[0]
        st = self.db.q("SELECT COUNT(*) n FROM strikes WHERE ts>=? AND ts<?", (s, e))[0]["n"]
        if not r["n"]:
            return None
        return dict(date=datetime.fromtimestamp(s, ZoneInfo(CFG.tz)).strftime("%a %b %-d"),
                    hi_f=round(c_to_f(r["hi"])), lo_f=round(c_to_f(r["lo"])),
                    rain_in=round(mm_to_in(r["rain"]), 2), max_gust_mph=round(ms_to_mph(r["g"] or 0)),
                    uv=r["uv"], strikes=st)

    async def send_daily(self):
        await self.fetch_forecast()
        y = self.local_midnight() - 86400
        s = self.daily_summary(y)
        today = self.forecast_brief()["daily"][:1]
        fc = ""
        if today:
            d = today[0]
            fc = f"Today: {d['conditions']}, {d['air_temp_high']}°/{d['air_temp_low']}°, {d['precip_probability']}% rain"
        if s:
            self.alert("daily", f"{s['date']}: {s['hi_f']}°/{s['lo_f']}°, {s['rain_in']:.2f} in rain",
                       f"gust {s['max_gust_mph']} mph, {s['strikes']} strikes", fc, cooldown=False)

    # --------------------------------------------------------------- loops
    async def udp_loop(self):
        loop = asyncio.get_running_loop()
        mon = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    mon.handle_message(json.loads(data), "udp")
                except json.JSONDecodeError:
                    pass

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", CFG.udp_port))
        await loop.create_datagram_endpoint(Proto, sock=sock)
        log.info("listening for Tempest UDP on :%d", CFG.udp_port)
        await asyncio.Future()

    async def ws_loop(self):
        """Cloud WebSocket fallback, only while LAN UDP is silent."""
        if not CFG.token:
            return
        await asyncio.sleep(CFG.udp_stale_s)
        while True:
            if time.time() - self.last_udp < CFG.udp_stale_s or not self.device_id:
                await asyncio.sleep(15)
                continue
            log.info("no UDP for %ds, switching to cloud WebSocket", CFG.udp_stale_s)
            try:
                async with self.http.ws_connect(f"{WS_URL}?token={CFG.token}", heartbeat=30) as ws:
                    await ws.send_json(dict(type="listen_start", device_id=self.device_id, id="obs"))
                    rapid_on = False
                    while time.time() - self.last_udp >= CFG.udp_stale_s:
                        if self.live != rapid_on:
                            rapid_on = self.live
                            await ws.send_json(dict(type="listen_rapid_start" if rapid_on else "listen_rapid_stop",
                                                    device_id=self.device_id, id="rapid"))
                        try:
                            msg = await ws.receive(timeout=20)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == WSMsgType.TEXT:
                            self.handle_message(json.loads(msg.data), "ws")
                        elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                            break
                    log.info("UDP is back, closing WebSocket")
            except Exception as e:
                log.warning("WebSocket error: %s", e)
                await asyncio.sleep(30)

    async def scheduler(self):
        """Polling cadence follows the mode: slow when calm, fast when live."""
        last_fc = last_nws = 0
        while True:
            now = time.time()
            fc_every = CFG.live_forecast_min * 60 if self.live else CFG.normal_forecast_hours * 3600
            nws_every = CFG.live_nws_min * 60 if self.live else CFG.normal_nws_min * 60
            if now - last_fc >= fc_every:
                last_fc = now
                await self.fetch_forecast()
            if now - last_nws >= nws_every:
                last_nws = now
                await self.fetch_nws()
            self.evaluate()
            # offline watchdog
            if self.last_packet and now - self.last_packet > CFG.offline_alert_min * 60 and not self.offline_alerted:
                self.offline_alerted = True
                self.alert("offline", f"No station data for {CFG.offline_alert_min:.0f} min", "", "", cooldown=False)
            self._mode_changed.clear()
            try:
                await asyncio.wait_for(self._mode_changed.wait(), timeout=10 if self.live else 30)
                last_fc = last_nws = 0  # mode changed: refresh everything right away
            except asyncio.TimeoutError:
                pass

    async def daily_loop(self):
        hh, mm = map(int, CFG.daily_summary_time.split(":"))
        while True:
            now = datetime.now(ZoneInfo(CFG.tz))
            nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep((nxt - now).total_seconds())
            await self.send_daily()
            cutoff = self.since(CFG.rapid_keep_hours * 3600)
            # keep 3-second wind for storms forever, prune the rest
            self.db.x("DELETE FROM rapid WHERE ts<? AND NOT EXISTS (SELECT 1 FROM storms s WHERE rapid.ts "
                      "BETWEEN s.start_ts AND COALESCE(s.end_ts, rapid.ts))", (cutoff,))


# ----------------------------------------------------------------------------- HTTP API


def bucket(rows, max_points):
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    return [rows[int(i * step)] for i in range(max_points)]


def build_app(mon: Monitor):
    app = web.Application()
    r = app.router

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    async def state(_):
        s = mon.snapshot()
        s["forecast"] = mon.forecast_brief()
        s["nws"] = mon.nws
        return web.json_response(s)

    async def history(req):
        hours = min(float(req.query.get("hours", 24)), 24 * 31)
        since = mon.since(hours * 3600)
        rows = mon.db.q("SELECT ts, temp_c, rh, pressure_mb, wind_avg, wind_gust, wind_dir, rain_mm, uv, solar "
                        "FROM obs WHERE ts>=? ORDER BY ts", (since,))
        out, cum = [], 0.0
        for x in rows:
            cum += x["rain_mm"] or 0
            out.append(dict(ts=x["ts"], temp_f=c_to_f(x["temp_c"]), rh=x["rh"],
                            pressure_inhg=mb_to_inhg(sea_level_mb(x["pressure_mb"], mon.elevation, x["temp_c"])),
                            wind_avg_mph=ms_to_mph(x["wind_avg"]), wind_gust_mph=ms_to_mph(x["wind_gust"]),
                            rain_cum_in=mm_to_in(cum), uv=x["uv"], solar=x["solar"]))
        return web.json_response(bucket(out, 720))

    async def rapid(req):
        mins = min(float(req.query.get("minutes", 30)), 24 * 60)
        rows = mon.db.q("SELECT ts, speed, dir FROM rapid WHERE ts>=? ORDER BY ts", (mon.since(mins * 60),))
        return web.json_response(bucket([dict(ts=x["ts"], mph=ms_to_mph(x["speed"]), dir=x["dir"]) for x in rows], 600))

    async def wind_candles(req):
        """OHLC-style wind: open/close = first/last average speed in the bucket,
        low = lowest lull, high = highest gust (3-second samples included when recorded)."""
        hours = min(float(req.query.get("hours", 24)), 24 * 31)
        bucket_s = int(req.query.get("bucket", 0)) or (900 if hours <= 6 else 3600 if hours <= 24
                                                        else 3 * 3600 if hours <= 72 else 6 * 3600)
        since = mon.since(hours * 3600)
        rows = mon.db.q("SELECT ts, wind_lull, wind_avg, wind_gust, wind_dir FROM obs WHERE ts>=? "
                        "AND wind_avg IS NOT NULL ORDER BY ts", (since,))
        rapid_max = {r["k"]: r["g"] for r in mon.db.q(
            "SELECT ts/? k, MAX(speed) g FROM rapid WHERE ts>=? GROUP BY k", (bucket_s, since))}
        import math
        out, cur = [], None
        for x in rows:
            k = x["ts"] // bucket_s
            if not cur or cur["k"] != k:
                if cur:
                    out.append(cur)
                cur = dict(k=k, ts=k * bucket_s, open=x["wind_avg"], low=x["wind_lull"] or 0,
                           high=x["wind_gust"] or 0, sx=0.0, sy=0.0, n=0, sum=0.0)
            cur["close"] = x["wind_avg"]
            cur["low"] = min(cur["low"], x["wind_lull"] if x["wind_lull"] is not None else x["wind_avg"])
            cur["high"] = max(cur["high"], x["wind_gust"] or 0)
            cur["sum"] += x["wind_avg"]
            cur["n"] += 1
            if x["wind_dir"] is not None and x["wind_avg"]:
                a = math.radians(x["wind_dir"])
                cur["sx"] += math.sin(a) * x["wind_avg"]
                cur["sy"] += math.cos(a) * x["wind_avg"]
        if cur:
            out.append(cur)
        res = []
        for c in out:
            hi = max(c["high"], rapid_max.get(c["k"]) or 0)
            d = (math.degrees(math.atan2(c["sx"], c["sy"])) + 360) % 360 if (c["sx"] or c["sy"]) else None
            res.append(dict(ts=c["ts"], bucket=bucket_s, open=ms_to_mph(c["open"]), close=ms_to_mph(c["close"]),
                            low=ms_to_mph(c["low"]), high=ms_to_mph(hi), mean=ms_to_mph(c["sum"] / c["n"]),
                            dir=d, card=cardinal(d), n=c["n"]))
        return web.json_response(res)

    async def strikes(req):
        hours = min(float(req.query.get("hours", 24)), 24 * 31)
        rows = mon.db.q("SELECT ts, dist_km FROM strikes WHERE ts>=? ORDER BY ts", (mon.since(hours * 3600),))
        return web.json_response([dict(ts=x["ts"], mi=km_to_mi(x["dist_km"])) for x in rows])

    async def storms(req):
        rows = mon.db.q("SELECT * FROM storms ORDER BY start_ts DESC LIMIT ?", (int(req.query.get("limit", 20)),))
        for x in rows:
            x["reasons"] = json.loads(x["reasons"] or "[]")
            x["summary"] = json.loads(x["summary"]) if x["summary"] else None
        return web.json_response(rows)

    async def events(req):
        return web.json_response(mon.db.q("SELECT ts, kind, message FROM events ORDER BY id DESC LIMIT ?",
                                          (int(req.query.get("limit", 50)),)))

    async def daily(req):
        days = min(int(req.query.get("days", 14)), 366)
        mid = mon.local_midnight()
        out = [mon.daily_summary(mid - i * 86400) for i in range(days)]
        return web.json_response([d for d in out if d])

    async def stream(req):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        mon.clients.add(q)
        try:
            await resp.write(f"event: hello\ndata: {json.dumps(mon.snapshot())}\n\n".encode())
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    msg = b": keepalive\n\n"
                await resp.write(msg)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            mon.clients.discard(q)
        return resp

    async def test_ifttt(_):
        if not CFG.ifttt_key:
            return web.json_response(dict(ok=False, error="IFTTT_KEY not set"), status=400)
        await mon.send_ifttt("test", "Tempest monitor test", mon.station.get("name") or "", "")
        return web.json_response(dict(ok=True, event=CFG.ifttt_prefix + "test"))

    r.add_get("/", index)
    r.add_get("/api/state", state)
    r.add_get("/api/history", history)
    r.add_get("/api/rapid", rapid)
    r.add_get("/api/strikes", strikes)
    r.add_get("/api/wind_candles", wind_candles)
    r.add_get("/api/storms", storms)
    r.add_get("/api/events", events)
    r.add_get("/api/daily", daily)
    r.add_get("/api/stream", stream)
    r.add_post("/api/test-ifttt", test_ifttt)

    async def shield(req):
        if req.method == "POST":
            body = await req.json()
            mode = body.get("mode")
            if mode not in ("auto", "on", "off"):
                return web.json_response(dict(error="mode must be auto, on or off"), status=400)
            mon.shield["mode"] = mode
            if mode == "auto" and mon.shield["on"] and not mon.hail_threats():
                mon.shield["clear_since"] = time.time() - CFG.shield_off_delay_min * 60  # nothing expected: off now
            mon.log_event("shield", f"Hail shield set to {mode.upper()} from the dashboard")
            mon.update_shield()
            mon.push("shield", mon.shield_state())
        return web.json_response(mon.shield_state())

    r.add_get("/api/shield", shield)
    r.add_post("/api/shield", shield)
    r.add_static("/static", STATIC)
    return app


# ----------------------------------------------------------------------------- main


async def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    mon = Monitor(DB(CFG.db_path))
    mon.http = ClientSession(timeout=ClientTimeout(total=30))
    # resume an open storm across restarts
    saved = mon.db.kv_get("shield")
    if saved:
        mon.shield.update(saved)
    open_storm = mon.db.kv_get("open_storm")
    if open_storm:
        mon.storm_id, mon.storm_start = open_storm["id"], open_storm["start"]
        mon.mode, mon.storm_last = "storm", time.time()
    last = mon.db.q("SELECT * FROM obs ORDER BY ts DESC LIMIT 1")
    if last:
        mon.latest = last[0]

    await mon.load_station()
    await mon.backfill()

    runner = web.AppRunner(build_app(mon))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CFG.http_port).start()
    log.info("dashboard on http://0.0.0.0:%d", CFG.http_port)
    await asyncio.gather(mon.udp_loop(), mon.ws_loop(), mon.scheduler(), mon.daily_loop())


if __name__ == "__main__":
    asyncio.run(main())
