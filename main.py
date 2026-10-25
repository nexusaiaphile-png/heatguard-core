"""HeatGuardAI thermal anomaly monitoring service.

Configuration is supplied exclusively through environment variables. This module
uses Hikvision ISAPI thermal metadata, a rolling statistical baseline, optional
Gemini image review, and concurrent Twilio/email notifications.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import statistics
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosmtplib
import google.generativeai as genai
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import JSON, DateTime, Float, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from twilio.rest import Client as TwilioClient


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s [%(name)s] %(message)s", force=True)
logger = logging.getLogger("heatguard")
logging.getLogger("sqlalchemy.engine").setLevel(os.getenv("SQLALCHEMY_LOG_LEVEL", "WARNING"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql+psycopg2://localhost/heatguard"
    hikvision_base_url: str = ""
    hikvision_username: str = ""
    hikvision_password: str = ""
    hikvision_thermal_endpoint: str = "/ISAPI/Thermal/channels/1/thermometry/1"
    hikvision_poll_seconds: int = 15
    hikvision_sites_json: str = "[]"
    baseline_window: int = 120
    anomaly_sigma: float = 3.0
    anomaly_min_delta_c: float = 4.0
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-pro"
    gemini_review_enabled: bool = False
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    alert_sms_to: str = ""
    alert_whatsapp_from: str = ""
    alert_whatsapp_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    alert_email_to: str = ""
    alert_cooldown_seconds: int = 300


settings = Settings()


class Base(DeclarativeBase):
    pass


class ThermalReading(Base):
    __tablename__ = "thermal_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[str] = mapped_column(String(160), index=True)
    camera_id: Mapped[str] = mapped_column(String(160), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    temperature_c: Mapped[float] = mapped_column(Float)
    baseline_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    deviation_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    anomaly: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(40), default="hikvision_isapi")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=1800)
Base.metadata.create_all(engine)


@dataclass
class ThermalSample:
    temperature_c: float
    observed_at: datetime
    metadata: dict[str, Any]


class RollingBaseline:
    """Per-camera rolling baseline; uses median and MAD-resistant clipping."""

    def __init__(self, window: int = 120, sigma: float = 3.0, min_delta_c: float = 4.0):
        self.values: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=max(5, window)))
        self.sigma = sigma
        self.min_delta_c = min_delta_c

    def assess(self, key: str, value: float) -> tuple[float | None, float | None, bool]:
        history = self.values[key]
        if len(history) < 5:
            history.append(value)
            return None, None, False
        baseline = float(statistics.median(history))
        deviations = [abs(x - baseline) for x in history]
        mad = float(statistics.median(deviations))
        robust_sigma = max(1.4826 * mad, 0.5)
        delta = value - baseline
        z_score = delta / robust_sigma
        anomalous = delta >= self.min_delta_c and z_score > self.sigma
        logger.info("baseline_metric camera=%s value_c=%.2f baseline_c=%.2f delta_c=%.2f robust_sigma=%.3f z_score=%.2f threshold=%.2f anomaly=%s", key, value, baseline, delta, robust_sigma, z_score, self.sigma, anomalous)
        history.append(value)
        return baseline, delta, anomalous


baseline_engine = RollingBaseline(
    settings.baseline_window, settings.anomaly_sigma, settings.anomaly_min_delta_c
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_thermal_payload(payload: bytes | str) -> ThermalSample:
    """Extract a measured temperature from common Hikvision ISAPI XML/JSON-like XML.

    Hikvision firmware schemas vary. This parser intentionally only accepts
    clearly named measured-temperature fields; it does not treat alarm limits as
    measurements.
    """
    raw = payload.encode() if isinstance(payload, str) else payload
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Thermal ISAPI response was not valid XML") from exc

    candidates: list[tuple[str, str]] = []
    for element in root.iter():
        name = _local_name(element.tag)
        text = (element.text or "").strip()
        if text:
            candidates.append((name, text))

    preferred = {
        "emissivitytemperature", "objecttemperature", "maxtemperature",
        "averagetemperature", "temperature", "thermometrytemperature",
        "currenttemperature", "measuredtemperature",
    }
    found: float | None = None
    for name, value in candidates:
        if name in preferred:
            try:
                found = float(value)
                break
            except ValueError:
                continue
    if found is None:
        # Some firmware returns the measurement under a field named temp.
        for name, value in candidates:
            if name in {"temp", "temperaturevalue"}:
                try:
                    found = float(value)
                    break
                except ValueError:
                    continue
    if found is None or not np.isfinite(found) or not -100 <= found <= 2000:
        raise ValueError("No valid measured temperature found in ISAPI payload")

    return ThermalSample(
        temperature_c=float(found),
        observed_at=datetime.now(timezone.utc),
        metadata={name: value for name, value in candidates},
    )


class HikvisionISAPIClient:
    def __init__(self, base_url: str, username: str, password: str, endpoint: str):
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.client = httpx.AsyncClient(
            auth=httpx.DigestAuth(username, password),
            timeout=httpx.Timeout(12.0),
            verify=os.getenv("HIKVISION_TLS_VERIFY", "true").lower() == "true",
        )

    async def read_temperature(self) -> ThermalSample:
        if not self.base_url:
            raise RuntimeError("HIKVISION_BASE_URL is not configured")
        response = await self.client.get(self.base_url + self.endpoint)
        response.raise_for_status()
        return parse_thermal_payload(response.content)

    async def close(self) -> None:
        await self.client.aclose()


class AlertDispatcher:
    async def dispatch(self, subject: str, body: str) -> dict[str, str]:
        jobs = {
            "sms": self._send_sms(subject, body),
            "whatsapp": self._send_whatsapp(subject, body),
            "email": self._send_email(subject, body),
        }
        names = list(jobs)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        outcome: dict[str, str] = {}
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                logger.warning("%s alert failed: %s", name, result)
                outcome[name] = "failed"
            else:
                outcome[name] = result
        return outcome

    async def _send_sms(self, subject: str, body: str) -> str:
        if not (settings.twilio_account_sid and settings.twilio_auth_token and
                settings.twilio_from_number and settings.alert_sms_to):
            return "not_configured"
        client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
        message = await asyncio.to_thread(
            client.messages.create,
            from_=settings.twilio_from_number,
            to=settings.alert_sms_to,
            body=f"{subject}\n{body}",
        )
        return str(message.sid)

    async def _send_whatsapp(self, subject: str, body: str) -> str:
        if not (settings.twilio_account_sid and settings.twilio_auth_token and
                settings.alert_whatsapp_from and settings.alert_whatsapp_to):
            return "not_configured"
        client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
        message = await asyncio.to_thread(
            client.messages.create,
            from_=settings.alert_whatsapp_from,
            to=settings.alert_whatsapp_to,
            body=f"{subject}\n{body}",
        )
        return str(message.sid)

    async def _send_email(self, subject: str, body: str) -> str:
        if not (settings.smtp_host and settings.smtp_from and settings.alert_email_to):
            return "not_configured"
        await aiosmtplib.send(
            f"From: {settings.smtp_from}\r\nTo: {settings.alert_email_to}\r\n"
            f"Subject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}",
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_port == 587,
            use_tls=settings.smtp_port == 465,
        )
        return "sent"


alerts = AlertDispatcher()
last_alert_at: dict[str, float] = {}


async def review_with_gemini(site_id: str, camera_id: str, temperature: float,
                             baseline: float | None, deviation: float | None) -> str:
    """Text-only review of telemetry; image review needs an explicitly supplied image."""
    if not settings.gemini_review_enabled or not settings.gemini_api_key:
        return "Gemini review disabled; telemetry threshold triggered this event."
    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)
    prompt = (
        "You are reviewing a thermal-camera anomaly for an industrial safety operator. "
        "Do not claim to have seen an image. Summarize this telemetry conservatively and "
        "recommend a human check if warranted. "
        f"Site={site_id}; camera={camera_id}; measured_c={temperature:.2f}; "
        f"baseline_c={baseline}; deviation_c={deviation}."
    )
    response = await asyncio.to_thread(model.generate_content, prompt)
    response_text = (getattr(response, "text", None) or "Gemini returned no text.").strip()
    logger.info("gemini_response site=%s camera=%s payload=%r", site_id, camera_id, response_text)
    return response_text


async def record_sample(site_id: str, camera_id: str, sample: ThermalSample) -> dict[str, Any]:
    key = f"{site_id}:{camera_id}"
    baseline, deviation, anomaly = baseline_engine.assess(key, sample.temperature_c)
    row_data = {
        "site_id": site_id,
        "camera_id": camera_id,
        "observed_at": sample.observed_at,
        "temperature_c": sample.temperature_c,
        "baseline_c": baseline,
        "deviation_c": deviation,
        "anomaly": anomaly,
        "source": "hikvision_isapi",
        "metadata_json": sample.metadata,
    }
    with Session(engine) as session:
        session.add(ThermalReading(**row_data))
        session.commit()
        logger.info("database_commit table=thermal_readings site=%s camera=%s temperature_c=%.2f anomaly=%s observed_at=%s", site_id, camera_id, sample.temperature_c, anomaly, sample.observed_at.isoformat())

    if anomaly:
        now = time.monotonic()
        if now - last_alert_at.get(key, 0) >= settings.alert_cooldown_seconds:
            last_alert_at[key] = now
            review = await review_with_gemini(
                site_id, camera_id, sample.temperature_c, baseline, deviation
            )
            subject = f"HEATGUARDAI THERMAL ALERT — {site_id}"
            body = (
                f"Site: {site_id}\nCamera: {camera_id}\n"
                f"Measured temperature: {sample.temperature_c:.2f} °C\n"
                f"Rolling baseline: {baseline:.2f} °C\n"
                f"Deviation: {deviation:.2f} °C\n"
                f"Observed: {sample.observed_at.isoformat()}\n\n"
                f"Review: {review}\n\nHuman verification is required."
            )
            asyncio.create_task(alerts.dispatch(subject, body))
    return {
        "site_id": site_id,
        "camera_id": camera_id,
        "observed_at": sample.observed_at.isoformat(),
        "temperature_c": sample.temperature_c,
        "baseline_c": baseline,
        "deviation_c": deviation,
        "anomaly": anomaly,
    }


async def poll_site(site: dict[str, Any]) -> None:
    client = HikvisionISAPIClient(
        str(site.get("base_url") or settings.hikvision_base_url),
        str(site.get("username") or settings.hikvision_username),
        str(site.get("password") or settings.hikvision_password),
        str(site.get("endpoint") or settings.hikvision_thermal_endpoint),
    )
    try:
        while True:
            try:
                sample = await client.read_temperature()
                await record_sample(str(site["site_id"]), str(site["camera_id"]), sample)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Thermal poll failed for site=%s camera=%s",
                                 site.get("site_id"), site.get("camera_id"))
            await asyncio.sleep(max(5, settings.hikvision_poll_seconds))
    finally:
        await client.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks: list[asyncio.Task] = []
    try:
        import json
        try:
            sites = json.loads(settings.hikvision_sites_json)
            if not isinstance(sites, list):
                raise ValueError("HIKVISION_SITES_JSON must be a JSON array")
        except (ValueError, TypeError):
            logger.exception("Invalid HIKVISION_SITES_JSON; polling disabled")
            sites = []
        for site in sites:
            if isinstance(site, dict) and site.get("site_id") and site.get("camera_id"):
                tasks.append(asyncio.create_task(poll_site(site)))
        app.state.poll_tasks = tasks
        yield
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="HeatGuardAI Core", version="0.1.0", lifespan=lifespan)


class IngestReading(BaseModel):
    site_id: str = Field(min_length=1, max_length=160)
    camera_id: str = Field(min_length=1, max_length=160)
    temperature_c: float = Field(ge=-100, le=2000)
    observed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "heatguard-core"}


@app.post("/api/thermal/readings")
async def ingest_reading(payload: IngestReading) -> dict[str, Any]:
    observed_at = payload.observed_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="observed_at must include a timezone")
    sample = ThermalSample(
        temperature_c=payload.temperature_c,
        observed_at=observed_at,
        metadata=payload.metadata,
    )
    return await record_sample(payload.site_id, payload.camera_id, sample)


@app.get("/api/thermal/readings/latest")
def latest_readings(limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    with Session(engine) as session:
        rows = session.scalars(
            select(ThermalReading).order_by(ThermalReading.observed_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": row.id,
                "site_id": row.site_id,
                "camera_id": row.camera_id,
                "observed_at": row.observed_at.isoformat(),
                "temperature_c": row.temperature_c,
                "baseline_c": row.baseline_c,
                "deviation_c": row.deviation_c,
                "anomaly": row.anomaly,
            }
            for row in rows
        ]
