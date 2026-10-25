#!/usr/bin/env python3
"""Interactive HeatGuard AI installer portal for atomic camera batch activation.

Requirements:
    python -m pip install httpx

Configuration:
    HEATGUARD_API_KEY  Required API key for the production gateway.
    HEATGUARD_API_URL  Optional base URL; defaults to the live Render service.

WARNING: A successful run registers camera records in the configured production
database. Confirm camera details and notification numbers before submitting.
"""
from __future__ import annotations

import ipaddress
import os
import sys
from typing import Any

import httpx


PORTAL_TITLE = "HEATGUARD AI - INSTALLER PORTAL v1.0"
DEFAULT_API_URL = "https://heatguard-core-engine.onrender.com"
BULK_ACTIVATION_PATH = "/api/v1/cameras/bulk-activate"
REQUEST_TIMEOUT_SECONDS = 30.0


def prompt_required(prompt: str) -> str:
    """Prompt until a non-empty value is entered."""
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("This field is required. Please try again.")


def prompt_camera_ip() -> str:
    """Prompt for a syntactically valid IPv4 or IPv6 address."""
    while True:
        value = prompt_required("Enter Camera Local Network IP Address: ")
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            print("Please enter a valid IPv4 or IPv6 address (for example, 192.168.1.20).")


def prompt_mobile_numbers() -> str:
    """Collect comma-separated notification numbers and normalize whitespace."""
    while True:
        raw = prompt_required(
            "Enter Primary Emergency Notification Mobile Numbers (comma-separated): "
        )
        numbers = [number.strip() for number in raw.split(",")]
        if all(numbers) and all(any(char.isdigit() for char in number) for number in numbers):
            return ",".join(numbers)
        print("Enter one or more mobile numbers separated by commas.")


def collect_camera(index: int) -> dict[str, Any]:
    """Collect one camera using the API's CameraCreate field names."""
    print(f"\n--- Camera {index} ---")
    return {
        "name": prompt_required(
            "Enter Camera Name / Tag (e.g., Conveyor-Belt-Zone-A): "
        ),
        "ip": prompt_camera_ip(),
        "location": prompt_required("Enter Facility Location: "),
        "alert_numbers": prompt_mobile_numbers(),
        # CameraCreate accepts this field as optional; no email prompt is required.
        "alert_emails": None,
    }


def collect_batch() -> list[dict[str, Any]]:
    """Collect at least one camera and let the technician add more."""
    cameras = [collect_camera(1)]
    while True:
        answer = input(
            "Do you want to add another camera to this installation batch? (y/n): "
        ).strip().lower()
        if answer in {"n", "no"}:
            return cameras
        if answer in {"y", "yes"}:
            cameras.append(collect_camera(len(cameras) + 1))
            continue
        print("Please enter 'y' to add another camera or 'n' to submit the batch.")


def print_activation_grid(nodes: list[dict[str, Any]]) -> None:
    """Print a compact grid of successfully activated camera records."""
    columns = [
        ("DATABASE ID", "id"),
        ("CAMERA NAME / TAG", "name"),
        ("FACILITY LOCATION", "location"),
    ]
    rows = [
        [str(node.get(key, "")) for _, key in columns]
        for node in nodes
    ]
    widths = [
        max(len(title), *(len(row[i]) for row in rows)) if rows else len(title)
        for i, (title, _) in enumerate(columns)
    ]

    def border(left: str, middle: str, right: str) -> str:
        return left + middle.join("─" * (width + 2) for width in widths) + right

    def row(values: list[str]) -> str:
        return "│" + "│".join(
            f" {value:<{widths[i]}} " for i, value in enumerate(values)
        ) + "│"

    print("\nACTIVATION BREAKDOWN")
    print(border("┌", "┬", "┐"))
    print(row([title for title, _ in columns]))
    print(border("├", "┼", "┤"))
    for values in rows:
        print(row(values))
    print(border("└", "┴", "┘"))


def submit_batch(cameras: list[dict[str, Any]]) -> int:
    """Submit the complete list in one authenticated production API request."""
    api_key = os.getenv("HEATGUARD_API_KEY", "").strip()
    if not api_key:
        print(
            "\nERROR: HEATGUARD_API_KEY is not set. Nothing was submitted.\n"
            "Set it in your environment, then run this script again.",
            file=sys.stderr,
        )
        return 2

    base_url = os.getenv("HEATGUARD_API_URL", DEFAULT_API_URL).strip().rstrip("/")
    endpoint = f"{base_url}{BULK_ACTIVATION_PATH}"
    payload = {"cameras": cameras}

    print(f"\nSubmitting {len(cameras)} camera(s) as one batch to:")
    print(f"  {endpoint}")
    print("This will create production database records. Sending request...")

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(
                endpoint,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(
            f"\nACTIVATION FAILED — HTTP {exc.response.status_code}\n"
            f"Server response: {exc.response.text}",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as exc:
        print(f"\nCONNECTION FAILED — {exc}", file=sys.stderr)
        return 1

    try:
        result = response.json()
    except ValueError:
        print("\nERROR: The gateway returned an invalid JSON response.", file=sys.stderr)
        return 1

    activated = result.get("nodes", [])
    print("\nBATCH RESULT")
    print(f"  Status:             {result.get('status', 'unknown')}")
    print(f"  Activated:          {result.get('activated_count', len(activated))}")
    print(f"  Skipped duplicates: {result.get('skipped_count', 0)}")

    if activated:
        print_activation_grid(activated)
    else:
        print("\nNo new camera records were activated in this batch.")

    if result.get("status") != "success":
        print("\nThe server did not report a successful batch status.", file=sys.stderr)
        return 1

    print("\nBatch submission completed.")
    return 0


def main() -> int:
    print("=" * len(PORTAL_TITLE))
    print(PORTAL_TITLE)
    print("=" * len(PORTAL_TITLE))
    print("Enter each camera's details. The full batch is sent only after you finish.\n")

    try:
        cameras = collect_batch()
    except (EOFError, KeyboardInterrupt):
        print("\n\nCancelled. No activation request was sent.")
        return 130

    print(f"\nBatch ready: {len(cameras)} camera(s).")
    confirm = input(
        "Submit this batch to the PRODUCTION gateway now? (y/n): "
    ).strip().lower()
    if confirm not in {"y", "yes"}:
        print("Cancelled. No activation request was sent.")
        return 0

    return submit_batch(cameras)


if __name__ == "__main__":
    raise SystemExit(main())
