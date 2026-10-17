"""Isolated Zorro v4 staging entrypoint.

Start with: uvicorn staging_main:app --host 0.0.0.0 --port $PORT
Do not use main:app for staging: main.py starts a catalog writer unconditionally
whenever DATABASE_URL is present.
"""
from __future__ import annotations

import logging
import secrets
import os
import re
import threading
from urllib.parse import unquote, urlsplit

import main as production_module
from fastapi.responses import JSONResponse

LOG = logging.getLogger("zorro.staging")
app = production_module.app

def _setup_pending() -> bool:
    # Bootstrap without secrets for a new *separate* Free test service.
    return os.getenv("ZORRO_STAGING_SETUP_MODE") == "1" and not os.getenv("DATABASE_URL")


def _disabled_db():
    raise RuntimeError("Staging database has not been configured")


@app.middleware("http")
async def _setup_gate(request, call_next):
    # Everything beyond public health/status checks is private on staging.
    # In particular, the chat endpoint can spend Gemini API credits.
    if os.getenv("ZORRO_ENV") == "staging" and request.url.path not in ("/", "/api/staging-status"):
        if _setup_pending():
            return JSONResponse(status_code=503, content={
                "status": "setup_required",
                "message": "Database has not been connected to the staging service"
            })
        access_token = os.getenv("ZORRO_STAGING_ACCESS_TOKEN", "")
        if len(access_token) < 24:
            return JSONResponse(status_code=503, content={
                "status": "setup_required",
                "message": "A private staging access token has not been configured"
            })
        presented = request.headers.get("x-zorro-staging-token", "")
        if not secrets.compare_digest(presented, access_token):
            return JSONResponse(status_code=403, content={"status": "forbidden"})
    return await call_next(request)


_ORIGINAL_DB = production_module.db


def _read_only_db():
    """Force SQL read-only at the PostgreSQL session level before queries."""
    conn = _ORIGINAL_DB()
    try:
        conn.set_session(readonly=True)
    except Exception:
        conn.close()
        raise
    return conn


def _verify_live_read_only_catalog() -> int:
    """SELECT-only probe on the existing database, with SQL-level read-only enforcement."""
    conn = _read_only_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_setting('transaction_read_only'), "
                "COUNT(*) FROM zorro_products_v2"
            )
            db_name, readonly, count = cur.fetchone()
            if db_name != "zorro_db" or readonly != "on":
                raise RuntimeError("Staging database read-only safety verification failed")
            return int(count)
    finally:
        conn.close()


def verify_staging_config() -> tuple[str, str]:
    """Fail closed before any catalog/database background writes."""
    if os.getenv("ZORRO_ENV") != "staging":
        raise RuntimeError("Set ZORRO_ENV=staging to start the isolated staging service")

    raw = os.getenv("DATABASE_URL", "")
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname:
            raise ValueError("not a PostgreSQL URL")
        db_name = unquote(parsed.path.lstrip("/"))
        db_host = parsed.hostname
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("Staging requires a separate DATABASE_URL") from exc

    existing_readonly = os.getenv("ZORRO_USE_EXISTING_DB_READ_ONLY") == "1"
    if existing_readonly:
        if db_name != "zorro_db":
            raise RuntimeError("Read-only existing-database mode only permits the verified zorro_db")
        if os.getenv("ZORRO_STAGING_SYNC_ENABLED", "0") != "0":
            raise RuntimeError("Production database is read-only: catalog sync must be disabled")
    elif not db_name or not re.search(r"(?:^|[_-])(staging|stage|test)(?:$|[_-])", db_name, re.I):
        raise RuntimeError("Refusing to run: staging database name must contain staging/stage/test")
    if os.getenv("ZORRO_STAGING_DB_NAME") != db_name:
        raise RuntimeError("Staging DB name does not match ZORRO_STAGING_DB_NAME")
    if os.getenv("ZORRO_STAGING_DB_HOST") != db_host:
        raise RuntimeError("Staging DB host does not match ZORRO_STAGING_DB_HOST")
    if not os.getenv("ZORRO_STAGING_DB_HOST") or not os.getenv("ZORRO_STAGING_DB_NAME"):
        raise RuntimeError("Staging DB name and host must both be explicitly confirmed")
    return db_host, db_name


# Prevent SMS/Telegram/customer callbacks from being sent in staging,
# even if Render copies an old production token into the staging environment.
production_module.send_telegram_notification = lambda phone, message="": False


def _run_smoke_with_error_logging() -> None:
    from staging_smoke_v4 import run_smoke
    try:
        run_smoke()
    except Exception:
        LOG.exception("GEMINI_SMOKE RESULT=FAIL (inspect logs; no production data changed)")


def _run_live_acceptance() -> None:
    from staging_acceptance_v4 import run_acceptance
    try:
        run_acceptance()
    except Exception:
        LOG.exception("ACCEPTANCE RESULT=FAIL (see individual test results)")


def staging_startup() -> None:
    if _setup_pending():
        if (os.getenv("ZORRO_ENV") != "staging"
                or os.getenv("ZORRO_USE_EXISTING_DB_READ_ONLY") != "1"
                or os.getenv("ZORRO_STAGING_SYNC_ENABLED", "0") != "0"):
            raise RuntimeError("Unsafe staging setup configuration")
        production_module.db = _disabled_db
        LOG.warning("Staging bootstrap online without database or Gemini secrets")
        return
    verify_staging_config()
    if os.getenv("ZORRO_USE_EXISTING_DB_READ_ONLY") == "1":
        production_module.db = _read_only_db
        count = _verify_live_read_only_catalog()
        LOG.warning(
            "Existing zorro_db verified read-only with %s catalog products; "
            "private access ready=%s; Gemini configured=%s",
            count,
            len(os.getenv("ZORRO_STAGING_ACCESS_TOKEN", "")) >= 24,
            bool(os.getenv("GEMINI_API_KEY")),
        )
        if os.getenv("ZORRO_STAGING_GEMINI_SMOKE", "0") == "1":
            if not os.getenv("GEMINI_API_KEY"):
                raise RuntimeError("Cannot smoke-test Gemini without a configured API key")
            threading.Thread(target=_run_smoke_with_error_logging, daemon=True).start()
        if os.getenv("ZORRO_STAGING_LIVE_ACCEPTANCE", "0") == "1":
            if not os.getenv("GEMINI_API_KEY"):
                raise RuntimeError("Cannot run live acceptance without Gemini key")
            threading.Thread(target=_run_live_acceptance, daemon=True).start()
        return
    production_module.db = _ORIGINAL_DB
    if os.getenv("ZORRO_STAGING_SYNC_ENABLED", "0") == "1":
        LOG.warning("Starting catalog sync on the confirmed staging-only PostgreSQL")
        threading.Thread(target=production_module.worker, daemon=True).start()
    else:
        LOG.info("Staging is running without catalog writes; sync disabled")


# Remove the production startup handler that otherwise writes to DATABASE_URL.
# Staging startup runs database verification *before* it may spawn the sync worker.
app.router.on_startup.clear()
app.router.on_startup.append(staging_startup)


@app.get("/api/staging-status")
def staging_status():
    return {
        "environment": "staging",
        "setup_required": _setup_pending(),
        "database_configured": bool(os.getenv("DATABASE_URL")),
        "private_access_ready": len(os.getenv("ZORRO_STAGING_ACCESS_TOKEN", "")) >= 24,
        "catalog_sync_enabled": os.getenv("ZORRO_STAGING_SYNC_ENABLED", "0") == "1",
        "gemini_key_configured": bool(os.getenv("GEMINI_API_KEY")),
        "live_customer_notifications": False,
        "existing_database_read_only": os.getenv("ZORRO_USE_EXISTING_DB_READ_ONLY") == "1",
    }
