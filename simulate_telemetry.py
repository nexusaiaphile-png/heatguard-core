"""Generate local mock telemetry for five HeatGuardAI camera nodes.

Run against a locally running API. Each node emits ambient readings between
30 and 35 °C; camera-03 periodically receives a synthetic critical spike.
This is test data only and does not connect to physical cameras.
"""
from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime, timezone

import httpx

NODES = [
    {"site_id": "mock-factory", "camera_id": f"camera-{index:02d}"}
    for index in range(1, 6)
]


async def run(base_url: str, interval: float, spike_every: int, cycles: int) -> None:
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        for cycle in range(cycles):
            readings = []
            for node in NODES:
                temperature = round(random.uniform(30.0, 35.0), 2)
                spike = node["camera_id"] == "camera-03" and cycle > 0 and cycle % spike_every == 0
                if spike:
                    temperature = round(random.uniform(49.0, 55.0), 2)
                payload = {
                    **node,
                    "temperature_c": temperature,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {
                        "simulated": True,
                        "cycle": cycle,
                        "synthetic_critical_spike": spike,
                        "channel": node["camera_id"],
                    },
                }
                try:
                    response = await client.post("/api/thermal/readings", json=payload)
                    response.raise_for_status()
                    result = response.json()
                    readings.append(result)
                    marker = " *** INJECTED SPIKE ***" if spike else ""
                    print(
                        f"[SIM] cycle={cycle:04d} {node['camera_id']} "
                        f"temp={temperature:.2f}C baseline={result.get('baseline_c')} "
                        f"deviation={result.get('deviation_c')} anomaly={result.get('anomaly')}"
                        f"{marker}",
                        flush=True,
                    )
                except httpx.HTTPError as exc:
                    print(f"[SIM] POST failed for {node['camera_id']}: {exc}", flush=True)
            if cycle + 1 < cycles:
                await asyncio.sleep(max(0.1, interval))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between cycles")
    parser.add_argument("--spike-every", type=int, default=10, help="inject on every Nth cycle")
    parser.add_argument("--cycles", type=int, default=60, help="number of cycles; 0 runs forever")
    args = parser.parse_args()
    if args.spike_every < 2:
        parser.error("--spike-every must be at least 2")
    cycles = args.cycles if args.cycles > 0 else 10**12
    asyncio.run(run(args.url, args.interval, args.spike_every, cycles))


if __name__ == "__main__":
    main()
