"""HomeReady Verifier-style tests (PRD §12)."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

# Configure env before importing app
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_PATH"] = _TMP.name
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8080"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["TZ"] = "UTC"
os.environ["CHECKLIST_SEND_HOUR"] = "18"
os.environ["HOLD_CUTOFF_HOUR"] = "6"
os.environ["MARKETING_URL"] = ""
os.environ["SECRET_KEY"] = "test-secret"
os.environ["HOMEREADY_DISABLE_SCHEDULER"] = "1"
for k in (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "OWNER_NOTIFY_EMAIL",
):
    os.environ.pop(k, None)

import helpers as H  # noqa: E402
from app import app  # noqa: E402


class HomeReadyTests(unittest.TestCase):
    def setUp(self) -> None:
        H.clear_template_cache()
        path = os.environ["DATABASE_PATH"]
        if Path(path).exists():
            Path(path).unlink()
        self.client = app.test_client()
        with app.app_context():
            H.init_schema(H.get_db())
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.today = date.today().isoformat()

    def tearDown(self) -> None:
        path = os.environ["DATABASE_PATH"]
        if Path(path).exists():
            Path(path).unlink()

    def _login(self):
        return self.client.post(
            "/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )

    def _create_hvac(self, name="Jordan Smith", scheduled=None):
        self._login()
        return self.client.post(
            "/jobs/new",
            data={
                "template_key": "hvac_install",
                "customer_name": name,
                "customer_email": "",
                "customer_phone": "",
                "job_ref": "JOB-1",
                "site_label": "123 Main",
                "scheduled_date": scheduled or self.tomorrow,
                "start_window": "8am–12pm",
                "crew_emails": "",
                "crew_phones": "",
                "extra_note": "Please clear the closet.",
                "notes": "internal only",
            },
            follow_redirects=False,
        )

    def test_01_health(self):
        rv = self.client.get("/health")
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertIs(data["smtp_configured"], False)
        self.assertIs(data["sms_configured"], False)

    def test_02_auth_and_public(self):
        rv = self.client.get("/")
        self.assertEqual(rv.status_code, 302)
        self.assertIn("/login", rv.headers.get("Location", ""))

        rv = self.client.get("/health")
        self.assertEqual(rv.status_code, 200)

        # unknown token public 404
        rv = self.client.get("/h/" + ("a" * 64))
        self.assertEqual(rv.status_code, 404)

        # create then public without login
        self._create_hvac()
        with app.app_context():
            job = H.get_db().execute("SELECT token FROM jobs LIMIT 1").fetchone()
        token = job["token"]
        self.client.get("/logout")
        rv = self.client.get(f"/h/{token}")
        self.assertEqual(rv.status_code, 200)
        self.assertIn(b"Harbor HVAC", rv.data)

    def test_03_create_snapshots_items_and_link(self):
        rv = self._create_hvac()
        self.assertEqual(rv.status_code, 302)
        with app.app_context():
            job = H.get_db().execute("SELECT * FROM jobs").fetchone()
            items = H.list_job_items(job["id"])
        self.assertEqual(job["status"], "scheduled")
        self.assertEqual(job["template_key"], "hvac_install")
        self.assertGreaterEqual(len(items), 4)
        labels = " ".join(i["label"].lower() for i in items)
        self.assertTrue(
            any(w in labels for w in ("closet", "outdoor", "pets", "adult", "parking"))
        )
        detail = self.client.get(f"/jobs/{job['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn(f"/h/{job['token']}".encode(), detail.data)
        self.assertIn(b"Clear path", detail.data)

    def test_04_send_now_and_public_items(self):
        self._create_hvac()
        with app.app_context():
            job = H.get_db().execute("SELECT * FROM jobs").fetchone()
        rv = self.client.post(f"/jobs/{job['id']}/send-now", follow_redirects=False)
        self.assertEqual(rv.status_code, 302)
        with app.app_context():
            job = H.get_job(job["id"])
            events = H.list_events(job["id"])
        self.assertEqual(job["status"], "checklist_sent")
        self.assertTrue(any(e["kind"] == "checklist_sent" for e in events))

        pub = self.client.get(f"/h/{job['token']}")
        self.assertEqual(pub.status_code, 200)
        body = pub.data.lower()
        self.assertTrue(b"closet" in body or b"outdoor" in body or b"pets" in body)
        self.assertIn(b"i'm ready", body)
        self.assertIn(b"not ready", body)

    def test_05_ready_blocked_when_unchecked(self):
        self._create_hvac()
        with app.app_context():
            job = H.get_db().execute("SELECT * FROM jobs").fetchone()
        self.client.post(f"/jobs/{job['id']}/send-now")
        rv = self.client.post(f"/h/{job['token']}/ready", data={}, follow_redirects=False)
        self.assertEqual(rv.status_code, 400)
        self.assertIn(b"required", rv.data.lower())
        with app.app_context():
            job = H.get_job(job["id"])
        self.assertEqual(job["status"], "checklist_sent")

    def test_06_ready_ok_without_smtp(self):
        self._create_hvac()
        with app.app_context():
            job = H.get_db().execute("SELECT * FROM jobs").fetchone()
            items = H.list_job_items(job["id"])
        self.client.post(f"/jobs/{job['id']}/send-now")
        data = {"item": [i["item_key"] for i in items]}
        rv = self.client.post(f"/h/{job['token']}/ready", data=data, follow_redirects=True)
        self.assertEqual(rv.status_code, 200)
        self.assertIn(b"Ready", rv.data)
        with app.app_context():
            job = H.get_job(job["id"])
            events = H.list_events(job["id"])
        self.assertEqual(job["status"], "ready")
        self.assertTrue(any(e["kind"] == "ready" for e in events))

    def test_07_not_ready_hold_and_reschedule_draft(self):
        self._create_hvac(name="Alex Lee")
        with app.app_context():
            job = H.get_db().execute("SELECT * FROM jobs").fetchone()
        self.client.post(f"/jobs/{job['id']}/send-now")
        rv = self.client.post(
            f"/h/{job['token']}/not-ready",
            data={"reason": "pets not secured"},
            follow_redirects=True,
        )
        self.assertEqual(rv.status_code, 200)
        with app.app_context():
            job = H.get_job(job["id"])
        self.assertEqual(job["status"], "held")
        self.assertIn("pets not secured", job["hold_reason"])

        self._login()
        board = self.client.get("/")
        self.assertEqual(board.status_code, 200)
        self.assertIn(b"pets not secured", board.data)
        self.assertIn(b"Reschedule draft", board.data)
        self.assertIn(b"Alex Lee", board.data)

        detail = self.client.get(f"/jobs/{job['id']}")
        self.assertIn(b"Reschedule draft", detail.data)
        self.assertIn(b"Copy reschedule draft", detail.data)

    def test_08_no_gate_code_fields(self):
        self._login()
        page = self.client.get("/jobs/new")
        low = page.data.lower()
        self.assertNotIn(b"gate code", low)
        self.assertNotIn(b"gate-code", low)
        self.assertNotIn(b"lockbox", low)
        self.assertNotIn(b"access code", low)

        self._create_hvac()
        with app.app_context():
            job = H.get_db().execute("SELECT * FROM jobs").fetchone()
        pub = self.client.get(f"/h/{job['token']}")
        low = pub.data.lower()
        self.assertNotIn(b"gate code", low)
        self.assertNotIn(b"lockbox", low)

    def test_09_out_of_scope_absent(self):
        self._login()
        pages = [
            self.client.get("/").data.lower(),
            self.client.get("/jobs/new").data.lower(),
        ]
        blob = b" ".join(pages)
        for needle in (
            b"whatsapp",
            b"google calendar",
            b"calendly",
            b"weather hold",
            b"csat",
            b"drip",
            b"change order",
            b"parts eta",
            b"webhook",
        ):
            self.assertNotIn(needle, blob)

        with app.app_context():
            tables = {
                r[0]
                for r in H.get_db()
                .execute("SELECT name FROM sqlite_master WHERE type='table'")
                .fetchall()
            }
        tables.discard("sqlite_sequence")
        self.assertEqual(tables, {"jobs", "job_items", "events"})
        self.assertNotIn("sequences", tables)
        self.assertNotIn("drips", tables)
        self.assertNotIn("gate_codes", tables)

    def test_10_empty_marketing_no_footer(self):
        self._login()
        rv = self.client.get("/")
        self.assertNotIn(b"Powered by HomeReady", rv.data)

    def test_11_works_without_sms_smtp(self):
        self.assertFalse(H.smtp_configured())
        self.assertFalse(H.sms_configured())
        # full Ready path already covered; ensure Not Ready also fine
        self._create_hvac(name="No SMTP")
        with app.app_context():
            job = H.get_db().execute("SELECT * FROM jobs ORDER BY id DESC").fetchone()
        self.client.post(f"/jobs/{job['id']}/send-now")
        rv = self.client.post(
            f"/h/{job['token']}/not-ready",
            data={"reason": "closet blocked"},
            follow_redirects=True,
        )
        self.assertEqual(rv.status_code, 200)
        with app.app_context():
            job = H.get_job(job["id"])
        self.assertEqual(job["status"], "held")

    def test_12_silence_auto_hold(self):
        self._create_hvac(name="Silent Customer", scheduled=self.today)
        with app.app_context():
            job = H.get_db().execute(
                "SELECT * FROM jobs WHERE customer_name = ?", ("Silent Customer",)
            ).fetchone()
            # Pretend checklist was sent earlier; force past cutoff by running auto-hold
            H.get_db().execute(
                "UPDATE jobs SET status = 'checklist_sent', checklist_sent_at = ? WHERE id = ?",
                (H.utc_now_iso(), job["id"]),
            )
            H.get_db().commit()
            # HOLD_CUTOFF_HOUR=6; if local now is before 6am UTC, force by setting scheduled_date to yesterday
            if H.local_now().hour < 6:
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                H.get_db().execute(
                    "UPDATE jobs SET scheduled_date = ? WHERE id = ?",
                    (yesterday, job["id"]),
                )
                H.get_db().commit()
            H.process_silence_auto_holds(app)
            job = H.get_job(job["id"])
            events = H.list_events(job["id"])
        self.assertEqual(job["status"], "held")
        self.assertTrue(any(e["kind"] == "auto_held_silence" for e in events))


if __name__ == "__main__":
    unittest.main()
