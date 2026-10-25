"""Safe checkout intent and verified product links for Zorro AI.

No database writes, checkout API calls, or orders are made here.
All purchase links come from the product catalog and must point to Zorro.
"""
import re
from urllib.parse import urlsplit


_ORDER_START = re.compile(
    r"^\s*(?:"
    r"оформити(?:\s+(?:замовлення|покупку))?\b|"
    r"хочу\s+(?:оформити|замовити|купити)\b|"
    r"беру\b|купую\b|замовляю\b|"
    r"як\s+(?:оформити(?:\s+замовлення)?|замовити|купити)\b|"
    r"де\s+(?:оформити|замовити|купити)\b"
    r")",
    re.IGNORECASE,
)


def explicit_checkout_intent(message: str) -> bool:
    """Catch short, explicit buy commands even if the model calls them store_info."""
    return bool(_ORDER_START.match(str(message or "")[:220]))


def trusted_product_url(url: str) -> str:
    """Never send buyers to a supplier, untrusted domain, or unsafe URL."""
    raw = str(url or "").strip()
    if not raw or len(raw) > 2048 or any(ord(c) < 32 for c in raw):
        return ""
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or parsed.hostname not in
                {"zorro.com.ua", "www.zorro.com.ua"} or
                parsed.username or parsed.password or parsed.port not in (None, 443)):
            return ""
    except ValueError:
        return ""
    return raw


def resolve_checkout_product(shown: list[dict], plan: dict, message: str,
                             history: list[dict]) -> dict | None:
    """Never choose one arbitrarily when multiple models are being considered."""
    by_id = {str(p.get("site_id")): p for p in shown}
    explicit = []
    positions = plan.get("positions")
    for pos in (positions if isinstance(positions, list) else [])[:5]:
        if type(pos) is int and 1 <= pos <= len(shown):
            explicit.append(shown[pos - 1])
    ids = plan.get("ids")
    for pid in (ids if isinstance(ids, list) else [])[:5]:
        if str(pid) in by_id:
            explicit.append(by_id[str(pid)])
    explicit += [p for p in shown
                 if len(str(p.get("name") or "")) >= 5 and
                 str(p["name"]).casefold() in message.casefold()]
    distinct = list({str(p["site_id"]): p for p in explicit}.values())
    if len(distinct) == 1:
        return distinct[0]
    if len(distinct) > 1:
        return None
    if len(shown) == 1:
        return shown[0]
    # After a description/recommendation, the last assistant message often names
    # exactly one model. It is safe to use that model if none other is mentioned.
    latest = next((h.get("content", "") for h in reversed(history)
                   if h.get("role") == "assistant"), "")
    named = [p for p in shown if len(str(p.get("name") or "")) >= 5 and
             str(p["name"]).casefold() in latest.casefold()]
    if len(named) == 1:
        return named[0]
    return None


def verified_product_links(reply: str, products: list[dict]) -> list[dict]:
    """Only return links for actual catalog product names present in the reply."""
    seen = set()
    result = []
    for p in products:
        name = str(p.get("name") or "")
        url = trusted_product_url(p.get("url") or "")
        pid = str(p.get("site_id") or "")
        if (len(name) < 5 or not url or not pid or
                name.casefold() not in str(reply).casefold() or pid in seen):
            continue
        seen.add(pid)
        result.append({"id": pid, "name": name, "url": url})
    return sorted(result, key=lambda item: -len(item["name"]))
