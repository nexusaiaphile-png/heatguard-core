"""Controlled live integration check for the HeatGuardAI telemetry pipeline.

This script targets a real service only when --confirm-live is supplied. It
creates/reuses a clearly named test camera with no alert recipients, submits
20 slightly varied baseline readings, then submits a synthetic 75 C anomaly.

Important:
- The API assigns camera IDs automatically; this script cannot force ID=1.
- Use the exact Render service URL, e.g. https://heatguard-core-engine.onrender.com.
- Test camera has empty SMS/email recipients to avoid contacting real people.
- The telemetry endpoint is currently unauthenticated; run only against your
  own service and do not expose this test flow to untrusted users.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from typing import Any

import httpx

CAMERA_NAME = "HeatGuardAI pipeline validation (synthetic)"
LOCATION = "Synthetic Test Zone - NOT A REAL FIRE"
ENDPOINT = "/api/v1/telemetry/submit"


def trace(label: str, status_code: int | None = None, payload: Any = None) -> None:
    print(f"\n=== {label} ===", flush=True)
    if status_code is not None:
        print(f"HTTP {status_code}", flush=True)
    if payload is not None:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), flush=True)


async def run(base_url: str, timeout_seconds: float) -> int:
    api_key = __import__("os").environ.get("HEATGUARD_API_KEY", "").strip()
    if not api_key:
        trace("SAFETY STOP", payload={"reason": "Set HEATGUARD_API_KEY in the environment before running the live validator; no requests sent."})
        return 2
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        follow_redirects=True,
        headers={"X-API-Key": api_key},
    ) as client:
        trace("1. CHECK SERVICE HEALTH", payload={"base_url": base_url, "health_endpoint": "/health"})
        response = await client.get("/health")
        health = _safe_json(response)
        trace("Health endpoint response", response.status_code, health)
        if response.status_code != 200 or not isinstance(health, dict) or health.get("status") != "HeatGuard AI is running":
            trace("SAFETY STOP", payload={
                "reason": "Health check did not return the expected healthy response; no test camera or telemetry was submitted.",
                "expected": {"status": "HeatGuard AI is running"},
            })
            return 1

        # Reuse this dedicated test camera when possible; never target an
        # arbitrary camera ID because that could alert real recipients.
        response = await client.get("/cameras")
        trace("2. LIST CAMERAS", response.status_code, _safe_json(response))
        response.raise_for_status()
        cameras = response.json()
        if not isinstance(cameras, list):
            trace("Unexpected /cameras response", payload={"expected": "JSON list"})
            return 1

        camera = next((item for item in cameras if item.get("name") == CAMERA_NAME), None)
        if camera:
            camera_id = int(camera["id"])
            trace("3. REUSE TEST CAMERA", payload={"camera_id": camera_id, "name": CAMERA_NAME})
        else:
            registration = {
                "name": CAMERA_NAME,
                "ip": "192.0.2.1",
                "location": LOCATION,
                "alert_numbers": "",
                "alert_emails": None,
            }
            response = await client.post("/cameras", json=registration)
            trace("3. REGISTER TEST CAMERA", response.status_code, _safe_json(response))
            if response.status_code == 409:
                # A concurrent run may have created it; re-query and reuse.
                retry = await client.get("/cameras")
                retry.raise_for_status()
                camera = next((item for item in retry.json() if item.get("name") == CAMERA_NAME), None)
                if not camera:
                    return 1
                camera_id = int(camera["id"])
            else:
                response.raise_for_status()
                camera_id = int(response.json()["id"])

        camera_response = await client.get("/cameras")
        camera_response.raise_for_status()
        selected_camera = next((item for item in camera_response.json() if int(item.get("id", -1)) == camera_id), None)
        if not selected_camera or selected_camera.get("active") is not False:
            trace("SAFETY STOP", payload={"reason": "Synthetic validation camera is missing or active; refusing to send synthetic telemetry.", "camera": selected_camera})
            return 1

        trace("Camera selected", payload={
            "camera_id": camera_id,
            "requested_id_1_note": "Camera IDs are assigned by the API; ID 1 cannot safely be forced.",
            "alert_numbers": "empty",
            "alert_emails": "empty",
            "location": LOCATION,
        })

        # Small variation is intentional: a constant baseline has zero
        # standard deviation, making the current detector unable to calculate
        # a meaningful positive Z-score.
        rng = random.Random(20261025)
        baseline = [round(31.5 + rng.uniform(-0.5, 0.5), 2) for _ in range(20)]
        trace("4. SUBMIT 20 BASELINE READINGS", payload={"count": len(baseline), "temperatures_c": baseline})
        for index, temperature in enumerate(baseline, start=1):
            body = {"camera_id": camera_id, "avg_temp": temperature, "location": LOCATION}
            response = await client.post(ENDPOINT, json=body)
            result = _safe_json(response)
            trace(f"Baseline {index}/20 ({temperature:.2f} C)", response.status_code, result)
            response.raise_for_status()
            await asyncio.sleep(0.05)

        spike_body = {"camera_id": camera_id, "avg_temp": 75.0, "location": LOCATION}
        trace("5. SUBMIT SYNTHETIC 75.0 C ANOMALY", payload=spike_body)
        response = await client.post(ENDPOINT, json=spike_body)
        result = _safe_json(response)
        trace("ANOMALY RESPONSE (contains AI analysis if detection triggered)", response.status_code, result)
        response.raise_for_status()

        if isinstance(result, dict) and result.get("status") in {"anomaly_detected", "anomaly_reviewed_not_genuine"}:
            trace("6. PIPELINE RESPONSE", payload={
                "interpretation": "Server response indicates the anomaly path was reached.",
                "status": result.get("status"),
                "note": "Gemini review and alert creation are indicated by anomaly_detected. This test camera has no recipients, so Twilio dispatch is intentionally not attempted.",
            })
        else:
            trace("6. PIPELINE NEEDS REVIEW", payload={
                "interpretation": "The response did not clearly confirm that the anomaly/alert path completed.",
                "status": result.get("status") if isinstance(result, dict) else None,
                "note": "Inspect the JSON response and Render logs; HTTP 200 alone does not prove alert delivery.",
            })
            return 1

        readings_response = await client.get("/readings", params={"camera_id": camera_id, "limit": 100})
        readings_response.raise_for_status()
        readings = readings_response.json()
        test_readings = [r for r in readings if r.get("location") == LOCATION]
        trace("7. VERIFY STORED READINGS", payload={"camera_id": camera_id, "matching_synthetic_readings_returned": len(test_readings), "expected_at_least": 21})
        if len(test_readings) < 21:
            trace("PIPELINE NEEDS REVIEW", payload={"reason": "Expected at least 20 baseline readings plus the anomaly reading."})
            return 1

        alerts_response = await client.get("/alerts", params={"limit": 100})
        alerts_response.raise_for_status()
        alerts = alerts_response.json()
        matching_alerts = [a for a in alerts if a.get("camera_id") == camera_id and a.get("location") == LOCATION]
        trace("8. VERIFY STORED ALERT", payload={"camera_id": camera_id, "matching_synthetic_alerts": len(matching_alerts), "latest": matching_alerts[0] if matching_alerts else None})
        if isinstance(result, dict) and result.get("status") == "anomaly_detected" and not matching_alerts:
            trace("PIPELINE NEEDS REVIEW", payload={"reason": "API reported anomaly_detected but no matching alert record was visible."})
            return 1
        return 0


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"body": response.text[:4000]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        required=True,
        help="Exact Render service URL, e.g. https://heatguard-core-engine.onrender.com",
    )
    parser.add_argument("--timeout", type=float, default=25.0, help="Per-request timeout in seconds")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required acknowledgement before sending synthetic data to the live service",
    )
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        parser.error("--url must begin with https://")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.confirm_live:
        print("Safety stop: this contacts a live service. Re-run with --confirm-live after checking --url.", file=sys.stderr)
        raise SystemExit(2)
    try:
        raise SystemExit(asyncio.run(run(args.url, args.timeout)))
    except httpx.HTTPError as exc:
        print(f"Request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
