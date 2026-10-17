"""Static widget/API contract tests; no browser, DB, or external calls."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = Path(__file__).with_name("zorro_widget_v4.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>\s*(.*?)\s*</script>", HTML, re.S)
assert SCRIPT, "Widget must contain a JavaScript script section"
JS = SCRIPT.group(1)

class WidgetContractTests(unittest.TestCase):
    def test_syntax_with_node(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        result=subprocess.run(["node","--check","-"],input=JS,text=True,capture_output=True,check=False)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_api_receives_history_and_state(self):
        self.assertIn("history:priorHistory",JS)
        self.assertIn("state:dialogueState",JS)
        self.assertIn("message:text",JS)

    def test_previous_history_captured_before_message(self):
        body = JS.split("function sendTextMessage(text)",1)[1]
        self.assertLess(body.index("var priorHistory = chatTurns.slice(-12)"),body.index("addUserText(text);"))
        self.assertIn("chatTurns.push({role:'user', content:text})",JS)

    def test_state_persisted_and_reset(self):
        self.assertIn("STORAGE_KEY_STATE",JS)
        self.assertIn("STORAGE_KEY_TURNS",JS)
        self.assertIn("localStorage.removeItem(STORAGE_KEY_STATE)",JS)
        self.assertIn("localStorage.removeItem(STORAGE_KEY_TURNS)",JS)

    def test_existing_widget_actions_and_callback_retained(self):
        for value in ("ai-chat-toggle","ai-callback-panel","ai-card-action",
                      "'/api/callback'","'call-modal'"):
            self.assertIn(value,HTML)

    def test_duplicate_submissions_blocked(self):
        self.assertIn("if (!text || busy) return",JS)
        self.assertIn("setBusy(true)",JS)
        self.assertIn("setBusy(false)",JS)

    def test_untrusted_reply_and_saved_history_are_sanitized(self):
        self.assertIn("function safeMarkup(raw)", JS)
        self.assertIn("div.appendChild(safeMarkup(text))", JS)
        self.assertIn("safeMarkup(savedHtml && savedHtml.trim()", JS)
        self.assertNotIn("div.innerHTML = String(text || '')", JS)
        self.assertNotIn("messagesContainer.innerHTML = savedHtml", JS)
        self.assertIn("blocked[tag]", JS)
        for dangerous in ("SCRIPT:1", "IFRAME:1", "SVG:1", "FORM:1", "OBJECT:1"):
            self.assertIn(dangerous, JS)
        self.assertIn("node.style.getPropertyValue(prop)", JS)

    def test_product_purchase_urls_allow_only_store_https(self):
        self.assertIn("function safeLink(raw, image)", JS)
        self.assertIn("url.protocol !== 'https:'", JS)
        self.assertIn("zorro", JS)
        self.assertIn("url.username || url.password", JS)
        self.assertIn("if (!href) return document.createTextNode", JS)
        self.assertIn("if (!src) return null", JS)

    def test_callback_messages_never_interpreted_as_html(self):
        self.assertIn("function addBotText(text, error, plainText)", JS)
        self.assertIn("if (error || plainText)", JS)
        self.assertIn("!(data && data.status === 'success'), true", JS)

    def test_widget_never_defaults_to_current_production_backend(self):
        self.assertIn("window.ZORRO_AI_API_BASE || ''", JS)
        self.assertNotIn("window.ZORRO_AI_API_BASE || 'https://fastapi-qie2.onrender.com'", JS)
        self.assertIn("Zorro AI backend URL is not configured", JS)

    def test_category_page_not_mistaken_for_a_product(self):
        self.assertIn("var productSchema =", JS)
        self.assertIn("var productArea =", JS)
        self.assertIn("if (!h1 || !productArea && !productSchema) return null", JS)

    def test_user_text_not_inserted_as_html(self):
        self.assertIn("span.textContent = text",JS)
        self.assertNotIn("userMsgDiv.innerHTML",JS)

if __name__ == "__main__":
    unittest.main()
