"""
Send fake Tempest UDP packets so you can test the monitor without waiting for weather.

  python tools/simulate_udp.py --host 127.0.0.1 --scenario storm --speed 20

Scenarios: calm (just observations), storm (calm -> distant lightning -> storm -> clear).
--speed compresses time: 20 means one simulated minute every 3 seconds.
Packets carry the current real time, so the monitor treats them as live.
"""
import argparse, json, random, socket, time

SERIAL = "ST-SIMULATOR"


def obs(t, temp, rh, pres, lull, avg, gust, wdir, rain_mm, strike_km=0, strikes=0):
    return {"serial_number": SERIAL, "type": "obs_st", "hub_sn": "HB-SIM", "firmware_revision": 176,
            "obs": [[int(t), lull, avg, gust, wdir, 3, pres, temp, rh, 0, 0, 0, rain_mm,
                     1 if rain_mm else 0, strike_km, strikes, 2.65, 1]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="255.255.255.255")
    ap.add_argument("--port", type=int, default=50222)
    ap.add_argument("--scenario", default="storm", choices=["calm", "storm"])
    ap.add_argument("--speed", type=float, default=20)
    a = ap.parse_args()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    send = lambda m: s.sendto(json.dumps(m).encode(), (a.host, a.port))
    minute = 60 / a.speed

    # (minutes, phase)
    plan = [(4, "calm")] if a.scenario == "calm" else [(3, "calm"), (3, "distant"), (6, "storm"), (8, "clear")]
    temp, pres = 21.0, 1003.0
    for mins, phase in plan:
        print(f"--- {phase} ({mins} simulated min)")
        for _ in range(mins):
            now = time.time()
            if phase == "calm":
                avg, gust, rain = 1.5, 3.0, 0
            elif phase == "distant":
                avg, gust, rain = 3.0, 6.0, 0
                send({"serial_number": SERIAL, "type": "evt_strike", "hub_sn": "HB-SIM", "evt": [int(now), random.randint(45, 60), 3000]})
            elif phase == "storm":
                avg, gust, rain = 9.0, 16.0, random.choice([0.4, 0.8, 1.6])
                temp -= 0.8
                pres += 0.3
                if random.random() < .7:
                    send({"serial_number": SERIAL, "type": "evt_strike", "hub_sn": "HB-SIM", "evt": [int(now), random.randint(4, 12), 9000]})
            else:
                avg, gust, rain = 2.0, 4.0, 0
            if phase == "storm" and _ == 0:
                send({"serial_number": SERIAL, "type": "evt_precip", "hub_sn": "HB-SIM", "evt": [int(now)]})
            send(obs(now, round(temp, 1), 80 if rain else 65, round(pres, 1), avg * .5, avg, gust, 160, rain))
            # rapid wind every "3 s"
            steps = max(1, int(minute / 0.5))
            for _i in range(steps):
                send({"serial_number": SERIAL, "type": "rapid_wind", "hub_sn": "HB-SIM",
                      "ob": [int(time.time()), round(max(0, random.gauss(avg, avg * .3)), 2), random.randint(140, 190)]})
                time.sleep(minute / steps)


if __name__ == "__main__":
    main()
