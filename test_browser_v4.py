"""Real Chromium widget tests. Isolated localhost fixture; no Zorro production API calls."""
import functools
import http.server
import json
import threading
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent


class WidgetBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.page = self.browser.new_page()
        self.requests = []
        self.page.add_init_script("window.ZORRO_AI_API_BASE = 'https://api.zorro.test';")

    def tearDown(self):
        self.page.close()

    def open(self, html="zorro_widget_v4.html"):
        self.page.goto(self.base + "/" + html)
        self.page.locator("#ai-chat-toggle").click()
        self.assertTrue(self.page.locator("#ai-chat-box").is_visible())

    def mock_chat(self, reply=None, state=None):
        reply = reply or ('<div class="bot-answer">Перевірена відповідь</div>'
                          '<div class="ai-card-action" data-action="desc" data-name="Casio Classic">Опис товару</div>'
                          '<a href="https://zorro.com.ua/casio-classic/">Купити</a>')
        state = state or {"filters":{"max_price":1800}, "product_ids":["3718"]}
        def handler(route):
            headers={"Access-Control-Allow-Origin":"*",
                     "Access-Control-Allow-Headers":"Content-Type",
                     "Access-Control-Allow-Methods":"OPTIONS,POST"}
            if route.request.method == "OPTIONS":
                route.fulfill(status=204,headers=headers)
                return
            self.requests.append(route.request.post_data_json)
            route.fulfill(status=200, content_type="application/json",headers=headers,
                          body=json.dumps({"reply":reply, "state":state},ensure_ascii=False))
        self.page.route("https://api.zorro.test/api/chat", handler)

    def send(self, text):
        self.page.locator("#ai-chat-input").fill(text)
        self.page.locator("#ai-chat-send").click()
        self.page.locator("#ai-chat-send:not([disabled])").wait_for(timeout=6000)

    def test_open_close_enter_and_click_to_send(self):
        self.mock_chat()
        self.open()
        self.page.locator("#ai-chat-input").fill("Годинник до 1800")
        self.page.locator("#ai-chat-input").press("Enter")
        self.page.get_by_text("Перевірена відповідь").wait_for()
        self.assertEqual(self.requests[0]["message"],"Годинник до 1800")
        self.assertEqual(self.requests[0]["history"],[])
        self.assertIn("product_ids", self.page.evaluate("JSON.parse(localStorage.getItem('zorro_ai_v4_state'))"))
        self.page.locator("#ai-chat-close").click()
        self.assertFalse(self.page.locator("#ai-chat-box").is_visible())

    def test_second_turn_keeps_history_and_state(self):
        self.mock_chat()
        self.open()
        self.send("Чоловічий до 1800")
        self.send("А без шкіри?")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[1]["state"]["filters"]["max_price"],1800)
        roles=[turn["role"] for turn in self.requests[1]["history"]]
        self.assertEqual(roles,["user","assistant"])

    def test_reload_and_clear_chat_history(self):
        self.mock_chat()
        self.open()
        self.send("До 1800")
        self.page.reload()
        self.page.locator("#ai-chat-toggle").click()
        self.assertTrue(self.page.get_by_text("До 1800",exact=True).first.is_visible())
        self.assertTrue(self.page.get_by_text("Перевірена відповідь").first.is_visible())
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator("#ai-chat-reset").click()
        self.assertEqual(self.page.evaluate("JSON.parse(localStorage.getItem('zorro_ai_v4_turns'))"),[])
        self.assertFalse(self.page.get_by_text("До 1800",exact=True).is_visible())

    def test_response_html_injection_is_stripped_and_real_cards_still_render(self):
        html = ('<p>Ось годинник</p><script>window.__attacked=1</script>'
                '<img src="https://evil.example/pixel" onerror="window.__attacked=2">'
                '<svg onload="window.__attacked=3"><circle/></svg>'
                '<a href="javascript:alert(1)" id="badlink">Небезпечна</a>'
                '<a href="https://evil.example/watches" id="phish">Купити дешево</a>'
                '<a href="https://zorro.com.ua/watch/">Купити у Zorro</a>'
                '<img src="https://zorro.com.ua/watch.jpg" alt="годинник">'
                '<button class="ai-chip" data-msg="Смарт годинник">Пошук</button>'
                '<div style="background-image:url(https://evil.example/a)">Опис</div>')
        self.mock_chat(reply=html)
        self.open()
        self.send("Мені годинник")
        message=self.page.locator("#ai-chat-messages")
        self.assertEqual(self.page.evaluate("window.__attacked || 0"),0)
        self.assertEqual(message.locator("script,svg,iframe").count(),0)
        self.assertEqual(message.locator('a[href^="javascript:"]').count(),0)
        self.assertEqual(message.locator('a[href*="evil.example"]').count(),0)
        self.assertEqual(message.locator('img[src*="evil.example"]').count(),0)
        self.assertEqual(message.locator('a[href^="https://zorro.com.ua/"]').count(),1)
        self.assertEqual(message.locator('img[src^="https://zorro.com.ua/"]').count(),1)
        self.assertEqual(message.locator('[style*="evil.example"]').count(),0)
        self.assertEqual(message.locator('button.ai-chip[data-msg="Смарт годинник"]').count(),1)

    def test_malicious_saved_local_storage_does_not_execute(self):
        self.open()
        self.page.evaluate("""() => {
            localStorage.setItem('zorro_ai_v4_html',
                '<img src=x onerror="window.__attacked=1"><svg onload="window.__attacked=2"></svg>'
                + '<p>Збережений текст</p><a href="javascript:alert(1)">Фішинг</a>');
        }""")
        self.page.reload()
        self.page.locator("#ai-chat-toggle").click()
        self.assertTrue(self.page.get_by_text("Збережений текст").is_visible())
        self.assertEqual(self.page.evaluate("window.__attacked || 0"),0)
        self.assertEqual(self.page.locator("#ai-chat-messages svg").count(),0)
        self.assertEqual(self.page.locator("#ai-chat-messages img").count(),0)
        self.assertEqual(self.page.locator('#ai-chat-messages a').count(),0)

    def test_callback_failure_and_untrusted_status_text(self):
        def failed(route):
            headers={"Access-Control-Allow-Origin":"*",
                     "Access-Control-Allow-Headers":"Content-Type",
                     "Access-Control-Allow-Methods":"OPTIONS,POST"}
            if route.request.method == "OPTIONS":
                route.fulfill(status=204,headers=headers)
                return
            self.requests.append(route.request.post_data_json)
            route.fulfill(status=200, content_type="application/json",headers=headers,
                          body=json.dumps({
                "status":"error",
                "reply":'<img src="x" onerror="window.__attacked=1">Заявку не передано'
            }))
        self.page.route("https://api.zorro.test/api/callback",failed)
        self.open()
        self.page.locator('[data-action="call-modal"]').first.click()
        self.page.locator("#ai-callback-phone").fill("0981234567")
        self.page.locator("#ai-callback-submit").click()
        self.page.get_by_text("Заявку не передано", exact=False).wait_for()
        self.assertEqual(self.page.evaluate("window.__attacked || 0"),0)
        self.assertTrue(self.page.locator("#ai-callback-panel").is_visible())
        self.assertEqual(self.requests[0]["phone"],"0981234567")

    def test_mobile_viewport_chat_fits_screen(self):
        self.page.set_viewport_size({"width":375,"height":780})
        self.open()
        bounds=self.page.locator("#ai-chat-box").bounding_box()
        self.assertGreaterEqual(bounds["x"],0)
        self.assertLessEqual(bounds["x"]+bounds["width"],375.5)
        self.assertLessEqual(bounds["height"],780)
        self.assertTrue(self.page.locator("#ai-chat-input").is_visible())

    def test_product_context_requires_detail_page_not_category_listing(self):
        self.open()
        self.assertEqual(self.page.get_by_text("Товар, який ви переглядаєте:").count(),0)
        # A category can have an item id without being a product detail page.
        self.page.evaluate("""() => {
            document.body.insertAdjacentHTML('afterbegin',
              '<h1>Каталог чоловічих годинників</h1><div data-product-id="5"></div>');
        }""")
        self.page.reload()
        self.page.locator("#ai-chat-toggle").click()
        self.assertEqual(self.page.get_by_text("Товар, який ви переглядаєте:").count(),0)

    def test_backend_missing_does_not_contact_production(self):
        self.page.close()
        self.page=self.browser.new_page()
        self.page.goto(self.base + "/zorro_widget_v4.html")
        self.page.locator("#ai-chat-toggle").click()
        self.page.locator("#ai-chat-input").fill("Привіт")
        self.page.locator("#ai-chat-send").click()
        self.page.locator("#ai-chat-send:not([disabled])").wait_for(timeout=6000)
        content = self.page.locator("#ai-chat-messages").inner_text()
        self.assertIn("Зараз немає зв", content, content)
        self.assertFalse(self.page.evaluate("!!window.ZORRO_AI_API_BASE"))


if __name__=="__main__":
    unittest.main()
