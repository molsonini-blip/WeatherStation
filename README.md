# Tempest Monitor

An always-on service for the **Malaga Hills Dr** Tempest station (217588). It records everything,
detects storms on its own, goes live when one is coming, serves a dashboard, and talks to IFTTT.

## How it works

See **[docs/architecture.html](docs/architecture.html)** for the block diagram and the protocol used at each stage
(download and open it in a browser).


| Mode | When | What happens |
|---|---|---|
| **Normal** | Quiet weather | 1-minute observations stored. Forecast checked every 3 h, NWS alerts every 15 min. Daily summary at 7:00 am. Dashboard refreshes every 5 min. |
| **Watch** ("right before") | Lightning within 40 mi, pressure down 2 mb in 3 h, ≥60% rain chance in the next 3 h, or an NWS watch/advisory | Goes live: 3-second wind recorded, NWS every 2 min, forecast every 15 min, dashboard streams in real time. |
| **Storm** | Rain falling, lightning within 15 mi, gusts ≥30 mph, or an NWS warning | Everything at max rate until **45 min** after the last trigger, then a storm summary (duration, rain, max gust, strikes, temp drop) is logged and sent to IFTTT. |

**Data sources**, in order:

1. **UDP from your hub (LAN).** It's free and unlimited: wind every 3 s, observations every minute, and rain-start and lightning events the moment they happen.
2. **Tempest cloud WebSocket.** This is the automatic fallback when no UDP arrives for 2.5 min, and it switches back when UDP returns.
3. **Tempest REST API.** Used for the forecast, station info, and backfilling gaps after a restart (up to 24 h).
4. **NWS (api.weather.gov).** Official watches and warnings for your exact location.

> **VLANs:** UDP broadcasts don't cross VLANs. Run this on the same VLAN as the Tempest hub,
> or add a UDP broadcast relay for port 50222. If it can't hear the hub, it still works through the cloud WebSocket.

## Setup (Docker)

```bash
cp .env.example .env        # paste your TEMPEST_TOKEN (and IFTTT_KEY)
docker compose up -d --build
docker compose logs -f      # look for "station Malaga Hills Dr ... serial ST-..."
```

Open `http://<server-ip>:8088`. Data lives in `./data/tempest.db` (SQLite).

There's also a systemd unit (`tempest-monitor.service`) if you'd rather not use Docker.

**Test without waiting for weather:**

```bash
docker exec tempest-monitor python /tools/simulate_udp.py --host 127.0.0.1 --scenario storm --speed 15
```

This runs about 80 seconds: calm, then distant lightning (watch), then a storm, then clearing.
Simulated packets use serial `ST-SIMULATOR`. If `TEMPEST_SERIAL` is set, or the serial is auto-detected from your token,
the monitor ignores them. To run the test, temporarily set `TEMPEST_SERIAL=ST-SIMULATOR`, and set
`STORM_CLEAR_MIN=0.5 STORM_LOOKBACK_MIN=0.3 WATCH_LOOKBACK_MIN=0.3 WATCH_CLEAR_MIN=0.3` so the storm clears quickly.

## IFTTT

### 1. Webhooks from the monitor

Get your key at **ifttt.com/maker_webhooks** → Documentation, and put it in `.env` as `IFTTT_KEY`.
Then create applets with **If: Webhooks → Receive a web request**, using these event names.
Each event sends `value1`, `value2` and `value3`, which you can use in notifications.

| Event | Fires when | value1 / value2 / value3 |
|---|---|---|
| `tempest_watch_start` | Storm likely soon | reasons |
| `tempest_storm_start` | Storm begins | reasons / temp / wind |
| `tempest_storm_end` | Storm over | duration / rain + max gust / strikes |
| `tempest_all_clear` | Back to normal | – |
| `tempest_hail_warning` | NWS warning/watch mentions hail (radar-indicated or forecast, usually minutes ahead) | size + name, e.g. "1.75 in hail possible (golf ball)" / alert + expiry / threat |
| `tempest_hail` | Your Tempest detects hail hitting the sensor | what was detected / time / rain rate + gust |
| `tempest_lightning_close` | Strike ≤ 8 mi | distance / time |
| `tempest_high_wind` | Gust ≥ 40 mph | gust / direction / avg |
| `tempest_heavy_rain` | Rate ≥ 1 in/hr | rate / today's total |
| `tempest_nws_alert` | New NWS watch/warning | event / headline / severity |
| `tempest_daily` | 7:00 am | yesterday's hi/lo/rain / gust + strikes / today's forecast |
| `tempest_offline` | No data for 15 min | – |
| `tempest_test` | "Send IFTTT test" button on the dashboard | – |

Alerts that repeat (wind, rain, lightning) have a 30-minute cooldown; station hail has a 15-minute cooldown (`HAIL_COOLDOWN_MIN`).
NWS alerts are checked every 5 min normally and every 2 min during a storm watch. Set `ALERT_HAIL_MIN_IN=1.0` to only be told about quarter-size hail or bigger.
The Tempest's hail detection comes from its haptic sensor, and very heavy rain can occasionally be mistaken for hail.

Ideas: storm_start → close the garage and turn the porch lights on. lightning_close → turn off the
outdoor smart plugs. storm_end → push a notification with the totals. high_wind → retract the awning.

### 2. Native Tempest applets (no code)

IFTTT's **WeatherFlow / Tempest** service has its own triggers, such as rain detected and lightning detected.
Use these as a backup that keeps working even if your server is down.

## How hail is decided

The Tempest flags hail from impacts on its sensor. Heavy rain can trip it, but missing real hail is worse, so:

- **Any hail flag from the Tempest inflates the car cover right away.** On its own it counts as *possible hail
  (unconfirmed)*, and the cover comes down `HAIL_UNCONFIRMED_OFF_MIN` (30) minutes after the last flag. Each new flag
  restarts that clock.
- **Backup sources confirm it**, switching to full protection:
  - an NWS warning (or watch, if `HAIL_SHIELD_ON_WATCH`) that mentions hail for your location,
  - the hail camera sees stones (when installed),
  - the sensor flags hail in at least `HAIL_CONFIRM_MINUTES` (3) of the last 10 minutes **and** lightning is within
    `HAIL_CONFIRM_LIGHTNING_MI` (15 mi) in the last 30 minutes.
- **Once confirmed**, the cover stays up while a thunderstorm is within `STORM_LIGHTNING_MI` or a severe warning is
  active, then deflates `HAIL_SHIELD_OFF_DELAY_MIN` (60) minutes after that. Plain rain doesn't hold it up.
- An NWS hail warning on its own inflates the cover too, before any hail reaches the station.
- Clicking **Auto** on the dashboard while it's up for an unconfirmed reading takes it down and ignores that reading.

## Car hail cover (smart plug)

The monitor can switch a smart plug that powers an inflatable car cover.

- **Inflates** (`tempest_hail_shield_on`) as soon as hail is expected or happening:
  an NWS warning with hail for your location, an NWS watch that mentions hail (`HAIL_SHIELD_ON_WATCH`), or hail hitting your Tempest.
- **Stays up** while a thunderstorm is still close or a severe warning is active, then **deflates** (`tempest_hail_shield_off`) once no hail is expected and
  `HAIL_SHIELD_OFF_DELAY_MIN` (60 min) has passed. If hail returns in that hour, the timer resets.
- **Dashboard control:** the Car hail cover card shows the status and has **Auto / On / Off** buttons.
  On and Off are manual overrides that pause automation until you pick Auto again. Use On to test the plug.
- The state survives restarts.

**YoLink plug (recommended, direct):** set `YOLINK_UAID`, `YOLINK_SECRET` and `YOLINK_DEVICE` (the plug's name in the YoLink app).
Get the UAID and Secret Key in the YoLink app: **Settings → Account → Advanced Settings → User Access Credentials → +**.
The monitor turns the plug on/off through YoLink's API, reads the state back to confirm, and retries up to 5 times.
If it still fails, it sends `tempest_shield_failed` to IFTTT so you know to check the cover. This path costs no IFTTT credits.

**Or via IFTTT:** make two applets: Webhooks `tempest_hail_shield_on` → your plug's **Turn on**, and `tempest_hail_shield_off` → **Turn off**.
Or set `HAIL_SHIELD_ON_URL` / `HAIL_SHIELD_OFF_URL` to call Home Assistant or the plug's own API directly (a POST to each URL), which skips IFTTT.

| Setting | Default | |
|---|---|---|
| `HAIL_SHIELD` | true | turn the feature on/off |
| `HAIL_SHIELD_ON_WATCH` | true | inflate for hail **watches** too (not just warnings) |
| `HAIL_SHIELD_MIN_IN` | 0 | minimum forecast hail size, inches |
| `HAIL_SHIELD_OFF_DELAY_MIN` | 60 | how long after the storm before deflating |
| `HAIL_SHIELD_MAX_HOURS` | 0 | safety cutoff (0 = none) |
| `HAIL_SHIELD_RESEND_MIN` | 0 | re-send "on" every N min while inflated, in case a plug command is missed (costs IFTTT credits) |

## YoLink tab (indoor temperature & humidity)

The dashboard has a **YoLink** tab (`http://<server>:8088/#yolink`). With `YOLINK_UAID` / `YOLINK_SECRET` set, the monitor
finds every YoLink temperature/humidity sensor on your account and reads each one every `YOLINK_POLL_MIN` (5) minutes.
The tab shows a card per sensor (temperature, humidity, 24 h low/high, battery, last report, YoLink alarms like
"Too hot" or "Low battery"). Click a card to expand its history: 24 h by default, or 3 days, 7 days and 30 days, with
the Tempest outdoor temperature as a dashed reference. There's also a sensor table. History is kept in the same database. New sensors are picked up every 6 hours, or right away
with **Refresh names & sensors**.

Sensor alarms (the limits you set in the YoLink app: too warm/cold, too humid/dry, low battery) and sensors going
offline (no report for `YOLINK_OFFLINE_HOURS`=6 hours) are recorded in the **Sensor log** on the YoLink tab, with the
reading and the limit, plus when each one cleared. No notifications are sent; the YoLink app already does that.

**Compare / delta:** every card shows how much warmer or colder it is than another source right now. Sensors compare with
Outdoor by default, and Outdoor compares with the sensor named "Attic". Change it with **Compare with** in an expanded card
(saved on the server, so it's the same on every device). Expanded cards then show both temperatures plus a **Difference**
chart (this sensor minus the other) with the average and range for the selected period.

## Bills tab (`#bills`)

Electric, water, gas and internet bills, month over month and year over year.

- **Bills from Google Drive:** `tools/windows/install_bill_sync.bat` (run once on malaga-office) creates an hourly Windows
  task that copies new PDFs from `G:\My Drive\Documents\Personal\Bills` (sub-folders per provider) to the ReadySHARE
  Bills folder below. Folder names (Atmos, TXU, Rhythm Energy, ATT, Brushy Creek Mud) tell the dashboard which bill it is;
  Electricity Facts Labels are skipped.
- **PDF bills:** drop them in the **Bills** folder next to the backups on ReadySHARE
  (`\\readyshare\Data\WeatherStation\Bills`, checked every 30 min, or click **Check bills folder**), or use **Upload bill PDF**.
  The amount, usage, billing period and due date are read from the PDF text (Rhythm, AT&T, City of Round Rock and Atmos
  are recognized by name; others by their units). If a local Ollama model is running on the EdgeXpert (`OLLAMA_URL`,
  default `http://127.0.0.1:11434`, `OLLAMA_MODEL` optional) it fills in anything the patterns miss. Anything still missing
  is marked **Needs review**; click **Edit** to fix it. Scanned (image-only) PDFs can't be read; add those by hand.
- **Smart Meter Texas:** with `SMT_USERNAME` / `SMT_PASSWORD` (your smartmetertexas.com login) the monitor downloads your
  meter's 15-minute readings (`SMT_BACKFILL_DAYS`=60 to start, then daily) and shows electricity per day next to the outdoor
  temperature. `SMT_ESIID` picks the meter if the account has more than one. This uses the same unofficial API as the Home
  Assistant integration and may break if Smart Meter Texas changes their site.
- Cards show the latest bill per utility with the change from the last bill and the same month last year (and ¢/kWh);
  the monthly cost chart stacks the four bills and shows cooling/heating degree-days from the Tempest on hover.

## Sprinklers tab (`#sprinklers`, Rachio)

Set `RACHIO_API_KEY` (Rachio app → Account settings → Get API key).

- **Status:** controller, what's watering now and time left, schedules, rain delay.
- **History:** every zone run is recorded (from Rachio's last-watered time and duration) with estimated gallons
  (zone nozzle rate × area from the Rachio app). Watering minutes per day sit next to the Tempest's rain per day.
- **Smart rain skip:** checked every 10 minutes. Sets a `RACHIO_SKIP_HOURS` (24) rain delay when the Tempest measured
  ≥ `RACHIO_SKIP_RAIN_IN` (0.25 in) in the last 24 h, the forecast expects ≥ `RACHIO_SKIP_FORECAST_IN` (0.25 in) in the next
  24 h, or the forecast low is ≤ `RACHIO_SKIP_FREEZE_F` (35°F). `RACHIO_SKIP_DRY_RUN=true` only logs what it would do.
  Turn it on or off on the tab.
- **Manual control:** run a zone for N minutes, stop watering, set or clear a 24 h / 48 h rain delay. Starting and stopping
  ask for a second click to confirm.
- **Sprinklers vs water bill:** estimated sprinkler gallons per month next to the water bill's gallons.
- About 300 of Rachio's 1,700 daily API calls are used.

## History (year over year)

Everything is kept: every 1-minute Tempest observation, every lightning strike, every YoLink reading, in `data/tempest.db`.
Nothing is ever pruned except the 3-second wind samples outside of storms.

- **Full Tempest history:** on first start the monitor downloads your station's entire history from Tempest's cloud,
  one day at a time (newest first, about 2 s per day, so roughly 12 minutes per year of history). Progress shows on the
  History tab. Turn off with `HISTORY_BACKFILL=false`.
- **Daily summaries:** each day is rolled up per sensor (high/low/average temperature and humidity, rain, peak gust,
  pressure, UV, lightning strikes, hail minutes) for fast year-over-year views.
- **History tab** (`#history`): pick a sensor and metric to get one line per year across Jan–Dec (rain as a running
  total), all-time records, a by-year table (rain days, 100°F days, freeze days, hail days…) and a month-by-year table with the
  change from last year. **Download CSV** exports the daily table for Excel. YoLink cards link straight to their history.
- **Backups:** every night after the daily summary, a copy of the database goes to `data/backups/`
  (last `BACKUP_KEEP_DAYS`=14 days plus one per month for `BACKUP_KEEP_MONTHS`=24 months).
  With `BACKUP_SMB_PATH=//readyshare/Data/WeatherStation` that folder is mirrored to the network share every night
  (only new or changed files are copied; old ones are removed to match). Guest access is used unless you set
  `BACKUP_SMB_USER` / `BACKUP_SMB_PASSWORD`. If the name doesn't resolve, the router's address is tried, or set
  `BACKUP_SMB_HOST`. The History tab shows the last backup and has a **Back up now** button.

## Hail camera (Reolink + AI measurement)

A Reolink camera looks down at a **hail plate**: a matte black board (400 × 400 mm by default) with four printed markers
in its corners. During storm watches, storms, NWS hail alerts or station-detected hail, the monitor grabs a frame every
2 seconds, straightens the plate using the markers, and measures every hailstone in millimetres. Each stone is counted once
as it lands.

**You get:** largest stone (with the familiar name: pea, penny, quarter, golf ball…), count, median size, stones per sq ft,
a size histogram, and photos with every stone outlined (saved in `data/hailcam/<date>/`).
Alerts: `tempest_hail_measured` (first stone, and again whenever the largest grows by 5 mm) and `tempest_hail_report`
(summary when the hail stops). Camera-confirmed hail also inflates the car cover.

**Setup**
1. Print the markers: `python tools/make_marker.py hail_markers.pdf` (or use the PDF in this repo's release). Print at 100 %,
   check the 100 mm ruler, laminate them.
2. Plate: matte black, flat, slightly tilted so meltwater drains. Glue the markers in the corners (ID 0 top-left, 1 top-right,
   2 bottom-right, 3 bottom-left as the camera sees it), black square 12 mm in from each edge.
3. Camera: mounted 1–2 m above the plate, looking down as straight as practical, plate filling most of the frame,
   under an overhang so the lens stays dry. 4K or 1080p main stream. IR on at night works: ice shows bright on the black plate.
4. Settings: `HAILCAM_HOST` (camera IP), `HAILCAM_USER`, `HAILCAM_PASSWORD`. Give the camera a fixed IP.
   If the camera is behind an NVR, set `HAILCAM_RTSP=rtsp://user:pass@ip:554/h264Preview_01_main` instead.
5. Dashboard → Hail camera → **Test capture**: you want **4/4 markers**. "Camera view" shows the raw image for aiming.

Test without hail: `python tools/synth_hail.py /tmp/hail` makes synthetic plate frames with known stone sizes.
In testing on synthetic frames the sizes came out within about 1–2 mm.

| Setting | Default | |
|---|---|---|
| `HAILCAM_PLATE_MM` | 400x400 | plate width x height |
| `HAILCAM_MARKER_MM` | 50 | printed black-square size |
| `HAILCAM_INSET_MM` | 12 | marker distance from plate edges |
| `HAILCAM_INTERVAL_S` | 2 | seconds between frames while active |
| `HAILCAM_MIN_MM` | 5 | ignore smaller objects |
| `HAILCAM_ALERT_MM` | 6 | smallest stone that triggers `tempest_hail_measured` |
| `HAILCAM_END_MIN` | 10 | minutes without new stones = hail over |

## API

These endpoints are useful for Home Assistant, Grafana, or your LLM stack:

`/api/state` · `/api/history?hours=24` · `/api/rapid?minutes=30` · `/api/strikes?hours=24` ·
`/api/storms` · `/api/events` · `/api/shield` (GET; POST `{"mode":"auto|on|off"}`) · `/api/wind_candles` · `/api/wind_rose?hours=24` · `/api/yolink/sensors` · `/api/yolink/history?hours=24&device=<id>` · `/api/history/sources` · `/api/history/yoy?source=out&metric=temp` · `/api/history/summary` · `/api/history/export.csv` · `/api/bills` · `/api/smt/daily` · `/api/rachio` · `/api/rachio/history` · `/api/hailcam` · `/api/hailcam/image` · `POST /api/hailcam/test` · `/api/daily?days=14` · `/api/stream` (server-sent events) ·
`POST /api/test-ifttt`

## Notes

- All thresholds are in `.env`. Restart the service after changing them.
- The cloud WebSocket authenticates with `?token=` in the URL, as Tempest documents. REST calls use an `Authorization` header.
- 3-second wind is kept for 48 h. Wind recorded during storms is kept permanently.
- No authentication on the dashboard. Keep it on your LAN or put it behind your reverse proxy.
