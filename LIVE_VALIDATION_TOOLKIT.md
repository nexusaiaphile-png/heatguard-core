# HeatGuard live validation toolkit

These commands target the deployed HeatGuard API. They are real requests, not dry runs.
Keep API keys and camera passwords private. Use a dedicated test camera and recipient
numbers you control. Bulk activation writes records to the production database; telemetry
writes readings and may attempt notifications.

## 1. Read the physical Hikvision camera's raw ISAPI response

Install dependencies if needed:

```bash
python3 -m pip install httpx
```

Set credentials in your shell (do not put secrets into the script or commit them):

```bash
export HIKVISION_HOST="192.168.1.20"
export HIKVISION_USERNAME="admin"
read -s HIKVISION_PASSWORD
export HIKVISION_PASSWORD
export HIKVISION_CHANNEL="1"
export HIKVISION_AUTH="digest"
```

Run:

```bash
python3 verify_hikvision_stream.py
```

The script makes one read-only GET to
`/ISAPI/Thermal/channels/{channel}/thermometry/basicInfo`, prints the HTTP status,
content type, and exact response body, and reports top-level keys if the camera
actually responds with JSON. Hikvision ISAPI may return XML; the script preserves
that response instead of falsely assuming it is JSON. The script defaults to Digest
authentication; set `HIKVISION_AUTH=basic` only if the camera is configured for it.
Use `HIKVISION_SCHEME=https` if the device has HTTPS enabled.

## 2. Set the HeatGuard API base URL and key

```bash
export HEATGUARD_API_URL="https://heatguard-core-engine.onrender.com"
export HEATGUARD_API_KEY="REPLACE_WITH_YOUR_REAL_API_KEY"
```

Do not share terminal history, screenshots, or logs containing the API key.

## 3. Bulk activation (writes production camera records)

First review the payload carefully. Use a unique test name and a reachable camera IP.
The endpoint skips existing/repeated names, so repeating a successful request may
return `skipped_count` rather than creating another record.

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$HEATGUARD_API_URL/api/v1/cameras/bulk-activate" \
  -H "X-API-Key: $HEATGUARD_API_KEY" \
  -H "Content-Type: application/json" \
  --data '{
    "cameras": [
      {
        "name": "SITE-TEST-THERMAL-01",
        "ip": "192.168.1.20",
        "location": "Pilot Site - Test Zone",
        "alert_numbers": "+27820000000",
        "alert_emails": null
      }
    ]
  }' | tee bulk-activation-response.json | python3 -m json.tool
```

Replace both the camera IP and recipient number with real values. This call creates
a production record; do not use a placeholder payload against production.

Check registered cameras (also authenticated):

```bash
curl --fail-with-body --silent --show-error \
  -H "X-API-Key: $HEATGUARD_API_KEY" \
  "$HEATGUARD_API_URL/cameras" | python3 -m json.tool
```

## 4. Submit one authenticated telemetry sample

This endpoint writes a reading. If enough history exists and the sample meets the
configured anomaly criteria, it may create an alert and attempt notifications. The
example temperature is deliberately a placeholder: replace it with a value approved
for your test plan. Do not inject extreme values into a live safety workflow without
a test-site owner approving the test.

```bash
export CAMERA_ID="REPLACE_WITH_REGISTERED_CAMERA_ID"
export TEST_TEMP_C="REPLACE_WITH_APPROVED_TEST_TEMPERATURE"
curl --fail-with-body --silent --show-error \
  -X POST "$HEATGUARD_API_URL/api/v1/telemetry/submit" \
  -H "X-API-Key: $HEATGUARD_API_KEY" \
  -H "Content-Type: application/json" \
  --data "{\"camera_id\":$CAMERA_ID,\"avg_temp\":$TEST_TEMP_C,\"location\":\"Pilot Site - Test Zone\"}" \
  | tee telemetry-response.json | python3 -m json.tool
```

Possible response statuses include `logged`, `anomaly_reviewed_not_genuine`, and
`anomaly_detected`. A logged sample alone does not prove the anomaly path was tested.

## 5. Inspect stored readings and alert records

Readings endpoint (raw telemetry is included):

```bash
curl --fail-with-body --silent --show-error \
  -H "X-API-Key: $HEATGUARD_API_KEY" \
  "$HEATGUARD_API_URL/readings?camera_id=$CAMERA_ID&limit=10" \
  | python3 -m json.tool
```

Alerts endpoint:

```bash
curl --fail-with-body --silent --show-error \
  -H "X-API-Key: $HEATGUARD_API_KEY" \
  "$HEATGUARD_API_URL/alerts?limit=20" | python3 -m json.tool
```

**Current implementation limitation:** `/alerts` does not include the database
`sent` field, and the application intentionally stores `sent=false` because the
SMS/WhatsApp/email helper functions do not return provider delivery receipts. A
successful HTTP response or completed coroutine only proves an attempt was processed,
not delivery to a recipient. Therefore, these existing endpoints cannot verify delivery
confirmation. Verify receipt using the provider's message status/console for the pilot,
or implement provider receipt persistence and an authenticated alert-detail/status route
before claiming delivery is confirmed.

## Suggested validation order

1. Confirm the camera is reachable from the machine running the script (often the
   machine must be on the same LAN/VPN as the camera).
2. Capture and inspect the camera's actual raw ISAPI payload and content type.
3. Confirm API authentication with a safe authenticated GET such as `/cameras`.
4. Activate only the explicitly approved test camera.
5. Submit a planned telemetry sample and save the JSON response.
6. Check stored readings/alerts and separately confirm notification status at the provider.
7. Record any mismatch between the camera's real keys/XML schema and the application's
   current `resp.json()` parser before enabling live monitoring.

The current production camera parser calls `resp.json()`; if the physical device returns
XML, this script will expose that mismatch, but does not modify production behavior.
