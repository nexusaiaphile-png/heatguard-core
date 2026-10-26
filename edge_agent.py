#!/usr/bin/env python3
"""HeatGuard Edge Agent (pilot MVP).

Runs on an authorised computer on the camera's local network. Pulls Hikvision
thermal basicInfo data and forwards average temperature to HeatGuard Cloud.
Requires explicit operator configuration; it does not install itself or hide
its process. Configure through environment variables only.
"""
from __future__ import annotations

import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import re

import httpx

LOG = logging.getLogger("heatguard_edge")


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _find_value(source: Any, names: set[str]) -> Any | None:
    if isinstance(source, dict):
        for key, value in source.items():
            if _key(key) in names and value not in (None, ""):
                return value
        for value in source.values():
            found = _find_value(value, names)
            if found is not None:
                return found
    elif isinstance(source, list):
        for value in source:
            found = _find_value(value, names)
            if found is not None:
                return found
    return None


def parse_thermal_payload(body: str) -> dict[str, Any]:
    """Parse supported JSON/XML payloads; reject missing/non-numeric temperatures."""
    body = body.strip()
    if not body:
        raise ValueError("Camera returned an empty payload")

    if body.startswith("<"):
        root = ET.fromstring(body)
        data: dict[str, Any] = {}
        for node in root.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if node.text and node.text.strip():
                data[tag] = node.text.strip()
    else:
        data = json.loads(body)
        if not isinstance(data, (dict, list)):
            raise ValueError("Unexpected thermal payload type")

    max_value = _find_value(data, {"maxtemperature"})
    avg_value = _find_value(data, {"averagetemperature", "avgtemperature"})
    min_value = _find_value(data, {"mintemperature"})
    if max_value is None or avg_value is None or min_value is None:
        raise ValueError("Payload must contain max, average and minimum temperature")

    try:
        maximum, average, minimum = float(max_value), float(avg_value), float(min_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Temperature values must be numeric") from exc

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "max_temp": maximum,
        "avg_temp": average,
        "min_temp": minimum,
        "location": _find_value(data, {"channelname"}) or os.getenv("HEATGUARD_LOCATION", "Unknown Zone"),
    }


class EdgeAgent:
    def __init__(self) -> None:
        self.camera_ip = os.environ["HIKVISION_CAMERA_IP"]
        self.camera_id = int(os.environ["HEATGUARD_CAMERA_ID"])
        self.api_key = os.environ["HEATGUARD_API_KEY"]
        self.api_url = os.getenv("HEATGUARD_API_URL", "https://heatguard-core-engine.onrender.com").rstrip("/")
        self.username = os.environ["HIKVISION_USERNAME"]
        self.password = os.environ["HIKVISION_PASSWORD"]
        self.channel = int(os.getenv("HIKVISION_CHANNEL", "1"))
        self.location = os.getenv("HEATGUARD_LOCATION", "Unknown Zone")
        self.interval = max(5, int(os.getenv("HEATGUARD_POLL_INTERVAL_SECONDS", "30")))
        self.state_dir = Path(os.getenv("HEATGUARD_EDGE_STATE_DIR", "./heatguard-edge-state"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.spool_path = self.state_dir / "pending.jsonl"

    def read_camera(self) -> dict[str, Any]:
        url = f"http://{self.camera_ip}/ISAPI/Thermal/channels/{self.channel}/thermometry/basicInfo"
        with httpx.Client(auth=(self.username, self.password), timeout=8.0) as client:
            response = client.get(url)
            response.raise_for_status()
        result = parse_thermal_payload(response.text)
        result["location"] = self.location if self.location != "Unknown Zone" else result["location"]
        return result

    def _spool(self, event: dict[str, Any]) -> None:
        with self.spool_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event) + "\n")

    def _submit(self, event: dict[str, Any]) -> None:
        # Existing API accepts average temperature and location only. The cloud
        # endpoint currently derives max/min, so do not imply those values are stored.
        payload = {
            "camera_id": self.camera_id,
            "avg_temp": event["avg_temp"],
            "location": event["location"],
        }
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{self.api_url}/api/v1/telemetry/submit",
                json=payload,
                headers={"X-API-Key": self.api_key},
            )
            response.raise_for_status()

    def run_once(self) -> bool:
        event: dict[str, Any] | None = None
        try:
            event = self.read_camera()
            self._submit(event)
            LOG.info("Telemetry submitted: camera_id=%s avg_temp=%s", self.camera_id, event["avg_temp"])
            return True
        except (httpx.HTTPError, ValueError, ET.ParseError, OSError, KeyError) as exc:
            # Spool only a successfully parsed sample. Retrying is at-least-once:
            # a timeout may occur after cloud commit, so duplicates are possible
            # until the API supports idempotency keys.
            if event is not None:
                try:
                    self._spool({"queued_at": datetime.now(timezone.utc).isoformat(), **event})
                except OSError:
                    LOG.exception("Could not save pending telemetry locally")
            LOG.warning("Telemetry cycle failed: %s", exc)
            return False

    def flush_spool(self) -> None:
        if not self.spool_path.exists():
            return
        lines = self.spool_path.read_text(encoding="utf-8").splitlines()
        remaining: list[str] = []
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
                self._submit(event)
            except Exception as exc:
                LOG.warning("Could not submit queued sample %d: %s", index + 1, exc)
                remaining.extend(lines[index:])
                break
        temporary = self.spool_path.with_suffix(".tmp")
        temporary.write_text("".join(line + "\n" for line in remaining), encoding="utf-8")
        temporary.replace(self.spool_path)

    def run_forever(self) -> None:
        LOG.info("HeatGuard Edge Agent started for camera_id=%s", self.camera_id)
        while True:
            self.flush_spool()
            self.run_once()
            time.sleep(self.interval)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    required = (
        "HIKVISION_CAMERA_IP", "HIKVISION_USERNAME", "HIKVISION_PASSWORD",
        "HEATGUARD_API_KEY", "HEATGUARD_CAMERA_ID",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    EdgeAgent().run_forever()


if __name__ == "__main__":
    main()
