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
    # the Tempest's hail flag alone is unreliable (heavy rain can trip it), so it needs backing up
    hail_confirm: bool = env("HAIL_STATION_CONFIRM", "true").lower() in ("1", "true", "yes", "on")
    hail_confirm_minutes: int = env("HAIL_CONFIRM_MINUTES", 3, int)       # hail-flagged minutes within 10 min
    hail_confirm_lightning_mi: float = env("HAIL_CONFIRM_LIGHTNING_MI", 15, float)  # and a thunderstorm this close
    hail_unconfirmed_off_min: float = env("HAIL_UNCONFIRMED_OFF_MIN", 30, float)  # cover down this long after an unbacked flag
    # hail shield (smart plug powering an inflatable car cover)
    shield_enabled: bool = env("HAIL_SHIELD", "true").lower() in ("1", "true", "yes", "on")
    shield_on_watch: bool = env("HAIL_SHIELD_ON_WATCH", "true").lower() in ("1", "true", "yes", "on")
    shield_min_in: float = env("HAIL_SHIELD_MIN_IN", 0.0, float)
    shield_off_delay_min: float = env("HAIL_SHIELD_OFF_DELAY_MIN", 60, float)
    shield_max_hours: float = env("HAIL_SHIELD_MAX_HOURS", 0, float)  # 0 = no limit
    shield_resend_min: float = env("HAIL_SHIELD_RESEND_MIN", 0, float)  # re-send "on" while active; 0 = off
    shield_on_url: str | None = env("HAIL_SHIELD_ON_URL")   # optional direct webhook (Home Assistant, plug API)
    shield_off_url: str | None = env("HAIL_SHIELD_OFF_URL")
    # YoLink cloud API (direct plug control: no IFTTT credits)
    yolink_uaid: str | None = env("YOLINK_UAID")
    yolink_secret: str | None = env("YOLINK_SECRET")
    yolink_device: str | None = env("YOLINK_DEVICE")  # plug name as shown in the YoLink app, or its deviceId
    yolink_host: str = env("YOLINK_HOST", "https://api.yosmart.com")
    yolink_poll_min: float = env("YOLINK_POLL_MIN", 5, float)  # temperature/humidity sensor polling
    yolink_offline_hours: float = env("YOLINK_OFFLINE_HOURS", 6, float)  # no report this long = offline
    # long-term history
    history_backfill: bool = env("HISTORY_BACKFILL", "true").lower() in ("1", "true", "yes")  # download full Tempest history
    history_backfill_delay_s: float = env("HISTORY_BACKFILL_DELAY_S", 2, float)
    backup_keep_days: int = env("BACKUP_KEEP_DAYS", 14, int)
    backup_keep_months: int = env("BACKUP_KEEP_MONTHS", 24, int)
    backup_smb_path: str | None = env("BACKUP_SMB_PATH")  # e.g. //readyshare/Data/WeatherStation
    backup_smb_host: str | None = env("BACKUP_SMB_HOST")  # optional IP if the name doesn't resolve
    backup_smb_user: str = env("BACKUP_SMB_USER", "guest")
    backup_smb_password: str = env("BACKUP_SMB_PASSWORD", "")
    backup_smb_port: int = env("BACKUP_SMB_PORT", 445, int)
    # hail camera (Reolink + printed size markers on a dark plate)
    hailcam_host: str | None = env("HAILCAM_HOST")
    hailcam_user: str = env("HAILCAM_USER", "admin")
    hailcam_password: str = env("HAILCAM_PASSWORD", "")
    hailcam_channel: int = env("HAILCAM_CHANNEL", 0, int)
    hailcam_https: bool = env("HAILCAM_HTTPS", "false").lower() in ("1", "true", "yes")
    hailcam_rtsp: str | None = env("HAILCAM_RTSP")
    hailcam_interval_s: float = env("HAILCAM_INTERVAL_S", 2, float)
    hailcam_plate: str = env("HAILCAM_PLATE_MM", "400x400")
    hailcam_marker_mm: float = env("HAILCAM_MARKER_MM", 50, float)
    hailcam_inset_mm: float = env("HAILCAM_INSET_MM", 12, float)
    hailcam_min_mm: float = env("HAILCAM_MIN_MM", 5, float)
    hailcam_alert_mm: float = env("HAILCAM_ALERT_MM", 6, float)
    hailcam_end_min: float = env("HAILCAM_END_MIN", 10, float)  # no new stones this long = hail event over
    live_forecast_min: float = env("LIVE_FORECAST_MIN", 15, float)
    live_nws_min: float = env("LIVE_NWS_MIN", 2, float)
    daily_summary_time: str = env("DAILY_SUMMARY_TIME", "07:00")
    udp_stale_s: float = env("UDP_STALE_S", 150, float)
    offline_alert_min: float = env("OFFLINE_ALERT_MIN", 15, float)
    rapid_keep_hours: float = env("RAPID_KEEP_HOURS", 48, float)


CFG = Config()
REST = os.environ.get("TEMPEST_REST", "https://swd.weatherflow.com/swd/rest")
WS_URL = "wss://ws.weatherflow.com/swd/data"
STATIC = Path(__file__).parent / "static"

ALERT_LABELS = dict(hail_measured="Hail measured", hail_report="Hail report", shield_failed="Car cover plug FAILED", hail_shield_on="Hail shield ON", hail_shield_off="Hail shield OFF", hail="Hail at station", hail_warning="Hail warning", watch_start="Storm watch", storm_start="Storm started", storm_end="Storm ended",
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


def resolve_share_host(host):
    """Find the file server. Netgear's ReadySHARE name often isn't in DNS, so also try
    readyshare.routerlogin.net and finally the default gateway (the router serves ReadySHARE itself)."""
    tries = [host] + ([f"{host}.routerlogin.net"] if host.lower() == "readyshare" else [])
    for name in tries:
        try:
            return socket.gethostbyname(name)
        except OSError:
            pass
    if host.lower() == "readyshare":
        try:
            for line in open("/proc/net/route").read().splitlines()[1:]:
                f = line.split()
                if f[1] == "00000000" and f[2] != "00000000":
                    return socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
        except OSError:
            pass
    raise RuntimeError(f"can't find '{host}' on the network; set BACKUP_SMB_HOST to its IP address")


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
CREATE TABLE IF NOT EXISTS hail_frames (ts INTEGER PRIMARY KEY, visible INTEGER, new INTEGER, max_mm REAL,
  sizes TEXT, image TEXT);
CREATE TABLE IF NOT EXISTS daily (day TEXT, source TEXT, n INTEGER, t_lo REAL, t_hi REAL, t_avg REAL,
  h_lo REAL, h_hi REAL, h_avg REAL, rain_in REAL, gust_mph REAL, wind_mph REAL, p_avg REAL, uv_max REAL,
  strikes INTEGER, hail_min INTEGER, PRIMARY KEY (day, source));
CREATE TABLE IF NOT EXISTS yl_th (ts INTEGER, device TEXT, temp_c REAL, humidity REAL, battery INTEGER,
  PRIMARY KEY (device, ts));
CREATE TABLE IF NOT EXISTS hail_events (id INTEGER PRIMARY KEY AUTOINCREMENT, start_ts INTEGER, end_ts INTEGER,
  summary TEXT, image TEXT);
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
    yolink: dict = field(default_factory=dict)
    yl_state: dict = field(default_factory=dict)
    yl_error: str | None = None
    cam: dict = field(default_factory=lambda: dict(enabled=False, active=False, last_ts=None, error=None,
                                                    markers=0, event_id=None, last_new_ts=None, alerted_mm=0))
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
        n, ok, why = self.station_hail()
        if not ok:
            self.log_event("hail", f"Possible hail at station (unconfirmed) · {when} · {why}")
            self.push("hail", dict(ts=row["ts"], kind=kind, confirmed=False))
            asyncio.get_event_loop().call_soon(self.evaluate)
            return
        self.log_event("alert:hail", f"Hail at station: {kind} · {when} · {why}")
        asyncio.get_event_loop().create_task(self.send_ifttt(
            "hail", f"{kind} at {self.station.get('name') or 'your station'}", when, why))
        self.push("hail", dict(ts=row["ts"], kind=kind, confirmed=True))
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
        n, ok, _ = self.station_hail()
        if n:
            storm.insert(0, "hail confirmed" if ok else "possible hail (unconfirmed)")
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
    def nws_hail(self):
        return [a for a in self.nws if a.get("hail_in") is not None and not (a["hail_in"] and a["hail_in"] < CFG.shield_min_in)
                and ("Warning" in a["event"] or (CFG.shield_on_watch and "Watch" in a["event"]))]

    def station_hail(self):
        """(flagged minutes in last 10 min, confirmed?, explanation). The Tempest flags hail from impacts on its
        sensor; very heavy rain can do the same, so on its own it only counts as 'possible hail'."""
        n10 = self.db.q("SELECT COUNT(*) n FROM obs WHERE ts>=? AND precip_type IN (2,3)", (self.since(600),))[0]["n"]
        recent = self.db.q("SELECT COUNT(*) n FROM obs WHERE ts>=? AND precip_type IN (2,3)",
                           (self.since(max(CFG.storm_lookback_min, 15) * 60),))[0]["n"]
        if not recent:
            return 0, False, ""
        if not CFG.hail_confirm:
            return recent, True, "station hail sensor"
        if self.nws_hail():
            return recent, True, "station + NWS hail alert"
        if self.cam.get("last_new_ts") and time.time() - self.cam["last_new_ts"] < 15 * 60:
            return recent, True, "station + hail camera"
        d, _ = self.nearest_strike_mi(1800)
        if n10 >= CFG.hail_confirm_minutes and d is not None and d <= CFG.hail_confirm_lightning_mi:
            return recent, True, f"station ({n10} min flagged) + lightning {d:.0f} mi"
        return recent, False, f"{recent} min flagged, no NWS hail alert or nearby thunderstorm"

    def hail_threats(self):
        """Reasons hail is expected or happening right now (empty list = none)."""
        out = [f"{a['event']}" + (f" ({a['hail_in']:.2f} in hail)" if a["hail_in"] else " (hail)") for a in self.nws_hail()]
        n, ok, why = self.station_hail()
        if n and ok:
            out.append(f"hail confirmed: {why}")
        if self.cam.get("last_new_ts") and time.time() - self.cam["last_new_ts"] < 15 * 60:
            out.append(f"hail on camera (up to {self.hail_det.max_mm:.0f} mm)")
        return out

    def update_shield(self):
        sh, now = self.shield, time.time()
        if not CFG.shield_enabled:
            return
        if sh["mode"] in ("on", "off"):  # manual override from the dashboard
            want, why = sh["mode"] == "on", f"manual {sh['mode']}"
        else:
            threats = self.hail_threats()
            last_flag = self.db.q("SELECT MAX(ts) t FROM obs WHERE precip_type IN (2,3)")[0]["t"] or 0
            unconfirmed_until = last_flag + CFG.hail_unconfirmed_off_min * 60
            if threats:
                # backed up by NWS, the camera, or repeated flags with lightning: full protection
                sh["clear_since"] = None
                sh["confirmed"] = True
                sh.pop("unconfirmed_until", None)
                want, why = True, "; ".join(threats)
            elif now < unconfirmed_until and not sh.get("confirmed") and last_flag > sh.get("dismissed_flag", 0):
                # cautious: any hail flag from the Tempest inflates right away; without backup it comes down
                # HAIL_UNCONFIRMED_OFF_MIN after the last flag (a new flag restarts the clock)
                sh["unconfirmed_until"] = unconfirmed_until
                when = datetime.fromtimestamp(unconfirmed_until, ZoneInfo(CFG.tz)).strftime("%-I:%M %p")
                want, why = True, f"possible hail at station (unconfirmed), down at {when} unless confirmed"
            elif sh["on"] and not sh.get("confirmed") and not sh.get("clear_since"):
                want, why = False, "no backup for the station's hail reading"
            elif sh["on"]:
                # stay up while a thunderstorm is still close or a severe warning is active, then wait the off-delay
                d, _ = self.nearest_strike_mi(1800)
                severe = any(a["event"] in NWS_STORM_EVENTS for a in self.nws)
                if severe or (d is not None and d <= CFG.storm_lightning_mi):
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
        if want and sh["on"] and sh["mode"] == "auto" and why and not sh.get("clear_since"):
            sh["reason"] = why  # keep the shown reason current (e.g. after switching back from manual)
        if want != sh["on"]:
            sh.update(on=want, since=now if want else None, clear_since=None, reason=why if want else "")
            if not want:
                sh.pop("confirmed", None)
                sh.pop("unconfirmed_until", None)
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
        if CFG.yolink_uaid and CFG.yolink_secret and self.http:
            loop.create_task(self.yolink_set(on))
        url = CFG.shield_on_url if on else CFG.shield_off_url
        if url and self.http:
            async def hit():
                try:
                    async with self.http.post(url) as r:
                        log.info("shield %s webhook -> HTTP %s", "on" if on else "off", r.status)
                except Exception as e:
                    log.warning("shield webhook failed: %s", e)
            loop.create_task(hit())

    async def yolink_call(self, body, retry_auth=True):
        """Call the YoLink Open API v2 with a cached access token."""
        yl = self.yolink
        if not yl.get("token") or time.time() > yl.get("exp", 0) - 60:
            async with self.http.post(f"{CFG.yolink_host}/open/yolink/token", data=dict(
                    grant_type="client_credentials", client_id=CFG.yolink_uaid,
                    client_secret=CFG.yolink_secret)) as r:
                t = await r.json(content_type=None)
            if "access_token" not in t:
                raise RuntimeError(f"YoLink auth failed: {t.get('msg') or t}")
            yl.update(token=t["access_token"], exp=time.time() + int(t.get("expires_in", 3600)))
        body = dict(body, time=int(time.time() * 1000))
        async with self.http.post(f"{CFG.yolink_host}/open/yolink/v2/api", json=body,
                                  headers={"Authorization": f"Bearer {yl['token']}"}) as r:
            res = await r.json(content_type=None)
        if res.get("code") not in ("000000", None):
            if retry_auth and str(res.get("code", "")).startswith("0102"):  # token problem: re-auth once
                yl["token"] = None
                return await self.yolink_call(body, retry_auth=False)
            raise RuntimeError(f"YoLink {body.get('method')}: {res.get('code')} {res.get('desc') or res.get('msg')}")
        return res.get("data") or {}

    async def yolink_plug(self):
        yl = self.yolink
        if not yl.get("device"):
            devs = await self.yolink_devices(refresh=True)
            want = (CFG.yolink_device or "").strip().lower()
            plugs = [d for d in devs if "Outlet" in d.get("type", "")]
            match = [d for d in plugs if want in (d["name"].lower(), d["deviceId"].lower())] if want else plugs[:1]
            if not match:
                names = ", ".join(f"{d['name']} ({d['type']})" for d in devs) or "none"
                raise RuntimeError(f"YoLink plug '{CFG.yolink_device}' not found. Devices: {names}")
            yl["device"] = match[0]
            log.info("YoLink plug: %s (%s)", match[0]["name"], match[0]["type"])
        return yl["device"]

    async def yolink_set(self, on):
        """Turn the YoLink plug on/off, retrying, then read back its state."""
        last = None
        for attempt in range(5):
            try:
                d = await self.yolink_plug()
                await self.yolink_call({"method": f"{d['type']}.setState", "targetDevice": d["deviceId"],
                                        "token": d["token"], "params": {"state": "open" if on else "close"}})
                await asyncio.sleep(3)
                st = (await self.yolink_call({"method": f"{d['type']}.getState", "targetDevice": d["deviceId"],
                                              "token": d["token"]})).get("state")
                ok = (st in ("open", True)) == on
                self.shield["plug"] = dict(ok=ok, state=st, ts=int(time.time()), name=d["name"])
                self.log_event("shield", f"YoLink plug '{d['name']}' is {'ON' if st in ('open', True) else 'OFF'}"
                               + ("" if ok else " (did not change, retrying)"))
                self.push("shield", self.shield_state())
                if ok:
                    return
            except Exception as e:
                last = e
                self.yolink.pop("device", None)
                log.warning("YoLink attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(10 * (attempt + 1))
        self.shield["plug"] = dict(ok=False, error=str(last or "state did not change"), ts=int(time.time()))
        self.alert("shield_failed", f"Could not turn car cover plug {'ON' if on else 'OFF'}", str(last or "")[:120], "",
                   cooldown=False)
        self.push("shield", self.shield_state())

    def shield_state(self):
        sh = self.shield
        left = None
        if sh["on"] and sh["clear_since"]:
            left = max(0, CFG.shield_off_delay_min * 60 - (time.time() - sh["clear_since"]))
        elif sh["on"] and sh.get("unconfirmed_until") and sh["mode"] == "auto":
            left = max(0, sh["unconfirmed_until"] - time.time())
        return dict(enabled=CFG.shield_enabled, on=sh["on"], mode=sh["mode"], since=sh["since"],
                    confirmed=bool(sh.get("confirmed")),
                    reason=sh["reason"], off_in_s=left, threats=self.hail_threats(), plug=sh.get("plug"),
                    yolink=bool(CFG.yolink_uaid and CFG.yolink_secret))

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
            hailcam=self.hailcam_state(),
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
            self.station_created = st.get("created_epoch")
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

    # --------------------------------------------------------------- hail camera
    # --------------------------------------------------------------- YoLink temperature / humidity sensors
    TH_TYPES = ("THSensor",)

    async def yolink_devices(self, refresh=False):
        yl = self.yolink
        if refresh or not yl.get("devices") or time.time() - yl.get("devices_ts", 0) > 15 * 60:
            devs = (await self.yolink_call({"method": "Home.getDeviceList"})).get("devices", [])
            yl.update(devices=devs, devices_ts=time.time())
            for d in devs:  # renames in the YoLink app show up right away
                if d["deviceId"] in self.yl_state:
                    self.yl_state[d["deviceId"]]["name"] = d["name"]
            self.db.kv_set("yl_devices", [{k: d.get(k) for k in ("deviceId", "name", "type", "modelName")} for d in devs])
        return yl["devices"]

    async def yolink_th_loop(self):
        """Poll every YoLink temperature/humidity sensor and keep a history."""
        if not (CFG.yolink_uaid and CFG.yolink_secret):
            return
        await asyncio.sleep(5)
        while True:
            try:
                devs = [d for d in await self.yolink_devices() if d.get("type") in self.TH_TYPES]
                for d in devs:
                    data = await self.yolink_call({"method": f"{d['type']}.getState", "targetDevice": d["deviceId"],
                                                   "token": d["token"]})
                    st = data.get("state") or {}
                    ts = self.parse_report_ts(data.get("reportAt"))
                    if st.get("temperature") is None:
                        continue
                    self.db.x("INSERT OR IGNORE INTO yl_th VALUES (?,?,?,?,?)",
                              (ts, d["deviceId"], st.get("temperature"), st.get("humidity"),
                               int(st["battery"]) if str(st.get("battery", "")).isdigit() else None))
                    self.yl_state[d["deviceId"]] = dict(
                        id=d["deviceId"], name=d["name"], model=d.get("modelName"), online=data.get("online"),
                        ts=ts, temp_f=c_to_f(st.get("temperature")), humidity=st.get("humidity"),
                        battery=int(st["battery"]) if str(st.get("battery", "")).isdigit() else None,
                        alarm=[k for k, v in (st.get("alarm") or {}).items() if v is True],
                        temp_limit_f=[c_to_f((st.get("tempLimit") or {}).get(k)) for k in ("min", "max")],
                        humidity_limit=[(st.get("humidityLimit") or {}).get(k) for k in ("min", "max")])
                    await asyncio.sleep(1)
                self.yolink_record_changes()
                self.yl_error = None
                self.push("yolink", self.yolink_snapshot())
            except Exception as e:
                self.yl_error = str(e)[:200]
                log.warning("YoLink sensors: %s", e)
            await asyncio.sleep(CFG.yolink_poll_min * 60)

    @staticmethod
    def parse_report_ts(v):
        try:
            return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except Exception:
            return int(time.time())

    ALARM_TEXT = dict(lowBattery="Low battery", lowTemp="Too cold", highTemp="Too warm",
                      lowHumidity="Too dry", highHumidity="Too humid")

    def yolink_offline(self, s):
        return s.get("online") is False or (s.get("ts") and time.time() - s["ts"] > CFG.yolink_offline_hours * 3600)

    def yolink_record_changes(self):
        """Write sensor alarms and offline/online changes to the activity log (YoLink's app does the notifying)."""
        prev = self.db.kv_get("yl_alarm_state", {}) or {}
        for dev, st in self.yl_state.items():
            name, p = st["name"], prev.get(dev, {})
            t = f"{st['temp_f']:.1f}°F" if st.get("temp_f") is not None else ""
            h = f"{st['humidity']:.0f}%" if st.get("humidity") is not None else ""
            lim_t = st.get("temp_limit_f") or [None, None]
            lim_h = st.get("humidity_limit") or [None, None]
            detail = dict(lowTemp=f"{t} (limit {lim_t[0]:.0f}°F)" if lim_t[0] is not None else t,
                          highTemp=f"{t} (limit {lim_t[1]:.0f}°F)" if lim_t[1] is not None else t,
                          lowHumidity=f"{h} (limit {lim_h[0]:.0f}%)" if lim_h[0] is not None else h,
                          highHumidity=f"{h} (limit {lim_h[1]:.0f}%)" if lim_h[1] is not None else h,
                          lowBattery=f"battery {st.get('battery')}/4")
            now_al = set(a for a in st.get("alarm", []) if a in self.ALARM_TEXT)
            was_al = set(p.get("alarm", []))
            for a in sorted(now_al - was_al):
                self.log_event("yolink", f"{name}: {self.ALARM_TEXT[a]} — {detail.get(a, '')}", dict(device=dev, alarm=a))
            for a in sorted(was_al - now_al):
                self.log_event("yolink", f"{name}: {self.ALARM_TEXT[a]} cleared — now {t} {h}".strip(), dict(device=dev, alarm=a, cleared=True))
            off = bool(self.yolink_offline(st))
            if off and not p.get("offline"):
                last = datetime.fromtimestamp(st["ts"], ZoneInfo(CFG.tz)).strftime("%b %-d, %Y %-I:%M %p") if st.get("ts") else "never"
                self.log_event("yolink", f"{name}: offline — last report {last}", dict(device=dev, offline=True))
            elif not off and p.get("offline"):
                self.log_event("yolink", f"{name}: back online — {t} {h}".strip(), dict(device=dev, offline=False))
            prev[dev] = dict(alarm=sorted(now_al), offline=off)
        self.db.kv_set("yl_alarm_state", prev)

    def yolink_snapshot(self):
        since = self.since(24 * 3600)
        out = []
        for dev_id, s in sorted(self.yl_state.items(), key=lambda kv: (bool(self.yolink_offline(kv[1])), kv[1]["name"].lower())):
            r = self.db.q("SELECT MIN(temp_c) lo, MAX(temp_c) hi, MIN(humidity) hlo, MAX(humidity) hhi FROM yl_th "
                          "WHERE device=? AND ts>=?", (dev_id, since))[0]
            out.append(dict(s, offline=bool(self.yolink_offline(s)), hi24_f=c_to_f(r["hi"]), lo24_f=c_to_f(r["lo"]), hum_lo24=r["hlo"], hum_hi24=r["hhi"]))
        return dict(enabled=bool(CFG.yolink_uaid and CFG.yolink_secret), error=self.yl_error, sensors=out,
                    outdoor=dict(temp_f=c_to_f((self.latest or {}).get("temp_c")), humidity=(self.latest or {}).get("rh")))

    def hailcam_setup(self):
        if not (CFG.hailcam_host or CFG.hailcam_rtsp):
            return
        from hailcam import HailDetector, PlateConfig, ReolinkCamera
        w, h = (float(x) for x in CFG.hailcam_plate.lower().split("x"))
        self.plate_cfg = PlateConfig(width_mm=w, height_mm=h, marker_mm=CFG.hailcam_marker_mm,
                                     inset_mm=CFG.hailcam_inset_mm, min_mm=CFG.hailcam_min_mm)
        self.hail_det = HailDetector(self.plate_cfg)
        self.camera = ReolinkCamera(self.http, CFG.hailcam_host, CFG.hailcam_user, CFG.hailcam_password,
                                    CFG.hailcam_channel, CFG.hailcam_https, CFG.hailcam_rtsp)
        self.cam_dir = Path(CFG.db_path).parent / "hailcam"
        self.cam_dir.mkdir(parents=True, exist_ok=True)
        self.cam["enabled"] = True
        log.info("hail camera enabled (%s), plate %s mm", CFG.hailcam_host or "RTSP", CFG.hailcam_plate)

    def hailcam_should_run(self):
        recent_station_hail = self.db.q("SELECT COUNT(*) n FROM obs WHERE ts>=? AND precip_type IN (2,3)",
                                        (self.since(15 * 60),))[0]["n"]
        return self.live or recent_station_hail or any(a.get("hail_in") is not None for a in self.nws)

    async def hailcam_capture(self, detector, count=True):
        import cv2
        frame = await self.camera.snapshot()
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, detector.analyze, frame, time.time())
        self.cam.update(last_ts=int(res.ts), error=res.error, markers=res.markers)
        if res.annotated is not None:
            cv2.imwrite(str(self.cam_dir / "latest.jpg"), res.annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
        cv2.imwrite(str(self.cam_dir / "latest_raw.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return res, frame

    async def hailcam_loop(self):
        import cv2
        if not self.cam["enabled"]:
            return
        while True:
            run = self.hailcam_should_run() or self.cam.get("test_until", 0) > time.time()
            if not run:
                if self.cam["active"]:
                    self.hailcam_finish()
                await asyncio.sleep(10)
                continue
            if not self.cam["active"]:
                self.cam["active"] = True
                self.hail_det.reset()
                self.cam.update(event_id=None, last_new_ts=None, alerted_mm=0)
                self.log_event("hailcam", "Hail camera watching the plate")
            try:
                res, frame = await self.hailcam_capture(self.hail_det)
                if res.ok and res.new:
                    self.on_camera_hail(res)
                if (self.cam["last_new_ts"] and time.time() - self.cam["last_new_ts"] > CFG.hailcam_end_min * 60
                        and not self.hailcam_should_run()):
                    self.hailcam_finish()
                self.push("hailcam", self.hailcam_state())
            except Exception as e:
                self.cam["error"] = str(e)[:200]
                log.warning("hail camera: %s", e)
                await asyncio.sleep(10)
            await asyncio.sleep(CFG.hailcam_interval_s)

    def on_camera_hail(self, res):
        import cv2
        now = time.time()
        first = self.cam["last_new_ts"] is None
        self.cam["last_new_ts"] = now
        stamp = datetime.fromtimestamp(now, ZoneInfo(CFG.tz))
        rel = f"{stamp:%Y-%m-%d}/{stamp:%H%M%S}.jpg"
        (self.cam_dir / f"{stamp:%Y-%m-%d}").mkdir(exist_ok=True)
        if res.annotated is not None:
            cv2.imwrite(str(self.cam_dir / rel), res.annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
        sizes = [round(s["d_mm"], 1) for s in res.stones]
        self.db.x("INSERT OR REPLACE INTO hail_frames VALUES (?,?,?,?,?,?)",
                  (int(now), len(res.stones), res.new, max(sizes or [0]), json.dumps(sizes), rel))
        if first:
            self.cam["event_id"] = self.db.x("INSERT INTO hail_events (start_ts, image) VALUES (?,?)", (int(now), rel))
        from hailcam import hail_label
        mx = self.hail_det.max_mm
        if mx >= CFG.hailcam_alert_mm and (first or mx >= self.cam["alerted_mm"] + 5):
            self.cam["alerted_mm"] = mx
            self.alert("hail_measured", f"Hail on camera: up to {mx:.0f} mm ({mx / 25.4:.2f} in, {hail_label(mx)})",
                       f"{self.hail_det.total_new} stones counted so far", "", cooldown=False)
        if first:
            asyncio.get_event_loop().call_soon(self.evaluate)

    def hailcam_finish(self):
        self.cam["active"] = False
        if self.cam.get("event_id"):
            from hailcam import hail_label
            sm = self.hail_det.summary()
            best = self.db.q("SELECT image FROM hail_frames WHERE ts>=? ORDER BY max_mm DESC LIMIT 1",
                             (int(sm["started"] or 0),))
            self.db.x("UPDATE hail_events SET end_ts=?, summary=?, image=COALESCE(?, image) WHERE id=?",
                      (int(time.time()), json.dumps(sm), best[0]["image"] if best else None, self.cam["event_id"]))
            self.alert("hail_report", f"{sm['count']} stones, largest {sm['max_mm']:.0f} mm ({hail_label(sm['max_mm'])})",
                       f"median {sm['median_mm']} mm · {sm['per_ft2']} per sq ft", "", cooldown=False)
        self.log_event("hailcam", "Hail camera idle")
        self.cam["event_id"] = None
        self.push("hailcam", self.hailcam_state())

    def hailcam_state(self):
        c = dict(self.cam)
        c.pop("test_until", None)
        if c["enabled"]:
            c["event"] = self.hail_det.summary() if c["active"] else None
            c["plate"] = CFG.hailcam_plate
        return c

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

    # --------------------------------------------------------------- long-term history
    def day_bounds(self, day):
        """(start_ts, end_ts) of a local calendar day 'YYYY-MM-DD'."""
        tz = ZoneInfo(CFG.tz)
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
        return int(d.timestamp()), int((d + timedelta(days=1)).replace(tzinfo=tz).timestamp())

    def rollup_day(self, day):
        """Summarise one local day for the outdoor station and every YoLink sensor into the daily table."""
        s, e = self.day_bounds(day)
        o = self.db.q("""SELECT COUNT(*) n, MIN(temp_c) tl, MAX(temp_c) th, AVG(temp_c) ta, MIN(rh) hl, MAX(rh) hh, AVG(rh) ha,
                         SUM(rain_mm) r, MAX(wind_gust) g, AVG(wind_avg) w, AVG(pressure_mb) p, MAX(uv) uv,
                         SUM(CASE WHEN precip_type IN (2,3) THEN 1 ELSE 0 END) hail FROM obs WHERE ts>=? AND ts<?""", (s, e))[0]
        if o["n"]:
            st = self.db.q("SELECT COUNT(*) n FROM strikes WHERE ts>=? AND ts<?", (s, e))[0]["n"]
            p = sea_level_mb(o["p"], self.elevation, o["ta"])
            self.db.x("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                day, "out", o["n"], c_to_f(o["tl"]), c_to_f(o["th"]), c_to_f(o["ta"]), o["hl"], o["hh"], o["ha"],
                mm_to_in(o["r"] or 0), ms_to_mph(o["g"]), ms_to_mph(o["w"]), mb_to_inhg(p), o["uv"], st, o["hail"]))
        for r in self.db.q("""SELECT device, COUNT(*) n, MIN(temp_c) tl, MAX(temp_c) th, AVG(temp_c) ta, MIN(humidity) hl,
                              MAX(humidity) hh, AVG(humidity) ha FROM yl_th WHERE ts>=? AND ts<? GROUP BY device""", (s, e)):
            self.db.x("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                day, r["device"], r["n"], c_to_f(r["tl"]), c_to_f(r["th"]), c_to_f(r["ta"]), r["hl"], r["hh"], r["ha"],
                None, None, None, None, None, None, None))

    def rollup_missing(self, recent_days=2):
        """Fill the daily table for every day that has raw data but no summary yet (and redo the last few days)."""
        first = min([x for x in (self.db.q("SELECT MIN(ts) t FROM obs")[0]["t"],
                                 self.db.q("SELECT MIN(ts) t FROM yl_th")[0]["t"]) if x] or [time.time()])
        tz = ZoneInfo(CFG.tz)
        d = datetime.fromtimestamp(first, tz).date()
        today = datetime.now(tz).date()
        have = {r["day"] for r in self.db.q("SELECT DISTINCT day FROM daily")}
        n = 0
        while d <= today:
            key = d.isoformat()
            if key not in have or (today - d).days <= recent_days:
                self.rollup_day(key)
                n += 1
            d += timedelta(days=1)
        return n

    async def history_backfill_loop(self):
        """Download the station's full 1-minute history from Tempest, one day per request, oldest gaps first."""
        if not (CFG.history_backfill and CFG.token):
            return
        await asyncio.sleep(60)
        while not self.device_id:
            await asyncio.sleep(60)
        tz = ZoneInfo(CFG.tz)
        state = self.db.kv_get("backfill", {}) or {}
        if state.get("done"):
            return
        created = getattr(self, "station_created", None)
        day = datetime.fromisoformat(state["next"]).date() if state.get("next") else \
            datetime.now(tz).date() - timedelta(days=1)
        empty_run = 0
        log.info("history backfill: downloading Tempest history backwards from %s", day)
        while True:
            s, e = self.day_bounds(day.isoformat())
            if created and e < created:
                break
            have = self.db.q("SELECT COUNT(*) n FROM obs WHERE ts>=? AND ts<?", (s, e))[0]["n"]
            if have < 1300:  # a full day is 1440 one-minute observations
                try:
                    data = await self.rest(f"/observations/device/{self.device_id}", time_start=s, time_end=e - 1)
                    rows = data.get("obs") or []
                    for o in rows:
                        self.ingest_obs(o, "rest")
                    empty_run = 0 if rows else empty_run + 1
                except Exception as ex:
                    log.warning("history backfill %s: %s", day, ex)
                    await asyncio.sleep(60)
                    continue
                self.rollup_day(day.isoformat())
                await asyncio.sleep(CFG.history_backfill_delay_s)
            else:
                empty_run = 0
            if not created and empty_run >= 45:  # 45 days with nothing = before the station existed
                break
            day -= timedelta(days=1)
            self.db.kv_set("backfill", dict(next=day.isoformat(), done=False))
            await asyncio.sleep(0)
        self.db.kv_set("backfill", dict(next=day.isoformat(), done=True))
        self.log_event("history", f"Tempest history download complete (back to {day + timedelta(days=1)})")
        log.info("history backfill complete")

    def backup_db(self):
        """Nightly copy of the database: last BACKUP_KEEP_DAYS days plus one per month."""
        bdir = Path(CFG.db_path).parent / "backups"
        bdir.mkdir(exist_ok=True)
        today = datetime.now(ZoneInfo(CFG.tz)).date()
        dst = bdir / f"tempest-{today}.db"
        out = sqlite3.connect(dst)
        with out:
            self.db.c.backup(out)
        out.close()
        files = sorted(bdir.glob("tempest-*.db"))
        monthly = {}
        for f in files:
            monthly.setdefault(f.stem[8:15], f)  # first backup of each month
        keep = set(files[-CFG.backup_keep_days:]) | set(list(monthly.values())[-CFG.backup_keep_months:])
        for f in files:
            if f not in keep:
                f.unlink()
        log.info("database backed up to %s", dst)
        if CFG.backup_smb_path:
            self.backup_to_share(bdir)

    def backup_to_share(self, bdir):
        """Mirror data/backups/ to a network share (e.g. the router's ReadySHARE drive). SMB 2 and 3."""
        import smbclient
        parts = CFG.backup_smb_path.replace("\\", "/").strip("/").split("/")
        host, share, folder = parts[0], parts[1], "/".join(parts[2:])
        status = dict(ts=int(time.time()), target=CFG.backup_smb_path, ok=False)
        try:
            ip = CFG.backup_smb_host or resolve_share_host(host)
            # guest logins have no signing key, so SMB 3's negotiate check has to be skipped for them
            smbclient.ClientConfig(require_secure_negotiate=bool(CFG.backup_smb_password))
            smbclient.register_session(ip, username=CFG.backup_smb_user or "guest",
                                       password=CFG.backup_smb_password or "", port=CFG.backup_smb_port,
                                       require_signing=False, connection_timeout=30)
            root = "\\".join(["", "", ip, share] + [x for x in folder.split("/") if x])
            smbclient.makedirs(root, exist_ok=True, port=CFG.backup_smb_port)
            remote = {}
            for name in smbclient.listdir(root, port=CFG.backup_smb_port):
                st = smbclient.stat(f"{root}\\{name}", port=CFG.backup_smb_port)
                if not (st.st_mode & 0o040000):
                    remote[name] = st.st_size
            local = {f.name: f for f in sorted(bdir.glob("tempest-*.db"))}
            sent = removed = 0
            for name, f in local.items():
                if remote.get(name) != f.stat().st_size:
                    with open(f, "rb") as src, smbclient.open_file(f"{root}\\{name}", mode="wb", port=CFG.backup_smb_port) as dst:
                        while chunk := src.read(1 << 20):
                            dst.write(chunk)
                    sent += 1
            for name in remote:
                if name.startswith("tempest-") and name.endswith(".db") and name not in local:
                    smbclient.remove(f"{root}\\{name}", port=CFG.backup_smb_port)
                    removed += 1
            if "README.txt" not in remote:
                with smbclient.open_file(f"{root}\\README.txt", mode="w", port=CFG.backup_smb_port) as fh:
                    fh.write("Tempest Monitor database backups (SQLite). Open with DB Browser for SQLite, or copy one back to\n"
                             "the EdgeXpert as data/tempest.db to restore. Managed automatically: daily copies plus one per month.\n")
            status.update(ok=True, files=len(local), sent=sent, removed=removed, server=ip)
            log.info("backups mirrored to %s (%d sent, %d removed)", CFG.backup_smb_path, sent, removed)
        except Exception as e:
            status["error"] = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            log.warning("network backup to %s failed: %s", CFG.backup_smb_path, e)
            self.log_event("backup", f"Network backup failed: {status['error']}")
        self.db.kv_set("backup_status", status)
        return status

    async def daily_loop(self):
        hh, mm = map(int, CFG.daily_summary_time.split(":"))
        while True:
            now = datetime.now(ZoneInfo(CFG.tz))
            nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep((nxt - now).total_seconds())
            await self.send_daily()
            try:
                self.rollup_missing()
                await asyncio.get_running_loop().run_in_executor(None, self.backup_db)
            except Exception as e:
                log.warning("nightly history/backup: %s", e)
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

    async def wind_rose(req):
        """Share of time the wind came from each of 16 directions, split by speed band (mph)."""
        hours = min(float(req.query.get("hours", 24)), 24 * 31)
        bands = [0, 5, 10, 15, 20]  # lower edges, mph; calm = average under 1 mph
        rows = mon.db.q("SELECT wind_avg, wind_dir FROM obs WHERE ts>=? AND wind_avg IS NOT NULL",
                        (mon.since(hours * 3600),))
        grid = [[0] * len(bands) for _ in range(16)]
        calm = 0
        for x in rows:
            mph = ms_to_mph(x["wind_avg"])
            if mph < 1 or x["wind_dir"] is None:
                calm += 1
                continue
            sec = int((x["wind_dir"] % 360) / 22.5 + 0.5) % 16
            b = max(i for i, lo in enumerate(bands) if mph >= lo)
            grid[sec][b] += 1
        n = len(rows) or 1
        return web.json_response(dict(
            total=len(rows), calm_pct=100 * calm / n, bands=bands,
            sectors=[dict(dir=cardinal(i * 22.5), deg=i * 22.5, pct=[100 * c / n for c in grid[i]]) for i in range(16)]))

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
        kind = req.query.get("kind")
        return web.json_response(mon.db.q("SELECT ts, kind, message FROM events WHERE (? IS NULL OR kind=?) "
                                          "ORDER BY id DESC LIMIT ?", (kind, kind, int(req.query.get("limit", 50)))))

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
    r.add_get("/api/wind_rose", wind_rose)
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
                # dismiss the current hail flags; only a newer flag starts a new cautious window
                mon.shield["dismissed_flag"] = mon.db.q("SELECT MAX(ts) t FROM obs WHERE precip_type IN (2,3)")[0]["t"] or 0
                mon.shield.pop("confirmed", None)
            mon.log_event("shield", f"Hail shield set to {mode.upper()} from the dashboard")
            mon.update_shield()
            mon.push("shield", mon.shield_state())
        return web.json_response(mon.shield_state())

    r.add_get("/api/shield", shield)
    r.add_post("/api/shield", shield)

    async def hailcam_status(req):
        st = mon.hailcam_state()
        st["events"] = []
        for e in mon.db.q("SELECT * FROM hail_events ORDER BY id DESC LIMIT 10"):
            e["summary"] = json.loads(e["summary"]) if e["summary"] else None
            st["events"].append(e)
        return web.json_response(st)

    async def hailcam_image(req):
        if not mon.cam["enabled"]:
            raise web.HTTPNotFound()
        name = req.query.get("f") or ("latest_raw.jpg" if req.query.get("raw") else "latest.jpg")
        path = (mon.cam_dir / name).resolve()
        if mon.cam_dir.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def hailcam_test(req):
        """One-off capture to check aim, focus and that all 4 markers are seen. Doesn't count stones."""
        if not mon.cam["enabled"]:
            return web.json_response(dict(ok=False, error="HAILCAM_HOST not set"), status=400)
        from hailcam import HailDetector
        try:
            res, _ = await mon.hailcam_capture(HailDetector(mon.plate_cfg))
        except Exception as e:
            return web.json_response(dict(ok=False, error=str(e)[:200]), status=502)
        mon.push("hailcam", mon.hailcam_state())
        return web.json_response(dict(ok=res.ok, error=res.error, markers=res.markers,
                                      stones=[round(x["d_mm"], 1) for x in res.stones]))

    r.add_get("/api/hailcam", hailcam_status)

    async def yl_sensors(_):
        return web.json_response(mon.yolink_snapshot())

    async def yl_history(req):
        hours = min(float(req.query.get("hours", 24)), 24 * 31)
        since = mon.since(hours * 3600)
        dev = req.query.get("device")
        rows = mon.db.q("SELECT ts, device, temp_c, humidity FROM yl_th WHERE ts>=? AND (? IS NULL OR device=?) ORDER BY ts",
                        (since, dev, dev)) if dev != "out" else []
        series = {}
        for x in rows:
            series.setdefault(x["device"], []).append(dict(ts=x["ts"], temp_f=c_to_f(x["temp_c"]), humidity=x["humidity"]))
        outdoor = mon.db.q("SELECT ts, temp_c, rh FROM obs WHERE ts>=? ORDER BY ts", (since,))
        outdoor = bucket([dict(ts=o["ts"], temp_f=c_to_f(o["temp_c"]), humidity=o["rh"]) for o in outdoor], 600)
        return web.json_response(dict(series={k: bucket(v, 600) for k, v in series.items()}, outdoor=outdoor))

    async def yl_refresh(_):
        try:
            await mon.yolink_devices(refresh=True)
        except Exception as e:
            return web.json_response(dict(ok=False, error=str(e)[:200]), status=502)
        mon.push("yolink", mon.yolink_snapshot())
        return web.json_response(dict(ok=True, devices=mon.db.kv_get("yl_devices", [])))

    # ---------------- compare two temperature sources (Outdoor vs Attic etc.)
    def pairs():
        saved = mon.db.kv_get("compare_pairs", {}) or {}
        out = {}
        for dev, st in mon.yl_state.items():
            out[dev] = saved.get(dev, "out")
        if "out" in saved:
            out["out"] = saved["out"]
        else:
            attic = next((d for d, st in mon.yl_state.items() if "attic" in st["name"].lower()), None)
            out["out"] = attic
        return out

    def series(src, since):
        if src == "out":
            return [(r["ts"], c_to_f(r["temp_c"])) for r in
                    mon.db.q("SELECT ts, temp_c FROM obs WHERE ts>=? AND temp_c IS NOT NULL ORDER BY ts", (since,))]
        return [(r["ts"], c_to_f(r["temp_c"])) for r in
                mon.db.q("SELECT ts, temp_c FROM yl_th WHERE device=? AND ts>=? AND temp_c IS NOT NULL ORDER BY ts", (src, since))]

    async def compare_pairs(req):
        if req.method == "POST":
            body = await req.json()
            saved = mon.db.kv_get("compare_pairs", {}) or {}
            saved[body["id"]] = body.get("other") or None
            mon.db.kv_set("compare_pairs", saved)
        return web.json_response(pairs())

    async def compare(req):
        """Temperature difference a minus b, matched at the sparser source's timestamps (nearest reading within 20 min)."""
        import bisect
        a, b = req.query["a"], req.query["b"]
        hours = min(float(req.query.get("hours", 24)), 24 * 366)
        since = mon.since(hours * 3600 + 1200)
        A, B = series(a, since), series(b, since)
        if not A or not B:
            return web.json_response(dict(points=[], a=a, b=b))
        base, other, flip = (A, B, False) if len(A) <= len(B) else (B, A, True)
        ots = [t for t, _ in other]
        pts = []
        for t, v in base:
            i = bisect.bisect_left(ots, t)
            best = min((j for j in (i - 1, i) if 0 <= j < len(ots)), key=lambda j: abs(ots[j] - t))
            if abs(ots[best] - t) > 1200:
                continue
            va, vb = (other[best][1], v) if flip else (v, other[best][1])
            pts.append(dict(ts=t, a=va, b=vb, delta=va - vb))
        return web.json_response(dict(points=bucket(pts, 900), a=a, b=b))

    r.add_get("/api/compare/pairs", compare_pairs)
    r.add_post("/api/compare/pairs", compare_pairs)
    r.add_get("/api/compare", compare)
    r.add_get("/api/yolink/sensors", yl_sensors)
    r.add_get("/api/yolink/history", yl_history)
    r.add_post("/api/yolink/refresh", yl_refresh)

    # ---------------- long-term history
    METRICS = {"temp": ("°F", "t"), "humidity": ("%", "h"), "rain": ("in", "rain_in"), "wind": ("mph", "gust_mph"),
               "pressure": ("inHg", "p_avg")}

    async def hist_sources(_):
        names = {d["deviceId"]: d["name"] for d in (mon.db.kv_get("yl_devices", []) or [])}
        out = []
        for r in mon.db.q("SELECT source, MIN(day) first, MAX(day) last, COUNT(*) days FROM daily GROUP BY source"):
            src = r["source"]
            out.append(dict(id=src, name="Outdoor (Tempest)" if src == "out" else names.get(src, src),
                            first=r["first"], last=r["last"], days=r["days"],
                            metrics=list(METRICS) if src == "out" else ["temp", "humidity"]))
        out.sort(key=lambda x: (x["id"] != "out", x["name"].lower()))
        return web.json_response(dict(sources=out, backfill=mon.db.kv_get("backfill", {}),
                                      backup=dict(mon.db.kv_get("backup_status", {}) or {}, target=CFG.backup_smb_path)))

    async def hist_yoy(req):
        src, metric = req.query.get("source", "out"), req.query.get("metric", "temp")
        if metric not in METRICS:
            raise web.HTTPBadRequest()
        unit, col = METRICS[metric]
        mon.rollup_day(datetime.now(ZoneInfo(CFG.tz)).date().isoformat())
        cols = {"t": "t_lo lo, t_hi hi, t_avg avg", "h": "h_lo lo, h_hi hi, h_avg avg"}.get(col, f"{col} v")
        rows = mon.db.q(f"SELECT day, {cols} FROM daily WHERE source=? ORDER BY day", (src,))
        years = {}
        for r in rows:
            y, md = r["day"][:4], r["day"][5:]
            r = dict(r, md=md)
            r.pop("day")
            years.setdefault(y, []).append(r)
        return web.json_response(dict(unit=unit, metric=metric, years=years))

    async def hist_summary(req):
        src = req.query.get("source", "out")
        mon.rollup_day(datetime.now(ZoneInfo(CFG.tz)).date().isoformat())
        yearly = mon.db.q("""SELECT substr(day,1,4) year, COUNT(*) days, MIN(t_lo) t_lo, MAX(t_hi) t_hi, AVG(t_avg) t_avg,
              AVG(h_avg) h_avg, SUM(rain_in) rain, MAX(gust_mph) gust, SUM(strikes) strikes,
              SUM(CASE WHEN rain_in>=0.01 THEN 1 ELSE 0 END) rain_days, SUM(CASE WHEN hail_min>0 THEN 1 ELSE 0 END) hail_days,
              SUM(CASE WHEN t_hi>=100 THEN 1 ELSE 0 END) days_100, SUM(CASE WHEN t_lo<=32 THEN 1 ELSE 0 END) freeze_days
              FROM daily WHERE source=? GROUP BY year ORDER BY year""", (src,))
        monthly = mon.db.q("""SELECT substr(day,1,4) year, substr(day,6,2) month, AVG(t_avg) t_avg, MAX(t_hi) t_hi, MIN(t_lo) t_lo,
              AVG(h_avg) h_avg, SUM(rain_in) rain, COUNT(*) days FROM daily WHERE source=? GROUP BY year, month""", (src,))
        hot = mon.db.q("SELECT day, t_hi FROM daily WHERE source=? AND t_hi IS NOT NULL ORDER BY t_hi DESC LIMIT 1", (src,))
        cold = mon.db.q("SELECT day, t_lo FROM daily WHERE source=? AND t_lo IS NOT NULL ORDER BY t_lo LIMIT 1", (src,))
        wet = mon.db.q("SELECT day, rain_in FROM daily WHERE source=? AND rain_in>0 ORDER BY rain_in DESC LIMIT 1", (src,))
        windy = mon.db.q("SELECT day, gust_mph FROM daily WHERE source=? AND gust_mph IS NOT NULL ORDER BY gust_mph DESC LIMIT 1", (src,))
        return web.json_response(dict(yearly=yearly, monthly=monthly, records=dict(
            hottest=hot[0] if hot else None, coldest=cold[0] if cold else None,
            wettest=wet[0] if wet else None, windiest=windy[0] if windy else None)))

    async def hist_export(req):
        """CSV of the daily table, for Excel."""
        src = req.query.get("source")
        rows = mon.db.q("SELECT * FROM daily WHERE (? IS NULL OR source=?) ORDER BY source, day", (src, src))
        import csv, io
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        return web.Response(text=buf.getvalue(), content_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="daily-{src or "all"}.csv"'})

    r.add_get("/api/history/sources", hist_sources)
    r.add_get("/api/history/yoy", hist_yoy)
    r.add_get("/api/history/summary", hist_summary)
    r.add_get("/api/history/export.csv", hist_export)

    async def backup_now(_):
        await asyncio.get_running_loop().run_in_executor(None, mon.backup_db)
        return web.json_response(mon.db.kv_get("backup_status", {}) or dict(ok=True, target=None))

    r.add_post("/api/backup", backup_now)

    import bills as bills_mod
    import rachio as rachio_mod
    bills_mod.register(r, web, mon, mon.bills)
    rachio_mod.register(r, web, mon, mon.rachio)
    import properties as properties_mod
    properties_mod.register(r, web, mon, mon.properties)
    r.add_get("/api/hailcam/image", hailcam_image)
    r.add_post("/api/hailcam/test", hailcam_test)
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
    if CFG.yolink_uaid and CFG.yolink_secret:
        try:
            d = await mon.yolink_plug()
            st = (await mon.yolink_call({"method": f"{d['type']}.getState", "targetDevice": d["deviceId"],
                                         "token": d["token"]})).get("state")
            log.info("YoLink connected: car cover plug '%s' (%s) is currently %s", d["name"], d["type"], st)
        except Exception as e:
            log.warning("YoLink check failed: %s", e)
    await mon.backfill()

    import bills as bills_mod
    import rachio as rachio_mod
    mon.bills = bills_mod.Bills(mon, CFG, resolve_share_host)
    mon.rachio = rachio_mod.Rachio(mon, CFG)
    import properties as properties_mod
    mon.properties = properties_mod.Properties(mon, CFG, resolve_share_host)
    runner = web.AppRunner(build_app(mon))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CFG.http_port).start()
    log.info("dashboard on http://0.0.0.0:%d", CFG.http_port)
    n = mon.rollup_missing()
    log.info("daily history: %d days summarised", n)
    mon.hailcam_setup()
    await asyncio.gather(mon.udp_loop(), mon.ws_loop(), mon.scheduler(), mon.daily_loop(), mon.hailcam_loop(),
                         mon.yolink_th_loop(), mon.history_backfill_loop(),
                         mon.bills.inbox_loop(), mon.bills.smt_loop(), mon.rachio.loop(),
                         mon.properties.inbox_loop())


if __name__ == "__main__":
    asyncio.run(main())
