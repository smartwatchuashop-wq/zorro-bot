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

    def test_user_text_not_inserted_as_html(self):
        self.assertIn("span.textContent = text",JS)
        self.assertNotIn("userMsgDiv.innerHTML",JS)

if __name__ == "__main__":
    unittest.main()
