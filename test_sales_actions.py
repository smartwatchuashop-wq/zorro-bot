"""Offline regression checks for Zorro checkout and verified product links."""
import unittest
from sales_actions import (
    explicit_checkout_intent, trusted_product_url,
    resolve_checkout_product, verified_product_links,
)


P1 = {"site_id": "3718", "name": "Skmei Popular Silver II 1123S",
      "url": "https://zorro.com.ua/test-skmei/", "available": True}
P2 = {"site_id": "3949", "name": "Smart Space Pro",
      "url": "https://zorro.com.ua/test-smart/", "available": True}


class SalesActionsTests(unittest.TestCase):
    def test_explicit_purchase_command(self):
        for phrase in ["Оформити замовлення", "Купую", "Хочу купити",
                       "Як оформити замовлення?", "Беру"]:
            self.assertTrue(explicit_checkout_intent(phrase), phrase)

    def test_no_checkout_for_delivery_and_price(self):
        for phrase in ["Які умови доставки?", "Скільки коштує?",
                       "Порадь годинник", "Гарантія при оформленні замовлення",
                       "Не хочу оформляти замовлення"]:
            self.assertFalse(explicit_checkout_intent(phrase), phrase)

    def test_one_shown_product(self):
        self.assertEqual(
            resolve_checkout_product([P1], {}, "Оформити замовлення", []), P1)

    def test_two_products_require_selection(self):
        self.assertIsNone(
            resolve_checkout_product([P1, P2], {}, "Оформити замовлення", []))

    def test_explicit_position(self):
        self.assertEqual(resolve_checkout_product(
            [P1, P2], {"positions": [2]}, "Беру другий", []), P2)

    def test_unambiguous_last_recommendation(self):
        h = [{"role": "assistant", "content": "Раджу Skmei Popular Silver II 1123S."}]
        self.assertEqual(resolve_checkout_product(
            [P1, P2], {}, "Оформити замовлення", h), P1)

    def test_ambiguous_last_assistant(self):
        h = [{"role": "assistant", "content": "Порівняємо Skmei Popular Silver II 1123S та Smart Space Pro."}]
        self.assertIsNone(resolve_checkout_product(
            [P1, P2], {}, "Оформити замовлення", h))

    def test_only_store_links(self):
        self.assertTrue(trusted_product_url(P1["url"]))
        for bad in ["javascript:alert(1)", "https://best-time.biz/p",
                    "https://zorro.com.ua.evil.org/p", "http://zorro.com.ua/p",
                    "https://user:pass@zorro.com.ua/p"]:
            self.assertEqual(trusted_product_url(bad), "", bad)

    def test_names_linked_only_if_present(self):
        self.assertEqual(verified_product_links(
            "Раджу Skmei Popular Silver II 1123S за вашим бюджетом.", [P1, P2]),
            [{"id": "3718", "name": P1["name"], "url": P1["url"]}])

    def test_untrusted_url_not_linked(self):
        fake = {**P1, "url": "https://best-time.biz/p"}
        self.assertEqual(verified_product_links(P1["name"], [fake]), [])


if __name__ == "__main__":
    unittest.main()
