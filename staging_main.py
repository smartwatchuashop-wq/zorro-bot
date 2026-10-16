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

LOG = logging.getLogger("zorro.staging")
app = production_module.app


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

    if not db_name or not re.search(r"(?:^|[_-])(staging|stage|test)(?:$|[_-])", db_name, re.I):
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
    verify_staging_config()
    if os.getenv("ZORRO_STAGING_SYNC_ENABLED", "0") == "1":
        LOG.warning("Starting catalog sync on the confirmed staging-only PostgreSQL")
        threading.Thread(target=production_module.worker, daemon=True).start()
    else:
        LOG.info("Staging is running without catalog writes; sync disabled")


# Remove the production startup handler that otherwise writes to DATABASE_URL.
# Staging startup runs database verification *before* it may spawn the sync worker.
app.router.on_startup.clear()
app.add_event_handler("startup", staging_startup)


@app.get("/api/staging-status")
def staging_status():
    return {
        "environment": "staging",
        "catalog_sync_enabled": os.getenv("ZORRO_STAGING_SYNC_ENABLED", "0") == "1",
        "gemini_key_configured": bool(os.getenv("GEMINI_API_KEY")),
        "live_customer_notifications": False,
    }
