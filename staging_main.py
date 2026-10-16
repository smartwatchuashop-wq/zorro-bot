"""Isolated Zorro v4 staging entrypoint.

Start with: uvicorn staging_main:app --host 0.0.0.0 --port $PORT
Do not use main:app for staging: main.py starts a catalog writer unconditionally
whenever DATABASE_URL is present.
"""
from __future__ import annotations

import logging
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
    if _setup_pending() and request.url.path not in ("/", "/api/staging-status"):
        return JSONResponse(status_code=503, content={
            "status": "setup_required",
            "message": "Database has not been connected to the staging service"
        })
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
        LOG.warning("Existing zorro_db mounted in read-only mode; all catalog writes disabled")
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
        "catalog_sync_enabled": os.getenv("ZORRO_STAGING_SYNC_ENABLED", "0") == "1",
        "gemini_key_configured": bool(os.getenv("GEMINI_API_KEY")),
        "live_customer_notifications": False,
        "existing_database_read_only": os.getenv("ZORRO_USE_EXISTING_DB_READ_ONLY") == "1",
    }
