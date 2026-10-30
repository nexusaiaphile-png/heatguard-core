""" 
HeatGuard AI - Complete Single-File System
AI-powered thermal heat monitoring & early fire detection
Matches the original video workflow exactly
"""

import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, Depends, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from twilio.rest import Client
import google.generativeai as genai
from email.message import EmailMessage
import aiosmtplib

# ====================== CONFIGURATION ======================
class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "sqlite:///./heatguard.db"
    HEATGUARD_API_KEY: str = ""
    API_SECURITY_TOKEN: str = ""

    # Hikvision
    HIKVISION_USERNAME: str = "admin"
    HIKVISION_PASSWORD: str = "your_password"
    
    # Gemini
    GEMINI_API_KEY: str = "your_gemini_api_key"

    # Twilio SMS
    TWILIO_ACCOUNT_SID: str = "your_sid"
    TWILIO_AUTH_TOKEN: str = "your_token"
    TWILIO_FROM_NUMBER: str = "+1234567890"
    TWILIO_WHATSAPP_FROM_NUMBER: str = ""  # e.g. whatsapp:+14155238886 for an enabled Twilio sender

    # Email (optional)
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = "your_email@gmail.com"
    SMTP_PASSWORD: str = "your_app_password"
    ALERT_EMAIL_FROM: str = "alerts@heatguard.ai"

    # Detection Settings
    BASELINE_WINDOW_HOURS: int = 72
    SPIKE_Z_SCORE: float = 2.8
    MIN_TEMP_RISE: float = 7.0  # °C

    class Config:
        env_file = ".env"

settings = Settings()

# Dedicated credential for edge-agent telemetry and installer camera activation.
# auto_error=False lets us consistently return HTTP 403 for a missing header.
heatguard_token_header = APIKeyHeader(name="X-HeatGuard-Token", auto_error=False)

def require_heatguard_token(token: Optional[str] = Security(heatguard_token_header)) -> None:
    if not token:
        raise HTTPException(status_code=403, detail="Forbidden")
    expected = settings.API_SECURITY_TOKEN.strip()
    if not expected:
        logging.error("API_SECURITY_TOKEN is not configured; protected endpoint denied")
        raise HTTPException(status_code=503, detail="API security is not configured")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")

# ====================== DATABASE ======================
engine = create_engine(settings.DATABASE_URL, pool_size=20, max_overflow=10, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Camera(Base):
    __tablename__ = "cameras"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True)
    ip = Column(String)
    location = Column(String)
    alert_numbers = Column(Text)  # comma separated
    alert_emails = Column(Text)
    is_active = Column(Boolean, default=True)

class TemperatureReading(Base):
    __tablename__ = "readings"
    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow)
    max_temp = Column(Float)
    avg_temp = Column(Float)
    min_temp = Column(Float)
    location = Column(String)
    raw_data = Column(Text)

class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)
    camera_id = Column(Integer)
    timestamp = Column(DateTime, default=datetime.utcnow)
    location = Column(String)
    current_temp = Column(Float)
    baseline_mean = Column(Float)
    z_score = Column(Float)
    risk_level = Column(String)
    summary = Column(Text)
    detailed_analysis = Column(Text)
    evidence_url = Column(String)
    sent = Column(Boolean, default=False)

Base.metadata.create_all(bind=engine)

def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    expected = settings.HEATGUARD_API_KEY.strip()
    if not expected:
        raise HTTPException(status_code=503, detail="API authentication is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

# ====================== MODELS ======================
class CameraCreate(BaseModel):
    name: str
    ip: str
    location: str
    alert_numbers: str  # "+2782...,+2783..."
    alert_emails: Optional[str] = None

class CameraBatchCreate(BaseModel):
    """Validated batch of camera nodes for installer/portal activation."""
    cameras: List[CameraCreate] = Field(min_length=1, max_length=500)

class ReadingOut(BaseModel):
    id: int
    camera_id: int
    timestamp: datetime
    avg_temp: float
    location: str

class ExternalTelemetry(BaseModel):
    avg_temp: float = Field(ge=-50, le=1000)
    location: str = Field(min_length=1, max_length=255)

class TelemetrySubmission(BaseModel):
    camera_id: int
    avg_temp: float = Field(ge=-50, le=1000)
    max_temp: float = Field(ge=-50, le=1000)
    min_temp: float = Field(ge=-50, le=1000)
    location: str = Field(min_length=1, max_length=255)

# ====================== SERVICES ======================

# ----- Hikvision Client -----
class HikvisionThermalClient:
    def __init__(self, ip: str, channel: int = 1):
        self.base_url = f"http://{ip}"
        self.auth = (settings.HIKVISION_USERNAME, settings.HIKVISION_PASSWORD)
        self.channel = channel

    async def get_temperature_data(self) -> Dict[str, Any]:
        """Pull radiometric data from Hikvision thermal camera (JSON or XML)."""
        import re
        import xml.etree.ElementTree as ET

        url = f"{self.base_url}/ISAPI/Thermal/channels/{self.channel}/thermometry/basicInfo"
        malformed = "Hikvision telemetry parsing exception: Malformed payload format"

        def normalize_key(key: Any) -> str:
            return re.sub(r"[^a-z0-9]", "", str(key).lower())

        def find_nested_value(source: Any, names: set[str]) -> Optional[Any]:
            if isinstance(source, dict):
                for key, value in source.items():
                    if normalize_key(key) in names and value not in (None, ""):
                        return value
                for value in source.values():
                    found = find_nested_value(value, names)
                    if found is not None:
                        return found
            elif isinstance(source, list):
                for value in source:
                    found = find_nested_value(value, names)
                    if found is not None:
                        return found
            return None

        try:
            async with httpx.AsyncClient(auth=self.auth, timeout=8.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                body = resp.text.strip()
                content_type = resp.headers.get("content-type", "").lower()

            if not body:
                raise ValueError(malformed)

            # Prefer XML when the body is XML, even if content-type is inaccurate.
            if body.startswith("<") or "xml" in content_type:
                try:
                    root = ET.fromstring(body)
                    data: Dict[str, Any] = {}
                    for node in root.iter():
                        tag = node.tag.rsplit("}", 1)[-1]
                        if node.text and node.text.strip():
                            data[tag] = node.text.strip()
                except (ET.ParseError, ValueError, TypeError):
                    raise ValueError(malformed) from None
            else:
                # Some camera models/configurations return application/json.
                try:
                    data = json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    raise ValueError(malformed) from None
                if not isinstance(data, (dict, list)):
                    raise ValueError(malformed)

            max_value = find_nested_value(data, {"maxtemperature"})
            avg_value = find_nested_value(data, {"averagetemperature", "avgtemperature"})
            min_value = find_nested_value(data, {"mintemperature"})
            location_value = find_nested_value(data, {"channelname"})

            if max_value is None or avg_value is None or min_value is None:
                raise ValueError(malformed)

            try:
                max_temp = float(max_value)
                avg_temp = float(avg_value)
                min_temp = float(min_value)
            except (TypeError, ValueError):
                raise ValueError(malformed) from None

            return {
                "timestamp": datetime.utcnow(),
                "max_temp": max_temp,
                "avg_temp": avg_temp,
                "min_temp": min_temp,
                "location": location_value or "Unknown Zone",
                "raw": data,
            }
        except ValueError as e:
            if str(e) == malformed:
                logging.error("Hikvision telemetry parsing failed for %s", self.base_url)
                raise
            logging.critical(
                "Live thermal telemetry unavailable for camera %s: %s",
                self.base_url,
                e,
                exc_info=True,
            )
            raise RuntimeError(
                f"Live thermal telemetry unavailable for camera {self.base_url}"
            ) from e
        except Exception as e:
            logging.critical(
                "Live thermal telemetry unavailable for camera %s; no synthetic reading will be generated: %s",
                self.base_url,
                e,
                exc_info=True,
            )
            raise RuntimeError(
                f"Live thermal telemetry unavailable for camera {self.base_url}"
            ) from e

# ----- Baseline + Anomaly Detection -----
def detect_spike(db: Session, camera_id: int, current_temp: float, location: str):
    window_start = datetime.utcnow() - timedelta(hours=settings.BASELINE_WINDOW_HOURS)
    
    readings = db.query(TemperatureReading).filter(
        TemperatureReading.camera_id == camera_id,
        TemperatureReading.timestamp >= window_start
    ).all()

    if len(readings) < 20:
        return False, None  # Not enough data yet

    temps = [r.avg_temp for r in readings]
    mean = float(np.mean(temps))
    std = float(np.std(temps)) if len(temps) > 1 else 0.1

    z_score = (current_temp - mean) / std if std > 0 else 0
    temp_rise = current_temp - mean

    is_spike = (z_score >= settings.SPIKE_Z_SCORE) and (temp_rise >= settings.MIN_TEMP_RISE)

    context = {
        "current_temp": round(current_temp, 1),
        "baseline_mean": round(mean, 1),
        "baseline_std": round(std, 1),
        "z_score": round(z_score, 2),
        "temp_rise": round(temp_rise, 1),
        "location": location,
        "sample_count": len(readings)
    }
    return is_spike, context

# ----- Gemini AI Reviewer (exactly like the video) -----
genai.configure(api_key=settings.GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-pro")

async def gemini_review(context: dict, history: List[float]) -> dict:
    prompt = f"""
You are an expert fire safety analyst reviewing thermal camera data for early fire detection.

Location: {context['location']}
Current temperature: {context['current_temp']}°C
Usual heat (baseline): {context['baseline_mean']}°C ± {context['baseline_std']}°C
Temperature rise: +{context['temp_rise']}°C
Statistical significance (z-score): {context['z_score']}
Samples used for baseline: {context['sample_count']}

Recent temperature history (°C): {history[-12:]}

Task:
1. Decide if this is a genuine unusual heat spike that requires attention.
2. Assign risk level: Low / Medium / High / Critical
3. Write a clear, professional one-sentence summary for SMS to the fire response team.
4. Write a short detailed analysis (2 sentences).
5. Recommend immediate action.

Return ONLY valid JSON in this exact format:
{{
  "is_genuine_spike": true,
  "risk_level": "High",
  "summary": "Sudden heat spike detected at Warehouse Zone B – temperature rose 11.4°C above normal.",
  "detailed_analysis": "The current reading is statistically significant and outside normal operating range. Immediate visual inspection is recommended before the situation escalates.",
  "recommended_action": "Dispatch team to inspect Zone B within 5 minutes"
}}
"""
    try:
        response = await gemini_model.generate_content_async(prompt)
        text = response.text.strip()
        # Clean markdown if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        return {
            "is_genuine_spike": True,
            "risk_level": "High",
            "summary": f"Heat anomaly detected at {context['location']} – {context['current_temp']}°C (+{context['temp_rise']}°C)",
            "detailed_analysis": "AI review failed – treating as genuine spike based on statistical detection.",
            "recommended_action": "Immediate inspection required"
        }

# ----- Alerts -----
twilio_client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

async def send_sms(numbers: List[str], message: str):
    for number in numbers:
        try:
            twilio_client.messages.create(
                body=message,
                from_=settings.TWILIO_FROM_NUMBER,
                to=number.strip()
            )
            logging.info(f"SMS sent to {number}")
        except Exception as e:
            logging.error(f"SMS failed to {number}: {e}")

async def send_whatsapp_alert(
    phone: str,
    location: str,
    temperature: float,
    temp_rise: float,
    risk_level: Optional[str],
    recommended_action: Optional[str],
    camera_id: int,
):
    """Send a WhatsApp alert through Twilio when a WhatsApp sender is configured."""
    sender = settings.TWILIO_WHATSAPP_FROM_NUMBER.strip()
    if not sender:
        logging.warning("WhatsApp alert skipped: TWILIO_WHATSAPP_FROM_NUMBER is not configured")
        return
    recipient = phone.strip()
    if not recipient.startswith("whatsapp:"):
        recipient = f"whatsapp:{recipient}"
    if not sender.startswith("whatsapp:"):
        sender = f"whatsapp:{sender}"
    body = (
        f"HEATGUARD {risk_level or 'HIGH'} ALERT\\n"
        f"Location: {location}\\nCamera ID: {camera_id}\\n"
        f"Temperature: {temperature:.1f}°C (+{temp_rise:.1f}°C)\\n"
        f"Action: {recommended_action or 'Inspect immediately'}"
    )
    try:
        await asyncio.to_thread(
            twilio_client.messages.create,
            body=body,
            from_=sender,
            to=recipient,
        )
        logging.info("WhatsApp alert sent to %s", phone)
    except Exception:
        logging.exception("WhatsApp alert failed for %s", phone)

async def send_email(emails: List[str], subject: str, body: str):
    if not emails:
        return
    msg = EmailMessage()
    msg["From"] = settings.ALERT_EMAIL_FROM
    msg["To"] = ", ".join(emails)
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True
        )
        logging.info("Email sent")
    except Exception as e:
        logging.error(f"Email failed: {e}")

# ====================== CORE PROCESSING ======================
async def process_camera(db: Session, camera: Camera):
    client = HikvisionThermalClient(camera.ip)
    data = await client.get_temperature_data()

    # Save reading
    reading = TemperatureReading(
        camera_id=camera.id,
        timestamp=data["timestamp"],
        max_temp=data["max_temp"],
        avg_temp=data["avg_temp"],
        min_temp=data["min_temp"],
        location=data["location"] or camera.location,
        raw_data=json.dumps(data["raw"])
    )
    db.add(reading)
    db.commit()
    db.refresh(reading)

    # Detect spike
    is_spike, context = detect_spike(db, camera.id, data["avg_temp"], reading.location)
    if not is_spike:
        return

    # Get recent history for Gemini
    recent = db.query(TemperatureReading).filter(
        TemperatureReading.camera_id == camera.id
    ).order_by(TemperatureReading.timestamp.desc()).limit(20).all()
    history = [r.avg_temp for r in reversed(recent)]

    # Gemini review
    review = await gemini_review(context, history)

    if not review.get("is_genuine_spike"):
        return

    # Create alert record
    evidence_url = f"https://yourdomain.com/alerts/{reading.id}"  # change later
    alert = Alert(
        camera_id=camera.id,
        location=reading.location,
        current_temp=context["current_temp"],
        baseline_mean=context["baseline_mean"],
        z_score=context["z_score"],
        risk_level=review["risk_level"],
        summary=review["summary"],
        detailed_analysis=review["detailed_analysis"],
        evidence_url=evidence_url
    )
    db.add(alert)
    db.commit()

    # Send alerts
    numbers = [n.strip() for n in camera.alert_numbers.split(",") if n.strip()]
    emails = [e.strip() for e in (camera.alert_emails or "").split(",") if e.strip()]

    sms_msg = f"""🔥 HEATGUARD ALERT – {review['risk_level'].upper()}

Location: {reading.location}
{review['summary']}

Action: {review['recommended_action']}
Evidence: {evidence_url}
"""
    await send_sms(numbers, sms_msg)

    email_body = f"""
HeatGuard AI – Early Fire Detection Alert

Risk Level: {review['risk_level']}
Location: {reading.location}
Current Temp: {context['current_temp']}°C
Baseline: {context['baseline_mean']}°C
Rise: +{context['temp_rise']}°C

Summary:
{review['summary']}

Detailed Analysis:
{review['detailed_analysis']}

Recommended Action:
{review['recommended_action']}

Evidence & Live View:
{evidence_url}
"""
    await send_email(emails, f"🔥 HeatGuard Alert – {reading.location}", email_body)

    # Delivery helpers currently log provider errors but do not return confirmed
    # provider receipts. Keep sent=False until delivery can be verified.
    alert.sent = False
    db.commit()
    logging.warning(
        "Alert %s created for camera %s; notification delivery remains unverified",
        alert.id,
        camera.name,
    )

# ====================== BACKGROUND WORKER ======================
async def monitoring_loop():
    logging.info("HeatGuard monitoring loop started")
    while True:
        db = SessionLocal()
        try:
            cameras = db.query(Camera).filter(Camera.is_active == True).all()
            for cam in cameras:
                try:
                    await process_camera(db, cam)
                except Exception as e:
                    logging.error(f"Error processing {cam.name}: {e}")
        finally:
            db.close()
        await asyncio.sleep(30)  # Poll every 30 seconds

# ====================== FASTAPI APP ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background worker
    task = asyncio.create_task(monitoring_loop())
    yield
    task.cancel()

app = FastAPI(
    title="HeatGuard AI",
    description="AI-powered thermal early fire detection system",
    version="1.0.0",
    lifespan=lifespan
)


# Permit the hosted installer portal to call the token-protected API.
# CORS controls browser origins; X-HeatGuard-Token remains the authentication layer.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://heatguard-ai-website.onrender.com",
        "https://heatguardai.co.za",
        "https://www.heatguardai.co.za",
    ],
    allow_credentials=False,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-HeatGuard-Token"],
)

@app.post("/cameras", status_code=201)
def add_camera(camera: CameraCreate, _auth: None = Depends(require_api_key)):
    db = SessionLocal()
    try:
        is_synthetic_test = camera.name == "HeatGuardAI pipeline validation (synthetic)"
        db_cam = Camera(
            name=camera.name,
            ip=camera.ip,
            location=camera.location,
            alert_numbers=camera.alert_numbers,
            alert_emails=camera.alert_emails,
            is_active=not is_synthetic_test,
        )
        db.add(db_cam)
        db.commit()
        db.refresh(db_cam)
        return {"id": db_cam.id, "message": "Camera added successfully"}
    finally:
        db.close()

@app.post("/api/v1/cameras/bulk-activate")
def bulk_activate_cameras(payload: CameraBatchCreate, _auth: None = Depends(require_heatguard_token)):
    """Atomically register up to 500 camera nodes; existing or repeated names are skipped."""
    db = SessionLocal()
    registered_nodes = []
    skipped_count = 0
    seen_names = set()
    try:
        requested_names = list(dict.fromkeys(cam.name for cam in payload.cameras))
        existing_names = {
            name for (name,) in db.query(Camera.name).filter(Camera.name.in_(requested_names)).all()
        }

        for cam_in in payload.cameras:
            if cam_in.name in existing_names or cam_in.name in seen_names:
                skipped_count += 1
                continue
            seen_names.add(cam_in.name)
            camera = Camera(
                name=cam_in.name,
                ip=cam_in.ip,
                location=cam_in.location,
                alert_numbers=cam_in.alert_numbers,
                alert_emails=cam_in.alert_emails,
                is_active=True,
            )
            db.add(camera)
            db.flush()
            registered_nodes.append({
                "id": camera.id,
                "name": camera.name,
                "location": camera.location,
            })

        db.commit()
        return {
            "status": "success",
            "activated_count": len(registered_nodes),
            "skipped_count": skipped_count,
            "nodes": registered_nodes,
        }
    except Exception:
        db.rollback()
        logging.exception("Bulk activation transaction failure")
        raise HTTPException(
            status_code=500,
            detail="Database transactional engine failure during batch onboarding.",
        )
    finally:
        db.close()

@app.post("/api/v1/telemetry/submit")
async def submit_external_telemetry(payload: TelemetrySubmission, _auth: None = Depends(require_heatguard_token)):
    """Accept JSON telemetry samples and dispatch alerts for detected spikes.

    API-key authenticated telemetry ingestion; the API key is required in production.
    """
    camera_id = payload.camera_id
    avg_temp = payload.avg_temp
    location = payload.location

    db = SessionLocal()
    try:
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if camera is None:
            raise HTTPException(status_code=404, detail="camera_id is not registered")

        reading = TemperatureReading(
            camera_id=camera_id,
            max_temp=payload.max_temp,
            avg_temp=payload.avg_temp,
            min_temp=payload.min_temp,
            location=location,
            raw_data='{"source": "external_edge_agent_node"}'
        )
        db.add(reading)
        db.commit()

        is_spike, context = detect_spike(db, camera_id, avg_temp, location)
        if not is_spike:
            return {
                "status": "logged",
                "current_z_score": context.get("z_score") if context else 0,
                "sample_count": context.get("sample_count", 0) if context else 0,
            }

        logging.warning("External telemetry anomaly: camera_id=%s context=%s", camera_id, context)

        historical_query = db.query(TemperatureReading.avg_temp).filter(
            TemperatureReading.camera_id == camera_id
        ).order_by(TemperatureReading.timestamp.desc()).limit(20).all()
        history_vector = [row[0] for row in reversed(historical_query)]

        analysis = await gemini_review(context, history_vector)
        if not analysis.get("is_genuine_spike"):
            return {"status": "anomaly_reviewed_not_genuine", "context": context, "analysis": analysis}

        alert_record = Alert(
            camera_id=camera_id,
            location=location,
            current_temp=avg_temp,
            baseline_mean=context["baseline_mean"],
            z_score=context["z_score"],
            risk_level=analysis.get("risk_level", "High"),
            summary=analysis.get("summary"),
            detailed_analysis=analysis.get("detailed_analysis"),
            evidence_url=f"https://heatguard.ai/alerts/{camera_id}",
            sent=False,
        )
        db.add(alert_record)
        db.commit()
        db.refresh(alert_record)

        alert_msg = (
            f"{analysis.get('summary') or 'Thermal anomaly detected.'} "
            f"Action: {analysis.get('recommended_action') or 'Inspect immediately'}. "
            f"Evidence: {alert_record.evidence_url}"
        )
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if camera:
            phones = [p.strip() for p in (camera.alert_numbers or "").split(",") if p.strip()]
            dispatches = [send_sms(phones, alert_msg)] if phones else []
            dispatches.extend(
                send_whatsapp_alert(
                    phone, location, avg_temp, context["temp_rise"],
                    analysis.get("risk_level"), analysis.get("recommended_action"), camera_id
                )
                for phone in phones
            )
            if dispatches:
                try:
                    # A successful gather only confirms coroutine completion, not provider delivery.
                    await asyncio.gather(*dispatches)
                except Exception:
                    alert_record.sent = False
                    db.commit()
                    logging.exception(
                        "Notification dispatch failed; alert %s remains unverified",
                        alert_record.id,
                    )
                else:
                    # Send helpers do not return provider delivery receipts; never claim sent.
                    alert_record.sent = False
                    db.commit()
                    logging.warning(
                        "Notification attempts completed without delivery confirmation; alert %s remains unverified",
                        alert_record.id,
                    )

        return {
            "status": "anomaly_detected",
            "context": context,
            "analysis": analysis,
            "alert_id": alert_record.id,
            "notifications_attempted": bool(camera and camera.alert_numbers),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logging.exception("External telemetry processing failed for camera_id=%s", camera_id)
        raise HTTPException(status_code=500, detail="Failed to process external telemetry")
    finally:
        db.close()

@app.get("/cameras")
def list_cameras(_auth: None = Depends(require_api_key)):
    db = SessionLocal()
    try:
        cams = db.query(Camera).all()
        return [{"id": c.id, "name": c.name, "ip": c.ip, "location": c.location, "active": c.is_active} for c in cams]
    finally:
        db.close()

@app.get("/readings")
def list_readings(camera_id: int, limit: int = 100, _auth: None = Depends(require_api_key)):
    """Return recent readings for a camera; bounded for validation and diagnostics."""
    db = SessionLocal()
    try:
        rows = db.query(TemperatureReading).filter(
            TemperatureReading.camera_id == camera_id
        ).order_by(TemperatureReading.timestamp.desc()).limit(max(1, min(limit, 500))).all()
        return [{
            "id": r.id,
            "camera_id": r.camera_id,
            "timestamp": r.timestamp,
            "avg_temp": r.avg_temp,
            "location": r.location,
            "raw_data": r.raw_data,
        } for r in rows]
    finally:
        db.close()

@app.get("/alerts")
def list_alerts(limit: int = 20, _auth: None = Depends(require_api_key)):
    db = SessionLocal()
    try:
        alerts = db.query(Alert).order_by(Alert.timestamp.desc()).limit(limit).all()
        return [
            {
                "id": a.id,
                "camera_id": a.camera_id,
                "location": a.location,
                "temp": a.current_temp,
                "risk": a.risk_level,
                "summary": a.summary,
                "time": a.timestamp
            } for a in alerts
        ]
    finally:
        db.close()

@app.get("/")
def root_status():
    """Simple root status endpoint for platform checks; not a substitute for live camera telemetry."""
    return {"service": "HeatGuard AI", "status": "running", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "HeatGuard AI is running", "time": datetime.utcnow().isoformat()}

# ====================== RUN ======================
if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO)
    print("🔥 HeatGuard AI starting...")
    print("Add cameras via POST /cameras")
    print("Monitoring starts automatically")
    uvicorn.run(app, host="0.0.0.0", port=8000)