# Tempest Monitor

An always-on service for the **Malaga Hills Dr** Tempest station (217588). It records everything,
detects storms on its own, goes live when one is coming, serves a dashboard, and talks to IFTTT.

## How it works

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

## API

These endpoints are useful for Home Assistant, Grafana, or your LLM stack:

`/api/state` · `/api/history?hours=24` · `/api/rapid?minutes=30` · `/api/strikes?hours=24` ·
`/api/storms` · `/api/events` · `/api/daily?days=14` · `/api/stream` (server-sent events) ·
`POST /api/test-ifttt`

## Notes

- All thresholds are in `.env`. Restart the service after changing them.
- The cloud WebSocket authenticates with `?token=` in the URL, as Tempest documents. REST calls use an `Authorization` header.
- 3-second wind is kept for 48 h. Wind recorded during storms is kept permanently.
- No authentication on the dashboard. Keep it on your LAN or put it behind your reverse proxy.
