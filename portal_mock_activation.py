"""Example portal mock: bulk-activate three industrial camera zones.

Usage:
    export HEATGUARD_API_KEY="your-production-api-key"
    export HEATGUARD_API_URL="https://heatguard-core-engine.onrender.com"
    export HEATGUARD_ALERT_NUMBERS="+27820000000"
    export HEATGUARD_ALERT_EMAILS="ops@example.com"
    python portal_mock_activation.py

This script sends one authenticated batch request. Replace example camera IPs,
contact details, and zone labels with values approved for your test environment.
"""
import os
import sys

import httpx


BASE_URL = os.getenv(
    "HEATGUARD_API_URL",
    "https://heatguard-core-engine.onrender.com",
).rstrip("/")
API_KEY = os.getenv("HEATGUARD_API_KEY", "").strip()
ALERT_NUMBERS = os.getenv("HEATGUARD_ALERT_NUMBERS", "+27820000000")
ALERT_EMAILS = os.getenv("HEATGUARD_ALERT_EMAILS", "ops@example.com")


def build_payload() -> dict:
    """Build a single request for three distinct industrial zones."""
    zones = [
        {
            "name": "Portal Mock - Boiler Room",
            "ip": "192.168.10.21",
            "location": "Industrial Site / Zone 1 - Boiler Room",
        },
        {
            "name": "Portal Mock - Production Line",
            "ip": "192.168.20.21",
            "location": "Industrial Site / Zone 2 - Production Line",
        },
        {
            "name": "Portal Mock - Finished Goods Warehouse",
            "ip": "192.168.30.21",
            "location": "Industrial Site / Zone 3 - Finished Goods Warehouse",
        },
    ]
    return {
        "cameras": [
            {
                **zone,
                "alert_numbers": ALERT_NUMBERS,
                "alert_emails": ALERT_EMAILS or None,
            }
            for zone in zones
        ]
    }


def main() -> int:
    if not API_KEY:
        print("ERROR: set HEATGUARD_API_KEY before running this script.", file=sys.stderr)
        return 2

    endpoint = f"{BASE_URL}/api/v1/cameras/bulk-activate"
    headers = {"X-API-Key": API_KEY, "Content-Type": "application/json"}
    payload = build_payload()

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            f"Bulk activation failed: HTTP {exc.response.status_code}: "
            f"{exc.response.text}",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as exc:
        print(f"Could not reach HeatGuardAI: {exc}", file=sys.stderr)
        return 1

    result = response.json()
    print(f"Endpoint: {endpoint}")
    print(f"Status: {result.get('status')}")
    print(f"Activated: {result.get('activated_count', 0)}")
    print(f"Skipped duplicates: {result.get('skipped_count', 0)}")
    for node in result.get("nodes", []):
        print(
            f"- id={node.get('id')} name={node.get('name')} "
            f"location={node.get('location')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
