"""Verified PostgreSQL retrieval for Zorro. Staging module; not wired to production yet."""
from __future__ import annotations
import re
from typing import Any
from psycopg2.extras import RealDictCursor

# Never remove mandatory restrictions to fill three recommendation slots.
# Pass only an explicitly authorized, bounded set of request filters here.
VALID_KEYS = {"max_price", "min_price", "brand", "category", "include", "exclude",
              "must_have", "sku", "audience", "watch_type"}
STOPWORDS = {"годинник", "годинники", "хочу", "покажи", "для", "мені", "які", "моделі"}

def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()

def _attr_text(product: dict) -> str:
    attrs = product.get("attributes") or {}
    return " ".join(f"{k} {v}" for k, v in attrs.items() if v is not None).casefold()

def _term_match(text: str, token: str) -> bool:
    return normalize(token) in text

NEGATIVE_VALUES = {"ні", "нет", "немає", "отсутствует", "відсутня", "відсутній", "не підтримується", "не підтримує", "none", "false", "0", "-"}

def _field_match(attrs: dict, field: str, wanted: str) -> bool:
    value = normalize(attrs.get(field, ""))
    expected = normalize(wanted)
    if not value or not expected or value in NEGATIVE_VALUES:
        return False
    if expected in ("так", "є", "yes", "true"):
        return value in ("так", "є", "yes", "true", "підтримується", "присутня", "присутній")
    if expected in ("ні", "немає", "no", "false"):
        return False  # Negative presence cannot fulfill a must-have constraint.
    # Do not treat a negated feature ("без шкіри", "не підтримує дзвінки")
    # as confirmation of the positive term inside it.
    if value.startswith(("без ", "не ", "without ", "no ", "немає ", "відсутн")):
        return False
    return expected == value or bool(re.search(r"(?<!\w)" + re.escape(expected) + r"(?!\w)", value))

def search_products(conn, filters: dict, limit: int = 5) -> list[dict]:
    """SQL first: live inventory, price, vendor/category; then exact attribute rules and ranking.

    filters example:
      {max_price:2000, category:"Чоловічі годинники",
       must_have:{"Матеріал ремінця":"Сталевий"},
       exclude:{"Матеріал ремінця":["Шкіряний"]},
       include:["класичний"]}
    Missing attribute never counts as a positive match.
    """
    unknown = set(filters) - VALID_KEYS
    if unknown:
        raise ValueError(f"Unknown filters: {sorted(unknown)}")
    limit = max(1, min(20, int(limit)))
    clauses = ["available = TRUE", "price IS NOT NULL", "price > 0", "url IS NOT NULL", "url <> ''"]
    args = []
    if filters.get("max_price") is not None:
        clauses.append("price <= %s")
        args.append(filters["max_price"])
    if filters.get("min_price") is not None:
        clauses.append("price >= %s")
        args.append(filters["min_price"])
    if filters.get("brand"):
        clauses.append("LOWER(vendor) = LOWER(%s)")
        args.append(filters["brand"])
    if filters.get("category"):
        clauses.append("LOWER(category) LIKE LOWER(%s)")
        args.append("%" + str(filters["category"]).strip() + "%")
    if filters.get("sku"):
        clauses.append("LOWER(sku) = LOWER(%s)")
        args.append(filters["sku"])
    # Pull the bounded live catalog, not the top-priced 80 items.
    sql = ("SELECT site_id, sku, name, vendor, category, price, available, "
           "url, image, description, attributes "
           "FROM zorro_products_v2 WHERE " + " AND ".join(clauses) + " ORDER BY site_id LIMIT 5000")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, args)
        candidates = [dict(row) for row in cur.fetchall()]

    must = filters.get("must_have") or {}
    exclude = filters.get("exclude") or {}
    if not isinstance(must, dict) or not isinstance(exclude, dict):
        raise ValueError("Attribute filters must be objects")
    if any(not isinstance(v, str) for v in must.values()):
        raise ValueError("Required attribute values must be strings")
    for key, vals in exclude.items():
        if not isinstance(vals, list) or not vals or any(not isinstance(v, str) for v in vals):
            raise ValueError(f"Exclusion values for {key} must be a list of strings")
    if not isinstance(filters.get("include") or [], list):
        raise ValueError("include must be an array")
    include = [normalize(t) for t in (filters.get("include") or []) if isinstance(t, str) and normalize(t)]
    ranked = []
    for p in candidates:
        attrs = p.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        audience = normalize(filters.get("audience"))
        corpus = normalize(p.get("category")) + " " + normalize(p.get("name")) + " " + _attr_text(p)
        if audience == "men" and not any(x in corpus for x in ("чоловіч", "чоловік", "мужск")):
            continue
        if audience == "women" and not any(x in corpus for x in ("жіноч", "жінок", "женск")):
            continue
        if audience == "kids" and not any(x in corpus for x in ("дитяч", "діт", "детск")):
            continue
        typ = normalize(filters.get("watch_type"))
        # Explicit quartz/mechanical queries must not be satisfied by a smart
        # watch that merely mentions a quartz/movement term in its description.
        product_type = normalize(p.get("category")) + " " + normalize(p.get("name"))
        is_smart = any(x in product_type for x in ("смарт", "smart", "розумн"))
        if typ == "smart" and not is_smart:
            continue
        if typ in ("quartz", "mechanical"):
            if is_smart:
                continue
            aliases = ("кварц", "quartz") if typ == "quartz" else ("механ", "автозавод", "mechan")
            if not any(x in corpus for x in aliases):
                continue
        # exact field matching is mandatory, missing attributes are rejected.
        if any(not _field_match(attrs, k, v) for k, v in must.items()):
            continue
        # Unknown material must not count as confirmed "without leather".
        if any(not normalize(attrs.get(k)) or any(_field_match(attrs, k, v) for v in vals)
               for k, vals in exclude.items()):
            continue
        searchable = " ".join([normalize(p.get("name")), normalize(p.get("vendor")),
                               normalize(p.get("category")), _attr_text(p)])
        score = sum(8 for t in include if _term_match(normalize(p.get("name")), t))
        score += sum(4 for t in include if _term_match(_attr_text(p), t))
        score += sum(1 for t in include if _term_match(searchable, t))
        # if the user specified positive search concepts, require some supporting evidence
        if include and score == 0:
            continue
        ranked.append((score, p))
    ranked.sort(key=lambda item: (-item[0], float(item[1]["price"]), normalize(item[1]["name"])))
    result, seen = [], set()
    for _, p in ranked:
        # Avoid duplicate variants on the initial recommendation list.
        key = (normalize(p["vendor"]), normalize(p["name"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
        if len(result) >= limit:
            break
    return result

def compare_by_ids(conn, ids: list[str]) -> list[dict]:
    """Use EXACT previously shown products; never launch a new ranking search."""
    if not ids or len(ids) > 20:
        return []
    ids = [str(x) for x in ids]
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT site_id, sku, name, vendor, category, price, available, url, "
            "image, description, attributes FROM zorro_products_v2 "
            "WHERE site_id = ANY(%s)", (ids,))
        items = {str(r["site_id"]): dict(r) for r in cur.fetchall() if r.get("available") and r.get("url") and r.get("price") is not None}
    return [items[str(x)] for x in ids if str(x) in items]
