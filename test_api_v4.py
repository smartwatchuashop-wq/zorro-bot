"""FastAPI contract tests with no real DB, Gemini, or Telegram requests."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import main

class ApiContractTests(unittest.TestCase):
    def setUp(self):
        # No lifespan context: startup catalog sync is deliberately NOT triggered.
        self.client=TestClient(main.app)

    def test_chat_transfers_state_and_history(self):
        def fake_handle(data, *args):
            self.assertEqual(data.message,"Порівняй перший і третій")
            self.assertEqual(data.state["product_ids"],["11","22","33"])
            self.assertEqual(data.history[0].role,"user")
            return {"reply":"Ось порівняння","state":data.state,"products":[{"id":"11"}]}
        with patch("conversation_v4.handle_chat",side_effect=fake_handle) as mock:
            r=self.client.post("/api/chat",json={
                "message":"Порівняй перший і третій",
                "history":[{"role":"user","content":"Чоловічі до 2000 грн"}],
                "state":{"filters":{"max_price":2000},"product_ids":["11","22","33"]}
            })
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()["state"]["filters"]["max_price"],2000)
        self.assertEqual(mock.call_count,1)

    def test_empty_message_rejected(self):
        r=self.client.post("/api/chat",json={"message":""})
        self.assertEqual(r.status_code,422)

    def test_callback_without_phone_rejected(self):
        r=self.client.post("/api/callback",json={"phone":""})
        self.assertEqual(r.json()["status"],"error")

    def test_callback_without_telegram_success_does_not_claim_submission(self):
        with patch.object(main,"send_telegram_notification",return_value=False):
            r=self.client.post("/api/callback",json={"phone":"0981234567"})
        self.assertEqual(r.json()["status"],"error")

    def test_catalog_status_handles_unavailable_database(self):
        with patch.object(main,"db",side_effect=RuntimeError("offline")):
            r=self.client.get("/api/catalog-status")
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()["status"],"loading_or_error")

if __name__=="__main__":
    unittest.main()
