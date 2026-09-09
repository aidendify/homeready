"""HomeReady: night-before trade home-prep checklists."""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import helpers as H

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", "homeready-self-hosted-change-me")

app.teardown_appcontext(H.close_db)

_scheduler_lock = threading.Lock()
_scheduler_started = False


def init_db() -> None:
    with app.app_context():
        H.init_schema(H.get_db())


@app.context_processor
def inject_globals() -> dict:
    return {
        "marketing_url": H._env("MARKETING_URL"),
        "smtp_configured": H.smtp_configured(),
        "sms_configured": H.sms_configured(),
        "business_name": H.business_name(),
        "business_phone": H.business_phone(),
        "owner_locked": bool(H.owner_password()),
        "logged_in": bool(session.get("owner")) or not H.owner_password(),
        "status_label": H.status_label,
        "event_label": H.event_label,
        "format_date_display": H.format_date_display,
        "template_label": H.template_label,
        "first_name": H.first_name,
        "STATUS_LABELS": H.STATUS_LABELS,
    }


@app.before_request
def protect_owner_routes():
    if request.endpoint in H.OPEN_ENDPOINTS or request.endpoint is None:
        return None
    if not H.owner_password():
        return None
    if session.get("owner"):
        return None
    nxt = request.path if request.method == "GET" else "/"
    return redirect(url_for("login", next=nxt))


def _safe_next(val: str | None) -> str:
    raw = (val or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return url_for("index")


def _parse_date(raw: str | None, field: str, errors: list[str]) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        errors.append(f"{field} must be YYYY-MM-DD.")
        return None


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "smtp_configured": H.smtp_configured(),
            "sms_configured": H.sms_configured(),
        }
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    if not H.owner_password():
        return redirect(nxt)
    if session.get("owner"):
        return redirect(nxt)
    error = None
    if request.method == "POST":
        provided = (request.form.get("password") or "").encode("utf-8")
        expected = H.owner_password().encode("utf-8")
        ok = len(provided) == len(expected) and secrets.compare_digest(provided, expected)
        if ok:
            session["owner"] = True
            return redirect(nxt)
        error = "Incorrect password."
    return render_template("login.html", next=nxt, error=error, public=True)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    # Run silence holds on board view so Verifier sees them without waiting for tick.
    try:
        H.process_silence_auto_holds(app)
    except Exception:
        app.logger.exception("auto-hold on board failed")

    today = H.board_today()
    db = H.get_db()
    today_jobs = db.execute(
        """
        SELECT * FROM jobs
        WHERE scheduled_date = ?
        ORDER BY id ASC
        """,
        (today,),
    ).fetchall()
    hold_queue = db.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'held'
        ORDER BY scheduled_date ASC, id ASC
        """
    ).fetchall()
    upcoming = db.execute(
        """
        SELECT * FROM jobs
        WHERE scheduled_date > ? AND status NOT IN ('cancelled')
        ORDER BY id ASC
        LIMIT 30
        """,
        (today,),
    ).fetchall()

    drafts = {}
    for job in hold_queue:
        drafts[job["id"]] = H.reschedule_draft(job)

    return render_template(
        "index.html",
        today=today,
        today_jobs=today_jobs,
        hold_queue=hold_queue,
        upcoming=upcoming,
        drafts=drafts,
        crew_ready_blurb=H.crew_ready_blurb,
        public_url=H.public_checklist_url,
    )


@app.route("/jobs/new", methods=["GET", "POST"])
def new_job():
    templates = H.load_templates()
    if request.method == "GET":
        return render_template(
            "new_job.html",
            templates=templates,
            default_date=H.board_today(),
            form=None,
            errors=None,
        )

    form = {
        "template_key": (request.form.get("template_key") or "").strip(),
        "customer_name": (request.form.get("customer_name") or "").strip(),
        "customer_email": (request.form.get("customer_email") or "").strip(),
        "customer_phone": (request.form.get("customer_phone") or "").strip(),
        "job_ref": (request.form.get("job_ref") or "").strip(),
        "site_label": (request.form.get("site_label") or "").strip(),
        "scheduled_date": (request.form.get("scheduled_date") or "").strip(),
        "start_window": (request.form.get("start_window") or "").strip(),
        "crew_emails": (request.form.get("crew_emails") or "").strip(),
        "crew_phones": (request.form.get("crew_phones") or "").strip(),
        "extra_note": (request.form.get("extra_note") or "").strip(),
        "notes": (request.form.get("notes") or "").strip(),
    }
    errors: list[str] = []
    if not form["template_key"] or form["template_key"] not in templates:
        errors.append("Choose a trade template.")
    if not form["customer_name"]:
        errors.append("Customer name is required.")
    scheduled_date = _parse_date(form["scheduled_date"], "Scheduled date", errors)
    if not scheduled_date and not form["scheduled_date"]:
        errors.append("Scheduled date is required.")

    if errors:
        return render_template(
            "new_job.html",
            templates=templates,
            default_date=H.board_today(),
            form=form,
            errors=errors,
        ), 400

    job_id = H.create_job(
        H.get_db(),
        template_key=form["template_key"],
        customer_name=form["customer_name"],
        customer_email=form["customer_email"] or None,
        customer_phone=form["customer_phone"] or None,
        job_ref=form["job_ref"] or None,
        site_label=form["site_label"] or None,
        scheduled_date=scheduled_date,
        start_window=form["start_window"] or None,
        crew_emails=form["crew_emails"] or None,
        crew_phones=form["crew_phones"] or None,
        extra_note=form["extra_note"] or None,
        notes=form["notes"] or None,
    )
    flash("Job created.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/jobs/<int:job_id>")
def job_detail(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    items = H.list_job_items(job_id)
    events = H.list_events(job_id)
    subject, body = H.reschedule_draft(job)
    return render_template(
        "job_detail.html",
        job=job,
        items=items,
        events=events,
        public_url=H.public_checklist_url(job["token"]),
        copy_blurb=H.checklist_copy_blurb(job),
        crew_blurb=H.crew_ready_blurb(job),
        reschedule_subject=subject,
        reschedule_body=body,
    )


@app.post("/jobs/<int:job_id>/send-now")
def send_now(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    db = H.get_db()
    if job["status"] != "scheduled":
        flash("Checklist already sent or job is not in scheduled status.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    if H.claim_checklist_send(db, job_id):
        job = H.get_job(job_id)
        H.send_checklist_notifications(app, job)
        flash("Checklist marked sent. Link is ready to share.", "ok")
    else:
        flash("Could not send (already claimed).", "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/hold")
def hold_job(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    reason = (request.form.get("reason") or "").strip() or "Manual hold"
    if H.mark_held_manual(H.get_db(), job_id, reason):
        flash("Job held.", "ok")
    else:
        flash("Could not hold this job.", "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/cancel")
def cancel_job(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    if H.mark_cancelled(H.get_db(), job_id):
        flash("Job cancelled.", "ok")
    else:
        flash("Could not cancel this job.", "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/mark-ready")
def override_ready(job_id: int):
    job = H.get_job(job_id)
    if job is None:
        abort(404)
    items = H.list_job_items(job_id)
    keys = [i["item_key"] for i in items]
    ok = H.mark_ready(
        H.get_db(),
        job_id,
        checked_keys=keys,
        ip=request.remote_addr,
        ua=request.headers.get("User-Agent"),
        override=True,
    )
    if ok:
        job = H.get_job(job_id)
        H.notify_crew_ready(app, job)
        flash("Marked ready (override).", "ok")
    else:
        flash("Could not mark ready.", "error")
    return redirect(url_for("job_detail", job_id=job_id))


@app.get("/h/<token>")
def public_checklist(token: str):
    job = H.get_job_by_token(token)
    if job is None:
        abort(404)
    items = H.list_job_items(job["id"])
    error = request.args.get("error")
    return render_template(
        "public_checklist.html",
        job=job,
        items=items,
        error=error,
        public=True,
    )


@app.post("/h/<token>/ready")
def public_ready(token: str):
    job = H.get_job_by_token(token)
    if job is None:
        abort(404)
    if job["status"] in ("ready", "held", "cancelled", "not_ready"):
        return redirect(url_for("public_checklist", token=token))

    items = H.list_job_items(job["id"])
    checked = set(request.form.getlist("item"))
    missing = [
        i for i in items if i["required"] and i["item_key"] not in checked
    ]
    if missing:
        return render_template(
            "public_checklist.html",
            job=job,
            items=items,
            error="Please check every required item before tapping I'm Ready.",
            checked=checked,
            public=True,
        ), 400

    ok = H.mark_ready(
        H.get_db(),
        job["id"],
        checked_keys=list(checked),
        ip=request.remote_addr,
        ua=request.headers.get("User-Agent"),
        override=False,
    )
    if ok:
        job = H.get_job(job["id"])
        H.notify_crew_ready(app, job)
    return redirect(url_for("public_checklist", token=token))


@app.post("/h/<token>/not-ready")
def public_not_ready(token: str):
    job = H.get_job_by_token(token)
    if job is None:
        abort(404)
    if job["status"] in ("ready", "held", "cancelled", "not_ready"):
        return redirect(url_for("public_checklist", token=token))

    reason = (request.form.get("reason") or "").strip()
    items = H.list_job_items(job["id"])
    if not reason:
        return render_template(
            "public_checklist.html",
            job=job,
            items=items,
            error="Please tell us why you're not ready.",
            checked=set(request.form.getlist("item")),
            public=True,
        ), 400

    ok = H.mark_not_ready(
        H.get_db(),
        job["id"],
        reason,
        ip=request.remote_addr,
        ua=request.headers.get("User-Agent"),
    )
    if ok:
        job = H.get_job(job["id"])
        H.notify_owner_hold(app, job, reason)
    return redirect(url_for("public_checklist", token=token))


@app.errorhandler(404)
def not_found(_err):
    return render_template("404.html", public=True), 404


def _scheduler_loop() -> None:
    while True:
        try:
            with app.app_context():
                H.process_tick(app)
        except Exception:
            app.logger.exception("scheduler tick failed")
        time.sleep(45)


def start_scheduler() -> None:
    global _scheduler_started
    flag = H._env("HOMEREADY_DISABLE_SCHEDULER").lower()
    if flag in {"1", "true", "yes", "on"}:
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    thread = threading.Thread(target=_scheduler_loop, name="homeready-scheduler", daemon=True)
    thread.start()


init_db()
start_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT") or "8080"), debug=False)
