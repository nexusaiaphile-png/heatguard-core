#!/usr/bin/env python3
"""HeatGuard AI: interactive camera onboarding and guarded telemetry smoke test.

Install dependency: python -m pip install httpx

Required environment variable:
  HEATGUARD_API_KEY       API key for the selected HeatGuard gateway.

Optional:
  HEATGUARD_API_URL       Gateway base URL (defaults to production Render URL).
  HEATGUARD_ALLOW_LIVE_ALERT_TEST=YES
                          Explicitly unlocks the production 74.5°C telemetry test.

IMPORTANT:
- Camera onboarding writes real camera records to the configured database.
- The telemetry endpoint writes a real reading. If sufficient baseline data exists
  and detection criteria are met, it can invoke Gemini and attempt real SMS/WhatsApp
  notifications to the numbers registered for that camera.
- A 74.5°C sample does NOT guarantee an anomaly alert: the server requires at least
  20 baseline readings and checks its configured z-score and temperature-rise limits.
- Notification attempts are not proof of delivery. Do not use this test as a fire
  safety certification or as a substitute for testing an actual camera.
"""
from __future__ import annotations

import ipaddress
import os
import sys
from typing import Any

import httpx

DEFAULT_API_URL = "https://heatguard-core-engine.onrender.com"
BULK_ACTIVATION_PATH = "/api/v1/cameras/bulk-activate"
TELEMETRY_PATH = "/api/v1/telemetry/submit"
TIMEOUT_SECONDS = 30.0


def required(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("Please enter a value.")


def comma_values(prompt: str) -> list[str]:
    while True:
        values = [part.strip() for part in input(prompt).split(",")]
        if values and all(values):
            return values
        print("Enter comma-separated values; empty entries are not allowed.")


def collect_batch() -> list[dict[str, Any]]:
    print("\nEnter names and IP addresses in matching order.")
    print("Example names: Loading Bay, Cold Storage")
    print("Example IPs:   192.168.1.20, 192.168.1.21")

    while True:
        names = comma_values("Camera names (comma-separated): ")
        if len(names) != len(set(names)):
            print("Camera names must be unique within this batch.")
            continue
        break

    while True:
        raw_ips = comma_values("Camera local IPs (comma-separated): ")
        if len(raw_ips) != len(names):
            print(f"Please provide exactly {len(names)} IP address(es), one per camera.")
            continue
        try:
            ips = [str(ipaddress.ip_address(value)) for value in raw_ips]
        except ValueError:
            print("One or more IP addresses are invalid. Please try again.")
            continue
        break

    location = required("Facility / site location: ")
    alert_numbers = ",".join(comma_values("Alert recipient phone number(s), including country code: "))

    return [
        {
            "name": name,
            "ip": ip,
            "location": location,
            "alert_numbers": alert_numbers,
            "alert_emails": None,
        }
        for name, ip in zip(names, ips)
    ]


def api_config() -> tuple[str, dict[str, str]]:
    api_key = os.getenv("HEATGUARD_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "HEATGUARD_API_KEY is not set. Nothing was submitted. "
            "Set it in your environment and rerun."
        )
    base_url = os.getenv("HEATGUARD_API_URL", DEFAULT_API_URL).strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("Gateway URL must use HTTPS.")
    return base_url, {"X-API-Key": api_key, "Content-Type": "application/json"}


def post_json(client: httpx.Client, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(url, headers=headers, json=payload)
    if response.is_error:
        raise RuntimeError(f"HTTP {response.status_code} from {url}: {response.text[:1000]}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Gateway returned non-JSON response from {url}.") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected response format from {url}.")
    return result


def main() -> int:
    print("=" * 56)
    print("HEATGUARD AI — ONBOARD & TEST")
    print("=" * 56)
    print("This tool writes to the selected gateway; production is the default.")
    try:
        cameras = collect_batch()
        print(f"\nBatch preview: {len(cameras)} camera(s)")
        for index, camera in enumerate(cameras, start=1):
            print(f"  {index}. {camera['name']} | {camera['ip']} | {camera['location']}")
        print(f"  Alert recipients: {cameras[0]['alert_numbers']}")
        confirm = input("\nRegister this batch in the configured database? Type YES to continue: ").strip()
        if confirm != "YES":
            print("Cancelled. No request was sent.")
            return 0

        base_url, headers = api_config()
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            activation = post_json(
                client,
                f"{base_url}{BULK_ACTIVATION_PATH}",
                headers,
                {"cameras": cameras},
            )

            if activation.get("status") != "success":
                raise RuntimeError(f"Gateway did not confirm successful activation: {activation}")

            nodes = activation.get("nodes", [])
            print("\nBATCH RESULT")
            print(f"  Activated: {activation.get('activated_count', len(nodes))}")
            print(f"  Skipped duplicates: {activation.get('skipped_count', 0)}")
            for node in nodes:
                print(f"  ID {node.get('id')}: {node.get('name')} — {node.get('location')}")

            if not nodes:
                print("\nNo new camera IDs were returned; cannot choose a newly created camera for testing.")
                return 0

            answer = input(
                "\nAttempt a 74.5°C telemetry test on one newly registered camera? (y/n): "
            ).strip().lower()
            if answer not in {"y", "yes"}:
                print("Onboarding complete. No test telemetry was sent.")
                return 0

            print(
                "\nLIVE TEST WARNING: This writes a synthetic reading to the configured database. "
                "If the server's baseline and thresholds trigger an anomaly, Gemini review and "
                "real notification attempts may occur for the registered recipients."
            )
            if os.getenv("HEATGUARD_ALLOW_LIVE_ALERT_TEST", "").strip() != "YES":
                print(
                    "Locked by default. To unlock, explicitly set "
                    "HEATGUARD_ALLOW_LIVE_ALERT_TEST=YES and rerun."
                )
                return 2

            for index, node in enumerate(nodes, start=1):
                print(f"  {index}. ID {node.get('id')} — {node.get('name')}")
            selected = input("Enter the ID of the camera to test: ").strip()
            try:
                camera_id = int(selected)
            except ValueError:
                print("Invalid camera ID. No telemetry was sent.", file=sys.stderr)
                return 2
            selected_node = next((node for node in nodes if node.get("id") == camera_id), None)
            if selected_node is None:
                print("That ID was not created in this batch. No telemetry was sent.", file=sys.stderr)
                return 2

            final_confirm = input(
                f"Type SEND 74.5 TO {camera_id} to write the reading and possibly trigger real alerts: "
            ).strip()
            if final_confirm != f"SEND 74.5 TO {camera_id}":
                print("Cancelled. No telemetry was sent.")
                return 0

            result = post_json(
                client,
                f"{base_url}{TELEMETRY_PATH}",
                headers,
                {
                    "camera_id": camera_id,
                    "avg_temp": 74.5,
                    "location": selected_node.get("location") or cameras[0]["location"],
                },
            )
            print("\nTELEMETRY TEST RESPONSE")
            print(f"  Status: {result.get('status', 'unknown')}")
            print(f"  Sample count: {result.get('sample_count', result.get('context', {}).get('sample_count', 'n/a'))}")
            if "current_z_score" in result:
                print(f"  Current z-score: {result['current_z_score']}")
            if "alert_id" in result:
                print(f"  Alert record ID: {result['alert_id']}")
            if result.get("notifications_attempted"):
                print("  Notification attempts: requested; provider delivery is not confirmed.")
            print("\nTest request completed. Review the response and production logs; do not assume delivery.")
            return 0

    except (EOFError, KeyboardInterrupt):
        print("\nCancelled by user. Any earlier completed API request remains in effect.")
        return 130
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
