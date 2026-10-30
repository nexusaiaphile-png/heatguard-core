"""Offline end-to-end verification of HeatGuardAI telemetry persistence and alerts.

Run: python test_pipeline_local.py
Uses an in-memory SQLite database and deterministic AI/notification stubs.
No network calls, real notifications, or production database writes are made.
"""
# Stub optional SDKs before importing main. This test must never contact providers.
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("twilio", MagicMock())
sys.modules.setdefault("twilio.rest", MagicMock())
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.generativeai", MagicMock())
sys.modules.setdefault("aiosmtplib", MagicMock())

import asyncio
import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main as service


API_KEY = "local-test-key-only"
TEST_LOCATION = "Synthetic Test Zone - NOT A REAL FIRE"


class LocalTelemetryPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestSessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=cls.engine
        )
        service.Base.metadata.create_all(bind=cls.engine)
        cls.original_session_local = service.SessionLocal
        cls.original_api_key = service.settings.HEATGUARD_API_KEY
        cls.original_activation_token = service.settings.API_SECURITY_TOKEN
        service.SessionLocal = cls.TestSessionLocal
        service.settings.HEATGUARD_API_KEY = API_KEY
        service.settings.API_SECURITY_TOKEN = "local-installer-token-only"

        # Deterministic review guarantees the anomaly path can be tested offline.
        cls.gemini_patch = patch.object(
            service,
            "gemini_review",
            new=AsyncMock(return_value={
                "is_genuine_spike": True,
                "risk_level": "Critical",
                "summary": "Synthetic local-test anomaly.",
                "detailed_analysis": "Deterministic local test review.",
                "recommended_action": "No real action; local test only.",
            }),
        )
        cls.sms_patch = patch.object(service, "send_sms", new=AsyncMock())
        cls.whatsapp_patch = patch.object(service, "send_whatsapp_alert", new=AsyncMock())
        cls.email_patch = patch.object(service, "send_email", new=AsyncMock())
        cls.gemini_patch.start()
        cls.sms_patch.start()
        cls.whatsapp_patch.start()
        cls.email_patch.start()
        cls.client_context = TestClient(service.app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        cls.gemini_patch.stop()
        cls.sms_patch.stop()
        cls.whatsapp_patch.stop()
        cls.email_patch.stop()
        service.SessionLocal = cls.original_session_local
        service.settings.HEATGUARD_API_KEY = cls.original_api_key
        service.settings.API_SECURITY_TOKEN = cls.original_activation_token
        service.Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        # Each test starts with an empty schema; no shared or production DB.
        service.Base.metadata.drop_all(bind=self.engine)
        service.Base.metadata.create_all(bind=self.engine)

    def test_authentication_is_required(self):
        response = self.client.get("/cameras")
        self.assertEqual(response.status_code, 401)
        response = self.client.get("/cameras", headers={"X-API-Key": "wrong"})
        self.assertEqual(response.status_code, 401)
        response = self.client.get("/cameras", headers={"X-API-Key": API_KEY})
        self.assertEqual(response.status_code, 200)

    def test_camera_bulk_activation_requires_token_and_registers_camera(self):
        payload = {"cameras": [{
            "name": "Local portal activation test",
            "ip": "192.0.2.44",
            "location": "Synthetic Test Site",
            "alert_numbers": "+27820000000",
            "alert_emails": None,
        }]}
        denied = self.client.post("/api/v1/cameras/bulk-activate", json=payload)
        self.assertEqual(denied.status_code, 403)

        response = self.client.post(
            "/api/v1/cameras/bulk-activate",
            headers={"X-HeatGuard-Token": "local-installer-token-only"},
            json=payload,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["activated_count"], 1)
        self.assertEqual(body["skipped_count"], 0)
        self.assertEqual(body["nodes"][0]["name"], "Local portal activation test")

    def test_activation_portal_cors_preflight(self):
        response = self.client.options(
            "/api/v1/cameras/bulk-activate",
            headers={
                "Origin": "https://heatguard-ai-website.onrender.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-heatguard-token",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "https://heatguard-ai-website.onrender.com",
        )

    def test_21_readings_persist_and_anomaly_creates_alert(self):
        with self.TestSessionLocal() as db:
            camera = service.Camera(
                name="HeatGuardAI pipeline validation (synthetic)",
                ip="192.0.2.1",
                location=TEST_LOCATION,
                alert_numbers="",
                alert_emails=None,
                is_active=False,
            )
            db.add(camera)
            db.commit()
            db.refresh(camera)
            camera_id = camera.id

        headers = {"X-HeatGuard-Token": "local-installer-token-only"}
        for i in range(21):
            temperature = 31.0 + (0.1 if i % 2 else -0.1)
            response = self.client.post(
                "/api/v1/telemetry/submit",
                headers=headers,
                json={
                    "camera_id": camera_id,
                    "max_temp": temperature + 2.0,
                    "avg_temp": temperature,
                    "min_temp": temperature - 3.0,
                    "location": TEST_LOCATION,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "logged")

        anomaly = self.client.post(
            "/api/v1/telemetry/submit",
            headers=headers,
            json={
                "camera_id": camera_id,
                "max_temp": 82.5,
                "avg_temp": 75.0,
                "min_temp": 61.0,
                "location": TEST_LOCATION,
            },
        )
        self.assertEqual(anomaly.status_code, 200, anomaly.text)
        body = anomaly.json()
        self.assertEqual(body["status"], "anomaly_detected")
        self.assertIn("alert_id", body)

        readings_response = self.client.get(
            "/readings", headers=headers,
            params={"camera_id": camera_id, "limit": 100},
        )
        self.assertEqual(readings_response.status_code, 200)
        readings = readings_response.json()
        self.assertEqual(len(readings), 22)
        self.assertEqual(sum(r["location"] == TEST_LOCATION for r in readings), 22)
        self.assertTrue(any(r["avg_temp"] == 75.0 for r in readings))
        with self.TestSessionLocal() as db:
            peak = db.query(service.TemperatureReading).filter(
                service.TemperatureReading.camera_id == camera_id,
                service.TemperatureReading.avg_temp == 75.0,
            ).one()
            self.assertEqual(peak.max_temp, 82.5)
            self.assertEqual(peak.min_temp, 61.0)
            self.assertEqual(
                peak.raw_data,
                '{"source": "external_edge_agent_node"}',
            )

        alerts_response = self.client.get(
            "/alerts", headers=headers, params={"limit": 100}
        )
        self.assertEqual(alerts_response.status_code, 200)
        matches = [
            alert for alert in alerts_response.json()
            if alert["camera_id"] == camera_id and alert["location"] == TEST_LOCATION
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["temp"], 75.0)

        # Synthetic camera has no recipients: no external notification calls.
        service.send_sms.assert_not_called() if hasattr(service.send_sms, "assert_not_called") else None
        self.sms_patch.new.assert_not_awaited()
        self.whatsapp_patch.new.assert_not_awaited()


class HikvisionThermalParserTest(unittest.IsolatedAsyncioTestCase):
    """Offline parser tests; mocked HTTP only, no camera or provider access."""

    XML_PAYLOAD = """<?xml version="1.0" encoding="UTF-8"?>
    <ThermometryBasicInfo xmlns="http://www.hikvision.com/ver20/XMLSchema">
      <channelName>Test Zone</channelName>
      <temperatureInfo>
        <maxTemperature>82.5</maxTemperature>
        <averageTemperature>46.25</averageTemperature>
        <minTemperature>21.0</minTemperature>
      </temperatureInfo>
    </ThermometryBasicInfo>"""

    JSON_PAYLOAD = {
        "ThermometryBasicInfo": {
            "channel": {
                "temperature": {
                    "maxTemperature": 82.5,
                    "averageTemperature": 46.25,
                    "minTemperature": 21.0,
                }
            },
            "channelName": "Nested Test Zone",
        }
    }

    async def _call_parser(self, body, content_type="application/xml"):
        class FakeResponse:
            text = body
            headers = {"content-type": content_type}

            @staticmethod
            def raise_for_status():
                return None

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url):
                return FakeResponse()

        with patch.object(service.httpx, "AsyncClient", FakeClient):
            client = service.HikvisionThermalClient("192.0.2.10")
            return await client.get_temperature_data()

    async def test_valid_namespaced_xml_payload(self):
        result = await self._call_parser(self.XML_PAYLOAD)
        self.assertEqual(result["max_temp"], 82.5)
        self.assertEqual(result["avg_temp"], 46.25)
        self.assertEqual(result["min_temp"], 21.0)
        self.assertEqual(result["location"], "Test Zone")

    async def test_nested_json_payload(self):
        import json
        result = await self._call_parser(
            json.dumps(self.JSON_PAYLOAD), "application/json"
        )
        self.assertEqual(result["max_temp"], 82.5)
        self.assertEqual(result["avg_temp"], 46.25)
        self.assertEqual(result["min_temp"], 21.0)
        self.assertEqual(result["location"], "Nested Test Zone")

    async def test_malformed_payload_raises_explicit_value_error(self):
        with self.assertRaisesRegex(
            ValueError,
            "^Hikvision telemetry parsing exception: Malformed payload format$",
        ):
            await self._call_parser("this is not JSON or XML", "text/plain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
