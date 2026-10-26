#!/usr/bin/env python3
"""Print the raw response from a physical Hikvision thermal camera's ISAPI route.

Required environment:
    HIKVISION_HOST       Camera IP/hostname (no scheme or path)
    HIKVISION_USERNAME   Camera username
    HIKVISION_PASSWORD   Camera password

Optional:
    HIKVISION_SCHEME     http (default) or https
    HIKVISION_CHANNEL    ISAPI channel number (default: 1)
    HIKVISION_AUTH       digest (default) or basic
    HIKVISION_TIMEOUT    Request timeout in seconds (default: 10)

This diagnostic performs one read-only GET. It does not register a camera or
submit telemetry to HeatGuard. Keep credentials in environment variables; do
not commit them or paste them into tickets/logs.
"""
from __future__ import annotations

import json
import os
import sys

import httpx


def main() -> int:
    host = os.getenv("HIKVISION_HOST", "").strip().rstrip("/")
    username = os.getenv("HIKVISION_USERNAME", "").strip()
    password = os.getenv("HIKVISION_PASSWORD", "")
    scheme = os.getenv("HIKVISION_SCHEME", "http").strip().lower()
    channel = os.getenv("HIKVISION_CHANNEL", "1").strip()
    auth_mode = os.getenv("HIKVISION_AUTH", "digest").strip().lower()

    if not host or not username or not password:
        print(
            "ERROR: Set HIKVISION_HOST, HIKVISION_USERNAME, and HIKVISION_PASSWORD.",
            file=sys.stderr,
        )
        return 2
    if scheme not in {"http", "https"}:
        print("ERROR: HIKVISION_SCHEME must be http or https.", file=sys.stderr)
        return 2
    if auth_mode not in {"digest", "basic"}:
        print("ERROR: HIKVISION_AUTH must be 'digest' or 'basic'.", file=sys.stderr)
        return 2
    if not channel.isdigit() or int(channel) < 1:
        print("ERROR: HIKVISION_CHANNEL must be a positive integer.", file=sys.stderr)
        return 2

    base_url = host if host.startswith(("http://", "https://")) else f"{scheme}://{host}"
    # Avoid accidentally applying the scheme twice when HOST includes one.
    url = f"{base_url}/ISAPI/Thermal/channels/{int(channel)}/thermometry/basicInfo"
    auth = (
        httpx.DigestAuth(username, password)
        if auth_mode == "digest"
        else (username, password)
    )
    try:
        timeout = float(os.getenv("HIKVISION_TIMEOUT", "10"))
        if timeout <= 0:
            raise ValueError
    except ValueError:
        print("ERROR: HIKVISION_TIMEOUT must be a positive number.", file=sys.stderr)
        return 2

    print(f"GET {url}")
    print(f"Authentication: {auth_mode}")
    try:
        with httpx.Client(auth=auth, timeout=timeout, follow_redirects=False) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        print(f"CONNECTION ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP status: {response.status_code}")
    print(f"Content-Type: {response.headers.get('content-type', '(not provided)')}")
    print("--- RAW RESPONSE BODY BEGIN ---")
    # Print the exact body decoded using the response's declared encoding.
    print(response.text, end="" if response.text.endswith("\n") else "\n")
    print("--- RAW RESPONSE BODY END ---")

    if response.is_error:
        print(
            "ERROR: Camera returned a non-success HTTP status; check network reachability, "
            "permissions, route support, and credentials.",
            file=sys.stderr,
        )
        return 1

    content_type = response.headers.get("content-type", "").lower()
    if "json" in content_type or response.text.lstrip().startswith(("{", "[")):
        try:
            payload = response.json()
        except ValueError:
            print("WARNING: Response looked like JSON but could not be parsed.", file=sys.stderr)
            return 1
        print("--- PARSED JSON KEYS ---")
        if isinstance(payload, dict):
            print(json.dumps(list(payload.keys()), indent=2))
        else:
            print(f"Top-level JSON type: {type(payload).__name__}")
    else:
        print(
            "NOTE: This camera returned non-JSON data (often XML for ISAPI). "
            "The raw payload above is authoritative; do not assume resp.json() will work."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
