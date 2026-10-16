"""Opt-in, read-only real Gemini smoke checks for Zorro v4 staging.

Never run on main/production. Run only when explicitly enabled on the isolated
staging service and turn the flag off immediately after evaluating results.
The smoke logs catalog IDs and short model replies; never secret values.
"""
import logging
import re
from html import unescape

from psycopg2.extras import RealDictCursor

LOG = logging.getLogger("zorro.staging.smoke")


def run_smoke() -> None:
    import main
    import conversation_v4 as conversation
    from search_v4 import _field_match, _material_kinds

    intent = conversation.ai_json("Порадь чоловічий годинник до 2000 грн", [])
    if not isinstance(intent, dict) or intent.get("action") != "search":
        raise RuntimeError("Gemini intent request failed or returned the wrong action")
    LOG.warning("GEMINI_SMOKE intent_ok=True action=search")

    def ask(message, state=None, history=None):
        data = main.ChatInput(message=message, state=state or {}, history=history or [])
        result = conversation.handle_chat(data, main.db, main.product_html, main.format_ai_text)
        ids = [str(item["id"]) for item in result.get("products", [])]
        if not ids:
            raise RuntimeError("No verified products returned for: " + message)
        reply = result.get("reply") or ""
        if "Ось моделі з підтвердженими характеристиками." in reply:
            raise RuntimeError("Gemini sales answer failed: deterministic fallback returned")
        if len(reply) < 25:
            raise RuntimeError("Gemini response is unexpectedly short")
        conn = main.db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT site_id, name, category, price, available, url, attributes "
                    "FROM zorro_products_v2 WHERE site_id=ANY(%s)", (ids,)
                )
                rows = {str(p["site_id"]): dict(p) for p in cur.fetchall()}
        finally:
            conn.close()
        if len(rows) != len(set(ids)):
            raise RuntimeError("Some returned catalog IDs were not verified")
        for pid in ids:
            p = rows[pid]
            if not p["available"] or not p["url"] or not p["price"] or float(p["price"]) > 2000:
                raise RuntimeError("Bad availability/URL/price for model ID " + pid)
            if "годинник" not in p["category"].casefold():
                raise RuntimeError("Non-watch product ID " + pid)
        text = unescape(re.sub(r"<[^>]*>", " ", reply))
        text = " ".join(text.split())
        LOG.warning("GEMINI_SMOKE stage=%s ids=%s text=%s", len(history or []) + 1,
                    ",".join(ids), text[:360])
        return result, rows

    first_msg = "Порадь чоловічий годинник до 2000 грн"
    first, _ = ask(first_msg)
    follow, follow_rows = ask(
        "А без шкіряного ремінця?",
        first["state"],
        [main.Msg(role="user", content=first_msg)],
    )
    if not follow["state"].get("filters", {}).get("max_price") == 2000:
        raise RuntimeError("Gemini follow-up lost initial budget")
    for p in follow_rows.values():
        material = (p.get("attributes") or {}).get("Матеріал ремінця")
        if not material or "leather" in _material_kinds(material):
            raise RuntimeError("Leather or unknown strap was returned: " + str(p["site_id"]))
    LOG.warning("GEMINI_SMOKE leather_exclusion_ok=True budget_retained=True")

    smart, smart_rows = ask("Покажи смартгодинник до 2000 грн, щоб можна було розмовляти")
    for p in smart_rows.values():
        if "смарт" not in p["category"].casefold() or not _field_match(
            p.get("attributes") or {}, "Можливість розмовляти", "Так"
        ):
            raise RuntimeError("Missing verified Bluetooth call ability for " + str(p["site_id"]))
    LOG.warning("GEMINI_SMOKE smart_calling_ok=True")
    LOG.warning("GEMINI_SMOKE RESULT=PASS; Gemini intent, answers, live PostgreSQL, follow-up and calling verified")
