"""Contract tests: no DB/Gemini/Telegram calls and no order mutation."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import consultation_v7


P1 = {"site_id": "3718", "name": "Skmei Popular Silver II 1123S",
      "url": "https://zorro.com.ua/test-skmei/", "price": 595,
      "attributes": {}, "available": True}
P2 = {"site_id": "3949", "name": "Smart Space Pro",
      "url": "https://zorro.com.ua/test-smart/", "price": 1695,
      "attributes": {}, "available": True}


def request(message, ids, history=None):
    return SimpleNamespace(
        message=message, current_product=None,
        history=[SimpleNamespace(**h) for h in (history or [])],
        state={"product_ids": ids, "filters": {}, "search_intent": ""},
    )


class CheckoutIntegrationTests(unittest.TestCase):
    def test_buy_command_overrides_bad_store_info_intent(self):
        with patch("main.get_ids", return_value=[P1]), \
             patch("main.merge_filter_state", return_value={}), \
             patch("consultation_v7._model_call",
                   return_value={"actions":["store_info"], "mode":"refine"}):
            result = consultation_v7.run(request("Оформити замовлення", ["3718"]))
        self.assertEqual(result["checkout"]["url"], P1["url"])
        self.assertEqual(result["product_links"][0]["name"], P1["name"])
        self.assertNotIn("потрібно звіряти", result["reply"])

    def test_ambiguous_order_never_picks_arbitrary_model(self):
        with patch("main.get_ids", return_value=[P1, P2]), \
             patch("main.merge_filter_state", return_value={}), \
             patch("consultation_v7._model_call",
                   return_value={"actions":["checkout"], "mode":"refine"}):
            result = consultation_v7.run(request("Оформити замовлення", ["3718","3949"]))
        self.assertNotIn("checkout", result)
        self.assertEqual(len(result["product_links"]), 2)
        self.assertIn("Яку саме", result["reply"])

    def test_delivery_remains_separate_from_checkout(self):
        with patch("main.get_ids", return_value=[P1]), \
             patch("main.merge_filter_state", return_value={}), \
             patch("consultation_v7._model_call",
                   return_value={"actions":["store_info"], "mode":"refine"}):
            result = consultation_v7.run(request("Які умови доставки?", ["3718"]))
        self.assertNotIn("checkout", result)
        self.assertIn("доставки", result["reply"])

    def test_search_response_supplies_verified_text_links(self):
        with patch("main.get_ids", return_value=[]), \
             patch("main.merge_filter_state", return_value={}), \
             patch("main.shortlist", return_value=[P1]), \
             patch("main.render_product", return_value={"id":"3718"}), \
             patch("consultation_v7._rerank", return_value=[P1]), \
             patch("consultation_v7._respond",
                   return_value="Рекомендую Skmei Popular Silver II 1123S."), \
             patch("consultation_v7._model_call",
                   return_value={"actions":["search"], "mode":"new",
                                 "search_query":"годинник"}):
            result = consultation_v7.run(request("Порадь годинник", []))
        self.assertEqual(len(result["product_links"]), 1)
        self.assertEqual(result["product_links"][0]["url"], P1["url"])


if __name__ == "__main__":
    unittest.main()
