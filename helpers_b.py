from __future__ import annotations

from datetime import date, datetime

from helpers_a import (  # noqa: F401
    _env,
    add_event,
    app_tz,
    business_name,
    compute_send_at,
    first_name,
    format_date_display,
    get_db,
    get_template,
    hold_cutoff_hour,
    local_now,
    parse_csv_list,
    public_checklist_url,
    sms_configured,
    smtp_configured,
    template_label,
    utc_now_iso,
)
from helpers_notify import send_smtp, send_twilio_sms

def create_job(
    conn,
    *,
    template_key: str,
    customer_name: str,
    customer_email: str | None,
    customer_phone: str | None,
    job_ref: str | None,
    site_label: str | None,
    scheduled_date: str,
    start_window: str | None,
    crew_emails: str | None,
    crew_phones: str | None,
    extra_note: str | None,
    notes: str | None,
    token: str | None = None,
) -> int:
    tpl = get_template(template_key)
    if not tpl:
        raise ValueError("Unknown template")
    now = utc_now_iso()
    tok = token or __import__("secrets").token_hex(32)
    send_at = compute_send_at(scheduled_date)
    cur = conn.execute(
        """
        INSERT INTO jobs (
            token, template_key, customer_name, customer_email, customer_phone,
            job_ref, site_label, scheduled_date, start_window, crew_emails,
            crew_phones, extra_note, notes, status, send_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)
        """,
        (
            tok,
            template_key,
            customer_name,
            customer_email,
            customer_phone,
            job_ref,
            site_label,
            scheduled_date,
            start_window,
            crew_emails,
            crew_phones,
            extra_note,
            notes,
            send_at,
            now,
            now,
        ),
    )
    job_id = int(cur.lastrowid)
    for idx, item in enumerate(tpl.get("items") or []):
        conn.execute(
            """
            INSERT INTO job_items (job_id, item_key, label, required, checked, sort_order)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (
                job_id,
                item.get("key") or f"item_{idx}",
                item.get("label") or item.get("key") or f"Item {idx + 1}",
                1 if item.get("required", True) else 0,
                idx,
            ),
        )
    add_event(conn, job_id, "created", meta={"template_key": template_key})
    conn.commit()
    return job_id


def claim_checklist_send(conn, job_id: int) -> bool:
    """Idempotent claim: scheduled → checklist_sent when due (or forced)."""
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE jobs
        SET status = 'checklist_sent',
            checklist_sent_at = ?,
            updated_at = ?
        WHERE id = ? AND status = 'scheduled'
        """,
        (now, now, job_id),
    )
    conn.commit()
    if cur.rowcount != 1:
        return False
    add_event(conn, job_id, "checklist_sent")
    conn.commit()
    return True


def send_checklist_notifications(app, job) -> None:
    """Optional email/SMS with link only. Never raises to caller."""
    link = public_checklist_url(job["token"])
    biz = business_name()
    date_disp = format_date_display(job["scheduled_date"])
    subject = f"Prep checklist for your visit — {biz}"
    email_body = (
        f"Hi {first_name(job['customer_name']) or 'there'},\n\n"
        f"{biz} sent a short home-prep checklist for your visit on {date_disp}.\n"
        f"Please open the link, check the items, and tap I'm Ready or Not Ready:\n\n"
        f"{link}\n\n"
        f"- {biz}"
    )
    sms_body = f"{biz}: please confirm home prep for {date_disp}: {link}"

    if smtp_configured() and job["customer_email"]:
        try:
            send_smtp(job["customer_email"], subject, email_body)
        except Exception as exc:  # noqa: BLE001
            app.logger.warning(
                "Checklist email failed for job_id=%s: %s", job["id"], type(exc).__name__
            )

    if sms_configured() and job["customer_phone"]:
        try:
            send_twilio_sms(job["customer_phone"], sms_body)
        except Exception as exc:  # noqa: BLE001
            app.logger.warning(
                "Checklist SMS failed for job_id=%s: %s", job["id"], type(exc).__name__
            )


def notify_crew_ready(app, job) -> None:
    link = public_checklist_url(job["token"])
    biz = business_name()
    date_disp = format_date_display(job["scheduled_date"])
    subject = f"Ready: {job['customer_name']} — {date_disp}"
    body = (
        f"{biz}: {job['customer_name']} marked Ready for {date_disp}.\n"
        f"Trade: {template_label(job['template_key'])}\n"
        f"Link: {link}\n"
    )
    sms = f"{biz}: Ready — {job['customer_name']} ({date_disp})."

    if smtp_configured():
        for email in parse_csv_list(job["crew_emails"]):
            try:
                send_smtp(email, subject, body)
            except Exception as exc:  # noqa: BLE001
                app.logger.warning(
                    "Crew email failed for job_id=%s: %s", job["id"], type(exc).__name__
                )
        owner = _env("OWNER_NOTIFY_EMAIL")
        if owner:
            try:
                send_smtp(owner, subject, body)
            except Exception as exc:  # noqa: BLE001
                app.logger.warning(
                    "Owner email failed for job_id=%s: %s", job["id"], type(exc).__name__
                )

    if sms_configured():
        for phone in parse_csv_list(job["crew_phones"]):
            try:
                send_twilio_sms(phone, sms)
            except Exception as exc:  # noqa: BLE001
                app.logger.warning(
                    "Crew SMS failed for job_id=%s: %s", job["id"], type(exc).__name__
                )


def notify_owner_hold(app, job, reason: str) -> None:
    owner = _env("OWNER_NOTIFY_EMAIL")
    if not (smtp_configured() and owner):
        return
    subject = f"Held: {job['customer_name']} — {format_date_display(job['scheduled_date'])}"
    body = (
        f"{business_name()}: job held.\n"
        f"Customer: {job['customer_name']}\n"
        f"Reason: {reason}\n"
        f"Link: {public_checklist_url(job['token'])}\n"
    )
    try:
        send_smtp(owner, subject, body)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning(
            "Owner hold email failed for job_id=%s: %s", job["id"], type(exc).__name__
        )


def checklist_copy_blurb(job) -> str:
    link = public_checklist_url(job["token"])
    date_disp = format_date_display(job["scheduled_date"])
    return (
        f"{business_name()}: please confirm home prep for your visit on {date_disp}: {link}"
    )


def crew_ready_blurb(job) -> str:
    date_disp = format_date_display(job["scheduled_date"])
    return (
        f"{business_name()}: Ready — {job['customer_name']} "
        f"({template_label(job['template_key'])}, {date_disp})."
    )


def reschedule_draft(job) -> tuple[str, str]:
    """Return (subject, body) copy for owner — no calendar API."""
    biz = business_name()
    name = first_name(job["customer_name"]) or job["customer_name"]
    reason = job["hold_reason"] or "the home was not ready"
    subject = f"Reschedule — {job['customer_name']} / {format_date_display(job['scheduled_date'])}"
    body = (
        f"Hi {name},\n\n"
        f"Thanks for letting us know. We held your visit because {reason}.\n\n"
        f"Please reply with a few dates that work and we'll get you back on the schedule.\n\n"
        f"- {biz}"
    )
    return subject, body


def mark_ready(conn, job_id: int, *, checked_keys: list[str], ip: str | None, ua: str | None, override: bool = False) -> bool:
    now = utc_now_iso()
    if override:
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = 'ready', responded_at = ?, updated_at = ?,
                response_ip = ?, response_ua = ?, hold_reason = NULL
            WHERE id = ? AND status NOT IN ('cancelled', 'ready')
            """,
            (now, now, ip, ua, job_id),
        )
    else:
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = 'ready', responded_at = ?, updated_at = ?,
                response_ip = ?, response_ua = ?, hold_reason = NULL
            WHERE id = ? AND status IN ('scheduled', 'checklist_sent')
            """,
            (now, now, ip, ua, job_id),
        )
    if cur.rowcount != 1:
        conn.commit()
        return False
    if checked_keys:
        for key in checked_keys:
            conn.execute(
                "UPDATE job_items SET checked = 1 WHERE job_id = ? AND item_key = ?",
                (job_id, key),
            )
    kind = "override_ready" if override else "ready"
    add_event(
        conn,
        job_id,
        kind,
        meta={"checked": checked_keys, "ip": ip, "ua": (ua or "")[:200]},
    )
    conn.commit()
    return True


def mark_not_ready(conn, job_id: int, reason: str, *, ip: str | None, ua: str | None) -> bool:
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE jobs
        SET status = 'held', hold_reason = ?, responded_at = ?, updated_at = ?,
            response_ip = ?, response_ua = ?
        WHERE id = ? AND status IN ('scheduled', 'checklist_sent')
        """,
        (reason, now, now, ip, ua, job_id),
    )
    if cur.rowcount != 1:
        conn.commit()
        return False
    add_event(conn, job_id, "not_ready", meta={"reason": reason})
    add_event(conn, job_id, "held", meta={"reason": reason, "from": "not_ready"})
    conn.commit()
    return True


def mark_held_manual(conn, job_id: int, reason: str) -> bool:
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE jobs
        SET status = 'held', hold_reason = ?, updated_at = ?
        WHERE id = ? AND status NOT IN ('cancelled', 'held')
        """,
        (reason or "Manual hold", now, job_id),
    )
    if cur.rowcount != 1:
        conn.commit()
        return False
    add_event(conn, job_id, "held", meta={"reason": reason or "Manual hold", "from": "manual"})
    conn.commit()
    return True


def mark_cancelled(conn, job_id: int) -> bool:
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE jobs
        SET status = 'cancelled', updated_at = ?
        WHERE id = ? AND status != 'cancelled'
        """,
        (now, job_id),
    )
    if cur.rowcount != 1:
        conn.commit()
        return False
    add_event(conn, job_id, "cancelled")
    conn.commit()
    return True


def process_due_checklist_sends(app) -> None:
    conn = get_db()
    now = utc_now_iso()
    rows = conn.execute(
        """
        SELECT id FROM jobs
        WHERE status = 'scheduled' AND send_at IS NOT NULL AND send_at <= ?
        ORDER BY id ASC
        """,
        (now,),
    ).fetchall()
    for row in rows:
        if claim_checklist_send(conn, row["id"]):
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            if job is not None:
                send_checklist_notifications(app, job)


def process_silence_auto_holds(app) -> None:
    """Jobs checklist_sent/scheduled past HOLD_CUTOFF_HOUR on job date without Ready → held."""
    conn = get_db()
    now_local = local_now()
    cutoff_hour = hold_cutoff_hour()
    today = now_local.date().isoformat()
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE status IN ('scheduled', 'checklist_sent')
          AND scheduled_date <= ?
        ORDER BY id ASC
        """,
        (today,),
    ).fetchall()
    for job in rows:
        job_date = date.fromisoformat(job["scheduled_date"][:10])
        cutoff_local = datetime(
            job_date.year, job_date.month, job_date.day, cutoff_hour, 0, 0, tzinfo=app_tz()
        )
        if now_local < cutoff_local:
            continue
        now = utc_now_iso()
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = 'held',
                hold_reason = COALESCE(hold_reason, 'No Ready response by morning cutoff'),
                updated_at = ?
            WHERE id = ? AND status IN ('scheduled', 'checklist_sent')
            """,
            (now, job["id"]),
        )
        if cur.rowcount != 1:
            conn.commit()
            continue
        add_event(
            conn,
            job["id"],
            "auto_held_silence",
            meta={"cutoff_hour": cutoff_hour, "scheduled_date": job["scheduled_date"]},
        )
        conn.commit()
        refreshed = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        if refreshed is not None:
            notify_owner_hold(app, refreshed, refreshed["hold_reason"] or "silence")


def process_tick(app) -> None:
    process_due_checklist_sends(app)
    process_silence_auto_holds(app)
