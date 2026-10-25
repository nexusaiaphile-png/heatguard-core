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
        service.SessionLocal = cls.TestSessionLocal
        service.settings.HEATGUARD_API_KEY = API_KEY

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

        headers = {"X-API-Key": API_KEY}
        for i in range(21):
            temperature = 31.0 + (0.1 if i % 2 else -0.1)
            response = self.client.post(
                "/api/v1/telemetry/submit",
                headers=headers,
                json={
                    "camera_id": camera_id,
                    "avg_temp": temperature,
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
                "avg_temp": 75.0,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
