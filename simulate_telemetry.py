"""Exercise HeatGuardAI's external telemetry endpoint with synthetic readings.

Creates/reuses a test camera, submits normal samples, then submits a critical
synthetic spike. No physical camera is contacted by this script.
"""
from __future__ import annotations

import argparse
import asyncio
import random

import httpx

CAMERA_NAME = "HeatGuardAI external simulator"
LOCATION = "Mock Factory / Test Zone"


async def run(base_url: str, normal_samples: int, interval: float) -> None:
    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        # Reuse a prior simulator camera if present; otherwise register one.
        response = await client.get("/cameras")
        response.raise_for_status()
        existing = next((c for c in response.json() if c.get("name") == CAMERA_NAME), None)
        if existing:
            camera_id = int(existing["id"])
            print(f"[SIM] Reusing registered test camera id={camera_id}", flush=True)
        else:
            response = await client.post("/cameras", json={
                "name": CAMERA_NAME,
                "ip": "192.0.2.1",
                "location": LOCATION,
                "alert_numbers": "",
                "alert_emails": None,
            })
            response.raise_for_status()
            camera_id = int(response.json()["id"])
            print(f"[SIM] Registered test camera id={camera_id}", flush=True)

        endpoint = "/api/v1/telemetry/submit"
        for index in range(normal_samples):
            temperature = round(random.uniform(30.0, 35.0), 2)
            response = await client.post(endpoint, params={
                "camera_id": camera_id,
                "avg_temp": temperature,
                "location": LOCATION,
            })
            response.raise_for_status()
            result = response.json()
            print(
                f"[SIM] normal {index + 1}/{normal_samples} temp={temperature:.2f}C "
                f"status={result.get('status')} samples={result.get('sample_count', 0)} "
                f"z={result.get('current_z_score')}",
                flush=True,
            )
            if interval > 0 and index + 1 < normal_samples:
                await asyncio.sleep(interval)

        # Deliberately far above the normal range to exercise spike detection.
        spike_temp = round(random.uniform(52.0, 55.0), 2)
        response = await client.post(endpoint, params={
            "camera_id": camera_id,
            "avg_temp": spike_temp,
            "location": LOCATION,
        })
        response.raise_for_status()
        result = response.json()
        print(
            f"[SIM] SPIKE temp={spike_temp:.2f}C status={result.get('status')} "
            f"context={result.get('context', result)}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Full Render service URL, e.g. https://your-service.onrender.com")
    parser.add_argument("--normal-samples", type=int, default=25, help="Normal readings before the spike (minimum 20)")
    parser.add_argument("--interval", type=float, default=0.2, help="Seconds between normal readings")
    args = parser.parse_args()
    if args.normal_samples < 20:
        parser.error("--normal-samples must be at least 20")
    if args.interval < 0:
        parser.error("--interval cannot be negative")
    asyncio.run(run(args.url, args.normal_samples, args.interval))


if __name__ == "__main__":
    main()
