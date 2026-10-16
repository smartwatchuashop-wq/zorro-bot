# Zorro AI v4: staging verification and safe rollout

**Status: NOT FOR PRODUCTION.** An explicitly read-only connection to the existing production catalog is permitted for tests; all writes are forbidden. Work only on `zorro-hybrid-search-v4`. The Render production service follows `main` and must not be changed before verification.

## 1. Prepare isolated test infrastructure
1. Duplicate the Render web service as a **new staging service**, selecting branch `zorro-hybrid-search-v4`. Do not redeploy or change the existing `fastapi-qie2` service.
2. Use a **separate PostgreSQL database and DATABASE_URL**. Never connect staging to the production PostgreSQL: `main.py` has a synchronization worker that WRITES catalog rows and availability.
3. Configure a Gemini key in the staging environment only. Do not commit secrets to GitHub.
4. Disable real customer notifications/callbacks in staging or route them only to a controlled test recipient.
5. With synchronization explicitly enabled on the isolated database, let staging sync from the public feed; verify `GET /api/catalog-status` reports a sensible product count and latest sync.

### Exact Render staging service configuration

Create a new PostgreSQL datastore named clearly for staging. Use a database name like `zorro_staging_db`; a separate user and separate host are preferred. **Never copy the production DATABASE_URL.**

New staging web service:
- **Repository / branch:** `smartwatchuashop-wq/zorro-bot` / `zorro-hybrid-search-v4`.
- **Build command:** `pip install -r requirements.txt`.
- **Start command:** `uvicorn staging_main:app --host 0.0.0.0 --port $PORT` (**not** `main:app`).
- **Environment variables:**
  - `ZORRO_ENV=staging`
  - `DATABASE_URL=<the NEW staging PostgreSQL internal URL>`
  - `ZORRO_STAGING_DB_NAME=<exact DB name from that URL>`
  - `ZORRO_STAGING_DB_HOST=<exact DB hostname from that URL>`
  - `ZORRO_STAGING_SYNC_ENABLED=0` (initially; read-only startup, no catalog writer)
  - `GEMINI_API_KEY=<test key>` (optional for initial setup; required for real model evaluation)
- **Never set production Telegram tokens** on the staging service. The code blocks callbacks in staging even if tokens are accidentally inherited.
- Before first catalog sync, verify the *actual* Render datastore linked to `DATABASE_URL` is not production. After verifying, change `ZORRO_STAGING_SYNC_ENABLED=1` on **staging only** and redeploy this staging service; the existing catalog worker will import public feed rows into the new DB.
- `GET /api/staging-status` reports whether the test service is configured for sync and Gemini, without showing secrets.
- Remember: matching the database name/hostname is a second-line safeguard, **not** a substitute for verifying Render's database resource identity.

## Using the EXISTING Zorro database safely (no second PostgreSQL)

Render currently allows only one active free-tier PostgreSQL database in this workspace. The user explicitly chose to test against the existing `zorro_db` instead of paying for another database.

**Already performed:** real live-catalog SELECT checks through the Render plugin's read-only Postgres query tool. This does not deploy the new app, expose credentials or alter production.

**For future staging HTTP tests**, create a *separate web service* from the v4 branch (do NOT redeploy the current service). The staging entrypoint has an explicit existing-database read-only mode:

- Start command: `uvicorn staging_main:app --host 0.0.0.0 --port $PORT`
- `ZORRO_ENV=staging`
- `DATABASE_URL=<existing production database URL, added by the user in the Render environment settings; never paste it into chat or GitHub>`
- `ZORRO_STAGING_DB_NAME=zorro_db`
- `ZORRO_STAGING_DB_HOST=<verified actual hostname from the DATABASE_URL>`
- `ZORRO_USE_EXISTING_DB_READ_ONLY=1`
- `ZORRO_STAGING_SYNC_ENABLED=0` (MANDATORY)
- `GEMINI_API_KEY=<key entered via Render secrets, if AI language-generation tests are needed>`

In this mode: a) startup refuses another database name, mismatched host, or enabled sync; b) the production background catalog writer is replaced; c) all database connections opened through the API are marked `readonly=True` by psycopg2 before any SQL is executed; d) Telegram callbacks are disabled. GitHub Actions tests assert these guarantees and validate that PostgreSQL rejects attempted writes from a read-only session.

**Caution:** Reusing live data increases operational risk compared with an isolated database. Do not run migrations, schema setup, SQL writes, manual product updates, or sync jobs against `zorro_db`. The name/host confirmation protects against mistakes but must be checked against the actual Render resource by a human. Read-only SELECT queries through the Render tool are the preferred method until a separate service has been reviewed.

## 2. Automated CI
- Workflow `.github/workflows/v4-tests.yml` starts a disposable PostgreSQL 16 container and runs mocked conversation tests, real SQL/JSONB tests, API contract tests, and Node.js syntax checks for `zorro_widget_v4.html`.
- GitHub Actions test data is synthetic. CI does NOT validate the production PostgreSQL contents, actual Gemini quality, or any live Horoshop browser session.

## 3. Manual staging conversation test
POST `<STAGING_URL>/api/chat`, JSON:
```json
{"message":"Порадь чоловічий годинник до 2000 грн","history":[],"state":{}}
```
Retain the returned `state`, then POST:
```json
{"message":"А без шкіряного ремінця?","history":[{"role":"user","content":"Порадь чоловічий годинник до 2000 грн"}],"state":{"filters":{"max_price":2000,"audience":"men"},"product_ids":[]}}
```
Real flow must use the **actual first response state** rather than this example placeholder. Check that every recommendation is <= 2000 UAH, available, and that the non-leather claim is confirmed by the supplier's own attributes. Also test `Порівняй перший і третій`, `Розкажи детальніше про <exact shown model>`, questions about warranty, and an unavailable/unknown specification.

## 4. Widget
- `zorro_widget_v4.html` preserves the original Horoshop UI and callback form.
- Before testing, set `window.ZORRO_AI_API_BASE = '<STAGING_URL>';` in the page **before** the widget snippet.
- Test on an internal preview/staging page; **do not paste into live Horoshop yet**.
- Inspect browser Network: POST `/api/chat` must include `history` and `state`, and subsequent calls must carry the returned values.
- Test mobile dimensions, close/reopen, page navigation persistence, reset, callback failure, slow requests and requests containing HTML text.

## 5. Go/no-go
Do NOT merge to `main` until all are independently verified:
- No budget violations, no false material/function claims, no out-of-stock card sold as in-stock.
- Gemini keeps a multi-turn shopping context; only named selected cards are described/compared.
- Staging Render logs show no errors; the actual supplier feed is synchronized.
- Browser widget works on Horoshop preview for mobile and desktop.
- Callback forms cannot silently report success on failed delivery.

The user has expressly asked not to alter their live site before staging is validated.
