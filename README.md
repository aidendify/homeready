# HomeReady

Free, self-hosted night-before home-prep checklists for local service owners. Pick a trade template, send the customer a magic link, get Ready or Not Ready, and hold the truck when the home isn’t ready.

No signup. No license. One Docker Compose service and a SQLite file. About 15 minutes on a 1GB VPS.

## What it does

- Create a job from one of four trade templates (HVAC install, water heater, under-sink plumber, remodel punch / install day)
- Snapshot checklist items onto the job; night-before send (or **Send checklist now**)
- Share an unguessable public link `{PUBLIC_BASE_URL}/h/{token}` — Copy link, Copy blurb
- Customer checks items → **I’m Ready** or **Not Ready** (reason required)
- Morning board: today’s jobs, hold queue with reschedule draft copy, silence auto-hold past cutoff
- Optional BYO SMTP email and/or Twilio SMS — works fully with Copy link alone
- `GET /health` → HTTP 200 `{"status":"ok","smtp_configured":false,"sms_configured":false}` even when SMTP/Twilio unset

Without SMTP or Twilio you still get in-app status and can copy the link / blurb / reschedule draft.

## Privacy

Self-hosted. You run the box; the owner is the data controller for customer and crew contact fields. No Stripe, no bundled SMS numbers, no third-party analytics SaaS. Data lives in your SQLite file on the Compose volume. HomeReady never stores gate codes or lockbox PINs.

## 15-minute Ubuntu VPS install

Documented on **Ubuntu 22.04 / 24.04**. About 15 minutes.

**Debian 13:** do **not** run the Ubuntu `docker-ce` recipe below on Debian. Use the distro packages instead:

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose
sudo usermod -aG docker "$USER"
```

Log out and back in (or `newgrp docker`). On Debian, start the stack with `docker-compose` (hyphen) if `docker compose` is not available.

**Amazon Linux:** not documented yet. Use Ubuntu or Debian.

### 1. Install Docker Engine and the Compose plugin (Ubuntu only)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo ${UBUNTU_CODENAME:-$VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so `docker` works without `sudo`.

### 2. Clone, configure, start

```bash
git clone https://github.com/aidendify/homeready.git
cd homeready
cp .env.example .env
```

Edit `.env` and set at least `BUSINESS_NAME`, `PUBLIC_BASE_URL`, `SECRET_KEY`, and `OWNER_PASSWORD`. Prefer `TZ` plus `CHECKLIST_SEND_HOUR` / `HOLD_CUTOFF_HOUR`. Leave `SMTP_*`, Twilio, and `MARKETING_URL` empty unless configured. Set `OWNER_PASSWORD` on any VPS reachable from the internet (empty means the admin UI is open).

```bash
docker compose up --build -d
```

(On Debian, `docker-compose up --build -d` if the Compose plugin is not installed.)

The app binds `0.0.0.0:8080` in the container. Compose maps host `8080:8080`. SQLite lives on the `homeready-data` volume at `/data/homeready.db`.

### 3. Smoke test

Use this `.env` for a first pass (Verifier values). Production should use a real `SECRET_KEY` and `OWNER_PASSWORD`. Do not bake these test passwords as production defaults.

```
OWNER_PASSWORD=testpass
PUBLIC_BASE_URL=http://localhost:8080
BUSINESS_NAME=Harbor HVAC
TZ=UTC
CHECKLIST_SEND_HOUR=18
HOLD_CUTOFF_HOUR=6
MARKETING_URL=
SECRET_KEY=change-me
```

Leave all `SMTP_*` and Twilio vars unset.

1. Healthcheck:

   ```bash
   curl -sf http://localhost:8080/health
   ```

   Expected: JSON containing `"status":"ok"`, `"smtp_configured":false`, `"sms_configured":false`, HTTP 200.

2. Open http://localhost:8080, log in with `testpass`, create an **HVAC install** job. On the detail page, hit **Send checklist now**. Copy the `/h/{token}` link.

3. Open the public link. Confirm business name, HVAC-style items (closet / outdoor / pets / adult), check all items, tap **I’m Ready**. Detail should show status `ready` and a ready event. No crash without SMTP.

4. Create a second job, **Send checklist now**, open the link, tap **Not Ready** with reason **pets not secured**. Confirm the hold queue on the board shows the job and a **Copy reschedule draft**.

## Configuration

Copy `.env.example` to `.env` before `docker compose up`. Variables:

| Variable | Purpose |
| --- | --- |
| `PORT` | Documented as 8080. The container always binds gunicorn to `0.0.0.0:8080`. |
| `DATABASE_PATH` | SQLite file. Compose overrides this to `/data/homeready.db`. |
| `SECRET_KEY` | Flask session key. Change it on a public VPS. |
| `OWNER_PASSWORD` | Admin login. Empty = open admin (local/dev). Set this on any internet-reachable VPS. |
| `BUSINESS_NAME` | Public page. |
| `BUSINESS_PHONE` | Optional phone shown under "Questions? Call us." |
| `PUBLIC_BASE_URL` | No trailing slash. Used in magic links, e.g. `http://localhost:8080`. |
| `TZ` | Default `UTC`. Night-before send and morning cutoff use this zone. |
| `CHECKLIST_SEND_HOUR` | Default 18. Hour (0–23) the evening before `scheduled_date` to mark checklist sent. |
| `HOLD_CUTOFF_HOUR` | Default 6. Morning of job date: still no Ready → auto-held. |
| `FROM_NAME`, `FROM_EMAIL` | SMTP From / email sign-off. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TLS` | Optional email notify. If `SMTP_HOST` is unset, email notify is unused. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Optional SMS notify. If unset, SMS is unused. |
| `OWNER_NOTIFY_EMAIL` | Optional owner alerts when SMTP is set. |
| `MARKETING_URL` | If set, footer link **Powered by HomeReady** points here. If unset, there is no footer. |

Do not commit `.env`. SMTP / Twilio secrets and `OWNER_PASSWORD` are never written to application logs.

## Healthcheck

`GET /health` → HTTP 200:

```json
{"status":"ok","smtp_configured":false,"sms_configured":false}
```

`smtp_configured` is `true` only when `SMTP_HOST` is set. `sms_configured` is `true` only when all three Twilio vars are set. Health succeeds even when both are unset. This route never requires login.

## Local development (optional)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_PATH=./homeready.db
export OWNER_PASSWORD=testpass
export PUBLIC_BASE_URL=http://localhost:8080
export BUSINESS_NAME="Harbor HVAC"
export HOMEREADY_DISABLE_SCHEDULER=1
python app.py
```

Then open http://localhost:8080. This path is for hacking on the code; the supported install is Docker Compose.

```bash
python -m unittest test_app.py -v
```

## What this is not

HomeReady is **not** VisitGate (no gate-code / lockbox vault). It is **not** a full FSM/CRM or calendar platform (no Google Calendar OAuth, no auto-book). It is **not** WhatsApp. It is **not** SkyHold (no weather Hold/Proceed). It is **not** PartPing (no parts milestones). It is **not** ChangeSlip (no priced Accept). It is **not** OpenPing (no quote open-tracking). It is **not** AfterJob (no CSAT / review ask). It is **not** FormFirst (no contact-form webhook). It is **not** Nudge (no drips).

No maps, no payments, no Redis, no Celery, no LLM, no second Compose service.
