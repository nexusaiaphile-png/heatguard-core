# HeatGuard Edge Agent (pilot MVP)

This agent runs on an authorised computer on the same local network as a compatible Hikvision thermal camera. It polls the camera's ISAPI thermometry endpoint and forwards max/average/min-temperature samples to HeatGuard Cloud over HTTPS. It is a foreground process for the pilot; install it as an operating-system service only after site-specific validation.

## Requirements

- Python 3.10+
- Network access from the edge computer to the camera's local IP
- Outbound HTTPS access to the HeatGuard API
- A registered HeatGuard camera ID and API key
- Camera credentials authorised by the site owner
- Camera model/firmware that supports `/ISAPI/Thermal/channels/{channel}/thermometry/basicInfo`

Install dependencies:

```bash
python3 -m pip install httpx
```

Set environment variables in the local shell or a protected service environment (never commit secrets):

```bash
export HIKVISION_CAMERA_IP="192.168.1.20"
export HIKVISION_USERNAME="site-authorised-camera-user"
export HIKVISION_PASSWORD="replace-with-camera-password"
export HEATGUARD_API_KEY="replace-with-HeatGuard-API-key"
export HEATGUARD_CAMERA_ID="123"
export HEATGUARD_LOCATION="Cold Storage"
export HEATGUARD_API_URL="https://heatguard-core-engine.onrender.com"
export HIKVISION_CHANNEL="1"
export HEATGUARD_POLL_INTERVAL_SECONDS="30"
python3 edge_agent.py
```

Use real values only on a site where access is authorised. The process logs successful submissions and errors; it does not install itself, conceal itself, or change camera settings.

## Current limitations (important)

- The telemetry API requires `camera_id`, `max_temp`, `avg_temp`, `min_temp`, and `location`. The edge agent submits the camera's parsed max/average/min values directly; older clients that omit max/min must be updated.
- Local spool retry is at-least-once. A network timeout can happen after the cloud stores a reading, so replay may create a duplicate. Production-grade deduplication requires an idempotency/event ID in the API.
- No agent heartbeat/health endpoint exists in the current API; the portal cannot yet show authoritative agent-connected status.
- The script polls and parses but has not been validated against a physical camera in this repository workflow. Confirm model/firmware response shape, credentials, units, TLS/API behaviour, and real alert delivery at a controlled pilot before relying on it.
- This agent is not a fire-safety certification or a substitute for the site's existing alarms and safety procedures.
