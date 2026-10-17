"""One-shot 5-scenario Gemini + existing read-only Zorro database acceptance suite.

Run ONLY on the dedicated staging server, with an explicit opt-in env flag.
This never writes to PostgreSQL and never sends customer notifications.
Previously confirmed live cases cover first-round search, multi-turn leather
negation, calling, comparison, and description. These are five additional cases.
"""
import logging
from psycopg2.extras import RealDictCursor

LOG = logging.getLogger("zorro.acceptance")


def run_acceptance():
    import main
    from conversation_v4 import handle_chat
    from search_v4 import _field_match

    scenarios = [
        ("women", "Порадь жіночий годинник до 2000 грн", "Жіночі годинники", 2000),
        ("kids", "Покажи дитячий годинник до 1500 грн", "Дитячі годинники", 1500),
        ("mechanical", "Покажи чоловічі механічні годинники до 2000 грн",
         "Чоловічі годинники", 2000),
        ("steel_skmei", "Покажи чоловічий годинник Skmei зі сталевим ремінцем до 2000 грн",
         "Чоловічі годинники", 2000),
        ("no_stock", "Знайди смартгодинник до 500 грн із можливістю розмовляти",
         "Смарт годинники", 500),
    ]
    failures=[]
    for label, message, category, max_price in scenarios:
        try:
            request=main.ChatInput(message=message,history=[],state={})
            result=handle_chat(request,main.db,main.product_html,main.format_ai_text)
            product_ids=[str(item["id"]) for item in result.get("products",[])]
            state=result.get("state") or {}
            budget=(state.get("filters") or {}).get("max_price")
            if budget != max_price:
                raise AssertionError("Buyer budget lost: "+str(budget))
            if label=="no_stock":
                if product_ids:
                    raise AssertionError("Out-of-budget smart watch was invented")
                LOG.warning("ACCEPTANCE %s PASS no matching watches returned",label)
                continue
            if not product_ids:
                raise AssertionError("No models despite known available stock: "+str(result.get("reply",""))[:140])
            conn=main.db()
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT site_id, sku, category, name, price, available, url, "
                        "vendor, attributes FROM zorro_products_v2 WHERE site_id=ANY(%s)",
                        (product_ids,),
                    )
                    verified={str(p["site_id"]):dict(p) for p in cur.fetchall()}
            finally:
                conn.close()
            if len(verified)!=len(set(product_ids)):
                raise AssertionError("Unverified product IDs")
            for pid in product_ids:
                p=verified[pid]
                if (p["category"] != category or not p["available"]
                        or not p["url"] or not (0<float(p["price"])<=max_price)):
                    raise AssertionError("Buyer requirements not met for "+pid)
                if label=="mechanical" and not _field_match(
                        p.get("attributes") or {}, "Тип механізму", "Механічний"):
                    raise AssertionError("Non-mechanical product: "+pid)
                if label=="steel_skmei":
                    if (p["vendor"].casefold()!="skmei"
                            or not _field_match(p.get("attributes") or {},
                                                "Матеріал ремінця","Сталевий")):
                        raise AssertionError("Brand or steel strap not confirmed: "+pid)
            LOG.warning("ACCEPTANCE %s PASS count=%s ids=%s",
                        label,len(product_ids),",".join(product_ids))
        except Exception as exc:
            LOG.error("ACCEPTANCE %s FAIL %s",label,str(exc)[:300])
            failures.append(label)
    if failures:
        raise RuntimeError("Live Gemini acceptance failed scenarios: "+",".join(failures))
    LOG.warning("ACCEPTANCE RESULT=PASS 5/5 additional real Gemini scenarios verified")
