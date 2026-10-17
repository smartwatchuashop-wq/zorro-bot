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
        with patch.dict(os.environ,cfg,clear=True), patch.object(main,"db") as db, patch.object(staging_main.threading,"Thread") as thread, patch.object(staging_main, "_verify_live_read_only_catalog", return_value=1035) as probe:
            staging_main.staging_startup()
            self.assertIs(main.db, staging_main._read_only_db)
            self.assertTrue(staging_main.staging_status()["existing_database_read_only"])
            thread.assert_not_called()
            probe.assert_called_once_with()

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

    def test_setup_mode_runs_without_secrets_or_database(self):
        cfg={"ZORRO_ENV":"staging","ZORRO_STAGING_SETUP_MODE":"1",
             "ZORRO_USE_EXISTING_DB_READ_ONLY":"1","ZORRO_STAGING_SYNC_ENABLED":"0",
             "ZORRO_STAGING_DB_NAME":"zorro_db"}
        with patch.dict(os.environ,cfg,clear=True), patch.object(staging_main.threading,"Thread") as thread:
            with TestClient(staging_main.app) as client:
                status=client.get("/api/staging-status")
                self.assertEqual(status.status_code,200)
                self.assertTrue(status.json()["setup_required"])
                self.assertFalse(status.json()["database_configured"])
                chat=client.post("/api/chat",json={"message":"Підбери смартгодинник","history":[],"state":{}})
                self.assertEqual(chat.status_code,503)
                self.assertEqual(chat.json()["status"],"setup_required")
            thread.assert_not_called()

    def test_setup_mode_never_allows_sync(self):
        cfg={"ZORRO_ENV":"staging","ZORRO_STAGING_SETUP_MODE":"1",
             "ZORRO_USE_EXISTING_DB_READ_ONLY":"1","ZORRO_STAGING_SYNC_ENABLED":"1"}
        with patch.dict(os.environ,cfg,clear=True):
            with self.assertRaises(RuntimeError):
                staging_main.staging_startup()

    def test_setup_mode_not_allowed_outside_staging(self):
        cfg={"ZORRO_ENV":"production","ZORRO_STAGING_SETUP_MODE":"1",
             "ZORRO_USE_EXISTING_DB_READ_ONLY":"1","ZORRO_STAGING_SYNC_ENABLED":"0"}
        with patch.dict(os.environ,cfg,clear=True):
            with self.assertRaises(RuntimeError):
                staging_main.staging_startup()

    def test_staging_chat_is_private_even_with_database_config(self):
        cfg=dict(CONF,ZORRO_STAGING_ACCESS_TOKEN="A" * 40)
        with patch.dict(os.environ,cfg,clear=True):
            with TestClient(staging_main.app) as client:
                denied=client.post("/api/chat",json={"message":"Доброго дня"})
                self.assertEqual(denied.status_code,403)
                self.assertEqual(denied.json()["status"],"forbidden")
                denied_catalog=client.get("/api/catalog-status")
                self.assertEqual(denied_catalog.status_code,403)

    def test_missing_staging_secret_blocks_requests(self):
        cfg=dict(CONF)
        with patch.dict(os.environ,cfg,clear=True):
            with TestClient(staging_main.app) as client:
                response=client.post("/api/chat",json={"message":"Доброго дня"})
                self.assertEqual(response.status_code,503)
                self.assertFalse(client.get("/api/staging-status").json()["private_access_ready"])

    def test_valid_secret_allows_safe_callback_endpoint(self):
        cfg=dict(CONF,ZORRO_STAGING_ACCESS_TOKEN="B" * 40)
        with patch.dict(os.environ,cfg,clear=True):
            with TestClient(staging_main.app) as client:
                response=client.post("/api/callback",json={"phone":"+380990000001"},
                    headers={"x-zorro-staging-token":"B" * 40})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()["status"],"error")
                self.assertTrue(client.get("/api/staging-status").json()["private_access_ready"])

    def test_live_sql_probe_accepts_only_actual_readonly_zorro_db(self):
        from unittest.mock import MagicMock
        conn=MagicMock()
        cur=conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value=("zorro_db","on",1035)
        with patch.object(staging_main,"_read_only_db",return_value=conn):
            count=staging_main._verify_live_read_only_catalog()
        self.assertEqual(count,1035)
        self.assertIn("COUNT(*)",cur.execute.call_args.args[0])
        conn.close.assert_called_once()

    def test_live_sql_probe_rejects_wrong_db(self):
        from unittest.mock import MagicMock
        conn=MagicMock()
        cur=conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value=("zorro_test","on",5)
        with patch.object(staging_main,"_read_only_db",return_value=conn):
            with self.assertRaises(RuntimeError):
                staging_main._verify_live_read_only_catalog()
        conn.close.assert_called_once()

    def test_live_sql_probe_rejects_non_readonly_session(self):
        from unittest.mock import MagicMock
        conn=MagicMock()
        cur=conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value=("zorro_db","off",1035)
        with patch.object(staging_main,"_read_only_db",return_value=conn):
            with self.assertRaises(RuntimeError):
                staging_main._verify_live_read_only_catalog()
        conn.close.assert_called_once()

    def test_gemini_smoke_starts_only_with_opt_in_and_key(self):
        cfg=dict(CONF,DATABASE_URL="postgresql://prod:secret@prod.internal/zorro_db",
                 ZORRO_STAGING_DB_NAME="zorro_db",ZORRO_STAGING_DB_HOST="prod.internal",
                 ZORRO_USE_EXISTING_DB_READ_ONLY="1",ZORRO_STAGING_GEMINI_SMOKE="1",
                 GEMINI_API_KEY="dummy-only-for-tests")
        with patch.dict(os.environ,cfg,clear=True), patch.object(
                staging_main,"_verify_live_read_only_catalog",return_value=1035), patch.object(
                staging_main.threading,"Thread") as thread:
            staging_main.staging_startup()
            thread.assert_called_once()
            self.assertIs(thread.call_args.kwargs["target"],
                          staging_main._run_smoke_with_error_logging)
            thread.return_value.start.assert_called_once()

    def test_gemini_smoke_fails_closed_without_key(self):
        cfg=dict(CONF,DATABASE_URL="postgresql://prod:secret@prod.internal/zorro_db",
                 ZORRO_STAGING_DB_NAME="zorro_db",ZORRO_STAGING_DB_HOST="prod.internal",
                 ZORRO_USE_EXISTING_DB_READ_ONLY="1",ZORRO_STAGING_GEMINI_SMOKE="1")
        with patch.dict(os.environ,cfg,clear=True), patch.object(
                staging_main,"_verify_live_read_only_catalog",return_value=1035), patch.object(
                staging_main.threading,"Thread") as thread:
            with self.assertRaises(RuntimeError):
                staging_main.staging_startup()
            thread.assert_not_called()

    def test_live_acceptance_is_explicitly_opt_in(self):
        cfg=dict(CONF,DATABASE_URL="postgresql://prod:secret@prod.internal/zorro_db",
                 ZORRO_STAGING_DB_NAME="zorro_db",ZORRO_STAGING_DB_HOST="prod.internal",
                 ZORRO_USE_EXISTING_DB_READ_ONLY="1",ZORRO_STAGING_LIVE_ACCEPTANCE="1",
                 GEMINI_API_KEY="test-only-not-a-real-key")
        with patch.dict(os.environ,cfg,clear=True), patch.object(
                staging_main,"_verify_live_read_only_catalog",return_value=1035), patch.object(
                staging_main.threading,"Thread") as thread:
            staging_main.staging_startup()
            thread.assert_called_once()
            self.assertIs(thread.call_args.kwargs["target"],
                          staging_main._run_live_acceptance)
            thread.return_value.start.assert_called_once()

    def test_live_acceptance_refuses_missing_gemini_key(self):
        cfg=dict(CONF,DATABASE_URL="postgresql://prod:secret@prod.internal/zorro_db",
                 ZORRO_STAGING_DB_NAME="zorro_db",ZORRO_STAGING_DB_HOST="prod.internal",
                 ZORRO_USE_EXISTING_DB_READ_ONLY="1",ZORRO_STAGING_LIVE_ACCEPTANCE="1")
        with patch.dict(os.environ,cfg,clear=True), patch.object(
                staging_main,"_verify_live_read_only_catalog",return_value=1035), patch.object(
                staging_main.threading,"Thread") as thread:
            with self.assertRaises(RuntimeError):
                staging_main.staging_startup()
            thread.assert_not_called()

    def test_staging_callback_cannot_send_real_message(self):
        self.assertFalse(main.send_telegram_notification("+380991234567", "test"))

if __name__ == "__main__":
    unittest.main()
