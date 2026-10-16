"""Staging must never start the production synchronization worker by accident."""
import os
import unittest
from unittest.mock import patch

import main
import staging_main
from fastapi.testclient import TestClient

DB_URL = "postgresql://staging_user:dummy@zorro-staging-db.internal:5432/zorro_staging_db"
CONF = {
    "ZORRO_ENV": "staging",
    "DATABASE_URL": DB_URL,
    "ZORRO_STAGING_DB_NAME": "zorro_staging_db",
    "ZORRO_STAGING_DB_HOST": "zorro-staging-db.internal",
    "ZORRO_STAGING_SYNC_ENABLED": "0"
}

class StagingSafetyTests(unittest.TestCase):
    def test_staging_startup_replaces_production_writer(self):
        self.assertNotIn(main.start, staging_main.app.router.on_startup)
        self.assertIn(staging_main.staging_startup, staging_main.app.router.on_startup)

    def test_production_database_name_is_rejected(self):
        bad = dict(CONF, DATABASE_URL="postgresql://prod:secret@prod.internal/zorro_products")
        with patch.dict(os.environ,bad,clear=True):
            with self.assertRaises(RuntimeError):
                staging_main.verify_staging_config()

    def test_missing_staging_environment_rejected(self):
        bad = dict(CONF)
        bad.pop("ZORRO_ENV")
        with patch.dict(os.environ,bad,clear=True):
            with self.assertRaises(RuntimeError):
                staging_main.verify_staging_config()

    def test_wrong_database_host_rejected(self):
        bad = dict(CONF, ZORRO_STAGING_DB_HOST="production.internal")
        with patch.dict(os.environ,bad,clear=True):
            with self.assertRaises(RuntimeError):
                staging_main.verify_staging_config()

    def test_database_name_requires_exact_confirmation(self):
        bad = dict(CONF, ZORRO_STAGING_DB_NAME="wrong_staging_db")
        with patch.dict(os.environ,bad,clear=True):
            with self.assertRaises(RuntimeError):
                staging_main.verify_staging_config()

    def test_no_catalog_writer_by_default(self):
        with patch.dict(os.environ,CONF,clear=True), patch.object(staging_main.threading,"Thread") as thread:
            staging_main.staging_startup()
            thread.assert_not_called()

    def test_catalog_writer_only_when_explicitly_enabled(self):
        cfg = dict(CONF, ZORRO_STAGING_SYNC_ENABLED="1")
        with patch.dict(os.environ,cfg,clear=True), patch.object(staging_main.threading,"Thread") as thread:
            staging_main.staging_startup()
            thread.assert_called_once()
            self.assertIs(thread.call_args.kwargs["target"], main.worker)
            thread.return_value.start.assert_called_once()

    def test_http_server_starts_read_only_with_confirmed_database(self):
        with patch.dict(os.environ, CONF, clear=True), patch.object(staging_main.threading, "Thread") as thread:
            with TestClient(staging_main.app) as client:
                result = client.get("/api/staging-status")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["environment"], "staging")
                self.assertFalse(result.json()["catalog_sync_enabled"])
                self.assertFalse(result.json()["live_customer_notifications"])
            thread.assert_not_called()

    def test_http_server_refuses_production_database_before_startup(self):
        unsafe = dict(CONF, DATABASE_URL="postgresql://prod:secret@prod.internal/zorro_products")
        with patch.dict(os.environ, unsafe, clear=True), patch.object(staging_main.threading, "Thread") as thread:
            with self.assertRaises(RuntimeError):
                with TestClient(staging_main.app):
                    pass
            thread.assert_not_called()

    def test_existing_database_requires_explicit_readonly_flag(self):
        cfg=dict(CONF, DATABASE_URL="postgresql://prod:secret@zorro-prod.internal/zorro_db",
                 ZORRO_STAGING_DB_NAME="zorro_db", ZORRO_STAGING_DB_HOST="zorro-prod.internal")
        with patch.dict(os.environ,cfg,clear=True):
            with self.assertRaises(RuntimeError):
                staging_main.verify_staging_config()

    def test_existing_database_never_starts_catalog_worker(self):
        cfg=dict(CONF, DATABASE_URL="postgresql://prod:secret@zorro-prod.internal/zorro_db",
                 ZORRO_STAGING_DB_NAME="zorro_db", ZORRO_STAGING_DB_HOST="zorro-prod.internal",
                 ZORRO_USE_EXISTING_DB_READ_ONLY="1")
        with patch.dict(os.environ,cfg,clear=True), patch.object(main,"db") as db, patch.object(staging_main.threading,"Thread") as thread:
            staging_main.staging_startup()
            self.assertIs(main.db, staging_main._read_only_db)
            self.assertTrue(staging_main.staging_status()["existing_database_read_only"])
            thread.assert_not_called()

    def test_existing_database_explicit_sync_is_forbidden(self):
        cfg=dict(CONF, DATABASE_URL="postgresql://prod:secret@zorro-prod.internal/zorro_db",
                 ZORRO_STAGING_DB_NAME="zorro_db", ZORRO_STAGING_DB_HOST="zorro-prod.internal",
                 ZORRO_USE_EXISTING_DB_READ_ONLY="1", ZORRO_STAGING_SYNC_ENABLED="1")
        with patch.dict(os.environ,cfg,clear=True), patch.object(staging_main.threading,"Thread") as thread:
            with self.assertRaises(RuntimeError):
                staging_main.staging_startup()
            thread.assert_not_called()

    def test_existing_database_never_accepts_other_db_name(self):
        cfg=dict(CONF, ZORRO_USE_EXISTING_DB_READ_ONLY="1")
        with patch.dict(os.environ,cfg,clear=True):
            with self.assertRaises(RuntimeError):
                staging_main.verify_staging_config()

    def test_readonly_connection_is_established_before_any_query(self):
        from unittest.mock import MagicMock
        conn=MagicMock()
        with patch.object(staging_main,"_ORIGINAL_DB",return_value=conn):
            result=staging_main._read_only_db()
        self.assertIs(result,conn)
        conn.set_session.assert_called_once_with(readonly=True)

    def test_readonly_connection_failure_closes_connection(self):
        from unittest.mock import MagicMock
        conn=MagicMock()
        conn.set_session.side_effect=RuntimeError("cannot enforce read-only")
        with patch.object(staging_main,"_ORIGINAL_DB",return_value=conn):
            with self.assertRaises(RuntimeError):
                staging_main._read_only_db()
        conn.close.assert_called_once()

    def test_staging_callback_cannot_send_real_message(self):
        self.assertFalse(main.send_telegram_notification("+380991234567", "test"))

if __name__ == "__main__":
    unittest.main()
