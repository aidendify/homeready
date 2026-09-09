"""HomeReady helpers: env, db, templates, mail, SMS, blurbs, tick claims."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import g

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = APP_ROOT / "homeready.db"
CHECKLISTS_DIR = APP_ROOT / "checklists"

OPEN_ENDPOINTS = {
    "health",
    "login",
    "logout",
    "public_checklist",
    "public_ready",
    "public_not_ready",
    "static",
}

STATUSES = (
    "scheduled",
    "checklist_sent",
    "ready",
    "not_ready",
    "held",
    "cancelled",
)

STATUS_LABELS = {
    "scheduled": "Scheduled",
    "checklist_sent": "Checklist sent",
    "ready": "Ready",
    "not_ready": "Not ready",
    "held": "Held",
    "cancelled": "Cancelled",
}

EVENT_LABELS = {
    "created": "Created",
    "checklist_sent": "Checklist sent",
    "ready": "Ready",
    "not_ready": "Not ready",
    "held": "Held",
    "auto_held_silence": "Auto-held (no response)",
    "cancelled": "Cancelled",
    "override_ready": "Marked ready (override)",
}

_template_cache: dict[str, dict] | None = None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def database_path() -> str:
    raw = _env("DATABASE_PATH")
    return raw if raw else str(DEFAULT_DB)


def smtp_configured() -> bool:
    return bool(_env("SMTP_HOST"))


def sms_configured() -> bool:
    return bool(
        _env("TWILIO_ACCOUNT_SID")
        and _env("TWILIO_AUTH_TOKEN")
        and _env("TWILIO_FROM_NUMBER")
    )


def owner_password() -> str:
    return os.environ.get("OWNER_PASSWORD", "").strip()


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL").rstrip("/")


def business_name() -> str:
    return _env("BUSINESS_NAME") or "HomeReady"


def business_phone() -> str:
    return _env("BUSINESS_PHONE")


def app_tz() -> ZoneInfo:
    name = _env("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def checklist_send_hour() -> int:
    raw = _env("CHECKLIST_SEND_HOUR") or "18"
    try:
        hour = int(raw)
        return max(0, min(23, hour))
    except ValueError:
        return 18


def hold_cutoff_hour() -> int:
    raw = _env("HOLD_CUTOFF_HOUR") or "6"
    try:
        hour = int(raw)
        return max(0, min(23, hour))
    except ValueError:
        return 6


def board_today() -> str:
    override = _env("HOMEREADY_TODAY")
    if override:
        return override
    return datetime.now(app_tz()).date().isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    return to_iso(utc_now())


def local_now() -> datetime:
    return datetime.now(app_tz()).replace(microsecond=0)


def public_checklist_url(token: str) -> str:
    base = public_base_url() or "http://localhost:8080"
    return f"{base}/h/{token}"


def status_label(key: str) -> str:
    return STATUS_LABELS.get(key, key)


def event_label(kind: str) -> str:
    return EVENT_LABELS.get(kind, kind)


def first_name(full: str) -> str:
    parts = (full or "").strip().split()
    return parts[0] if parts else ""


def format_date_display(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        return f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    except ValueError:
        return raw


def load_templates() -> dict[str, dict]:
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    templates: dict[str, dict] = {}
    if CHECKLISTS_DIR.is_dir():
        for path in sorted(CHECKLISTS_DIR.glob("*.json")):
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            key = data.get("key") or path.stem
            templates[key] = data
    _template_cache = templates
    return templates


def clear_template_cache() -> None:
    global _template_cache
    _template_cache = None


def template_keys() -> list[str]:
    return list(load_templates().keys())


def get_template(key: str) -> dict | None:
    return load_templates().get(key)


def template_label(key: str) -> str:
    tpl = get_template(key)
    if tpl:
        return tpl.get("label") or key
    return key


def compute_send_at(scheduled_date: str) -> str:
    """Evening before scheduled_date at CHECKLIST_SEND_HOUR in TZ, as UTC ISO."""
    d = date.fromisoformat(scheduled_date[:10])
    evening_before = d - timedelta(days=1)
    hour = checklist_send_hour()
    local_dt = datetime(
        evening_before.year,
        evening_before.month,
        evening_before.day,
        hour,
        0,
        0,
        tzinfo=app_tz(),
    )
    return to_iso(local_dt)


def connect_db() -> sqlite3.Connection:
    path = database_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = connect_db()
        g._db = db
    return db


def close_db(_exc: BaseException | None = None) -> None:
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            template_key TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            customer_email TEXT,
            customer_phone TEXT,
            job_ref TEXT,
            site_label TEXT,
            scheduled_date TEXT NOT NULL,
            start_window TEXT,
            crew_emails TEXT,
            crew_phones TEXT,
            extra_note TEXT,
            notes TEXT,
            status TEXT NOT NULL,
            send_at TEXT,
            checklist_sent_at TEXT,
            responded_at TEXT,
            hold_reason TEXT,
            response_ip TEXT,
            response_ua TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS job_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            item_key TEXT NOT NULL,
            label TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            checked INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            kind TEXT NOT NULL,
            at TEXT NOT NULL,
            meta_json TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_token ON jobs(token);
        CREATE INDEX IF NOT EXISTS idx_jobs_scheduled_date ON jobs(scheduled_date);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_job_items_job_id ON job_items(job_id);
        CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id);
        """
    )
    db.commit()


def get_job(job_id: int):
    return get_db().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_by_token(token: str):
    return get_db().execute("SELECT * FROM jobs WHERE token = ?", (token,)).fetchone()


def list_job_items(job_id: int):
    return get_db().execute(
        "SELECT * FROM job_items WHERE job_id = ? ORDER BY sort_order ASC, id ASC",
        (job_id,),
    ).fetchall()


def list_events(job_id: int):
    return get_db().execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY id ASC",
        (job_id,),
    ).fetchall()


def add_event(conn, job_id: int, kind: str, meta=None, at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO events (job_id, kind, at, meta_json) VALUES (?, ?, ?, ?)",
        (
            job_id,
            kind,
            at or utc_now_iso(),
            json.dumps(meta) if meta is not None else None,
        ),
    )
    return int(cur.lastrowid)


def parse_csv_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
