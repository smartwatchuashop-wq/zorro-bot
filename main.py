"""Zorro AI: real catalog, Gemini function calling, controlled conversation state.

Public API for the existing Horoshop/GTM widget. Never writes to PostgreSQL.
Do not put Gemini or DB credentials in the browser or GitHub.
"""
import json
import logging
import os
import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

import psycopg2
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field
from hybrid_search import ranked_ids, reciprocal_rank_fusion, index_status
from grounded_answer import natural_reply
from buyer_profile import clean_profile, merge_profile, retrieval_context, dislikes_product

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("zorro.ai")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
DB_URL = os.getenv("DATABASE_URL", "")
KEY = os.getenv("GEMINI_API_KEY", "")

app = FastAPI(title="Zorro AI Consultant", version="6.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

CATEGORIES = {
    "men": "Чоловічі годинники", "women": "Жіночі годинники",
    "kids": "Дитячі годинники", "smart": "Смарт годинники",
    "sport": "Спортивні годинники", "unisex": "Унісекс годинники",
}
CATEGORY_WORDS = {
    "men": r"чоловіч|чоловік|мужск", "women": r"жіноч|жінк|женск",
    "kids": r"дитяч|дитин|дітей|дітям|діток|дитині|детск|ребен",
    "smart": r"смарт|smart|розумн\w* годин",
    "sport": r"спортивн|sport", "unisex": r"унісекс|unisex",
}
MATERIAL = {
    "steel": r"сталев|сталь|стальн|нержавіюч|stainless|\bsteel\b",
    "leather": r"шкір|кожан|leather|замш", "silicone": r"силікон|силикон|silicon",
    "rubber": r"каучук|гумов|резин|rubber", "fabric": r"тканев|тканин|текстил|нейлон|nylon",
    "titanium": r"титан|titanium", "ceramic": r"керамік|ceramic",
    "metal": r"метал|metal", "plastic": r"пластик|plastic",
}
NEGATIVE = {"ні", "нет", "немає", "відсутнє", "відсутній", "відсутня", "false", "0", "-"}
STOP = {"годинник", "годинники", "годинника", "хочу", "покажи", "порадь", "для",
        "мені", "які", "який", "або", "потрібен", "потрібні", "шукаю", "купити", "будь", "ласка",
        "грн", "чоловічі", "жіночі", "дитячі", "смартгодинники", "смарт", "годинників", "з", "до", "від"}


class Msg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[Msg] = Field(default_factory=list)
    state: dict = Field(default_factory=dict)
    current_product: str | None = None


class CallbackIn(BaseModel):
    phone: str
    message: str = ""


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def words(value: Any) -> set[str]:
    return {x for x in re.findall(r"[\wа-яіїєґ]+", norm(value)) if len(x) > 2 and not x.isdigit() and x not in STOP}


def category_from_text(text: str) -> str | None:
    s = norm(text)
    for key in ("kids", "women", "men", "smart", "sport", "unisex"):
        if re.search(CATEGORY_WORDS[key], s):
            return CATEGORIES[key]
    return None


def canonical_category(value: Any) -> str | None:
    s = norm(value)
    if not s:
        return None
    for cat in CATEGORIES.values():
        if s == norm(cat):
            return cat
    return category_from_text(s)


def db_connection():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL not configured")
    connection = psycopg2.connect(DB_URL, connect_timeout=10)
    try:
        connection.set_session(readonly=True, autocommit=True)
    except Exception:
        connection.close()
        raise
    return connection


def load_catalog(filters: dict) -> list[dict]:
    """Full eligible catalog (not first 80): mandatory constraints in SQL."""
    sql = ["available = TRUE", "price > 0", "url IS NOT NULL", "url <> ''", "category = ANY(%s)"]
    params: list[Any] = [list(CATEGORIES.values())]
    for field, expression in (("max_price", "price <= %s"), ("min_price", "price >= %s")):
        if filters.get(field) is not None:
            sql.append(expression)
            params.append(filters[field])
    if filters.get("category"):
        sql.append("category = %s")
        params.append(filters["category"])
    if filters.get("brand"):
        sql.append("LOWER(vendor) = LOWER(%s)")
        params.append(filters["brand"])
    if filters.get("sku"):
        sql.append("LOWER(sku) = LOWER(%s)")
        params.append(filters["sku"])
    connection = db_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT site_id,sku,name,vendor,category,price,available,url,image,description,attributes "
                        "FROM zorro_products_v2 WHERE " + " AND ".join(sql) + " ORDER BY site_id", params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        connection.close()


def material_kind(raw: Any) -> set[str]:
    return {k for k, pattern in MATERIAL.items() if re.search(pattern, norm(raw))}


def attr_value(attrs: dict, *names: str) -> str:
    for name in names:
        if attrs.get(name) is not None:
            return str(attrs[name])
    return ""


def passes_hard_filters(p: dict, f: dict) -> bool:
    attrs = p.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    strap = norm(attr_value(attrs, "Матеріал ремінця", "Матеріал браслету"))
    if f.get("strap"):
        wanted = f["strap"]
        if wanted == "metal":
            if not (material_kind(strap) & {"steel", "titanium", "metal"}):
                return False
        elif wanted not in material_kind(strap):
            return False
    if f.get("exclude_leather"):
        # Unknown is NOT proof of a non-leather strap.
        if not strap or material_kind(strap) & {"leather"}:
            return False
    if f.get("calling") is True:
        if norm(attr_value(attrs, "Можливість розмовляти")) not in {"так", "є", "yes", "true"}:
            return False
    if f.get("calling") is False:
        if norm(attr_value(attrs, "Можливість розмовляти")) not in NEGATIVE:
            return False
    if f.get("mechanism"):
        mechanism = norm(attr_value(attrs, "Тип механізму", "Механізм"))
        if f["mechanism"] == "mechanical" and not re.search(r"механ|автозавод|automatic", mechanism):
            return False
        if f["mechanism"] == "quartz" and not re.search(r"кварц|quartz", mechanism):
            return False
    if f.get("sim") is not None:
        sim_fields = [norm(v) for k, v in attrs.items()
                      if re.search(r"\bsim\b|сім.кар|сим.кар|слот.*карт", norm(k))]
        if not sim_fields:
            return False  # Unknown SIM status cannot satisfy a strict condition.
        has_sim = any(re.search(r"\bтак\b|\byes\b|\bє\b|nano.sim|micro.sim", v)
                      for v in sim_fields if v not in NEGATIVE)
        no_sim = all(v in NEGATIVE or re.search(r"немає|не підтрим|відсут", v) for v in sim_fields)
        if f["sim"] is True and not has_sim:
            return False
        if f["sim"] is False and not no_sim:
            return False
    return True


def score_product(p: dict, query: str, f: dict) -> float:
    attrs = p.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    title = norm(p.get("name"))
    details = norm(" ".join(str(v) for v in attrs.values()))
    corpus = norm(" ".join([title, p.get("vendor") or "", p.get("category") or "", details,
                            (p.get("description") or "")[:500]]))
    query_words = words(query)
    title_tokens = words(title)
    full_tokens = words(corpus)
    score = 0.0
    for term in query_words:
        if term in title_tokens:
            score += 5.0
        elif term in full_tokens:
            score += 2.0
        elif any(SequenceMatcher(None, term, t).ratio() >= .83 for t in title_tokens if len(t) >= 4):
            score += 1.5
        elif term in corpus:
            score += 0.5
    for term in f.get("keywords") or []:
        if norm(term) in corpus:
            score += 2.0
    # Prefer price variety over blindly choosing three cheapest items.
    if f.get("max_price") and not query_words:
        target = float(f["max_price"]) * .7
        score -= abs(float(p["price"]) - target) / max(target, 1) * .25
    return score


def legacy_shortlist(filters: dict, query: str, count: int = 12) -> list[dict]:
    eligible = [p for p in load_catalog(filters) if passes_hard_filters(p, filters)]
    if not eligible:
        return []
    ranked = sorted(eligible, key=lambda p: (-score_product(p, query, filters), float(p["price"]), norm(p["name"])))
    # Select variety of prices from the *full* filtered catalog for broad queries.
    semantic_terms = words(query) - words(filters.get("brand"))
    broad = len(semantic_terms) <= 2 and not filters.get("sku") and not filters.get("keywords")
    if broad and len(ranked) >= count:
        price_sorted = sorted(ranked, key=lambda p: float(p["price"]))
        ratios = (0.12, .25, .4, .55, .7, .85, .94)
        spread = [price_sorted[round((len(price_sorted) - 1)*r)] for r in ratios]
        ranked = ranked[:3] + spread + ranked[3:]
    dedup, seen = [], set()
    for p in ranked:
        key = (norm(p.get("vendor")), norm(p.get("name")))
        if key in seen:
            continue
        dedup.append(p)
        seen.add(key)
        if len(dedup) >= count:
            break
    return dedup



def shortlist(filters: dict, query: str, count: int = 36) -> list[dict]:
    """Full-catalog hybrid: vector + PostgreSQL FTS + trigram, RRF fusion.

    All hard constraints are enforced again on live PostgreSQL products.
    If index is not configured, the old full-catalog lexical route remains
    operational (but the status endpoint clearly says hybrid is NOT ready).
    """
    try:
        branches = ranked_ids(query, filters, semantic=True)
        ids = reciprocal_rank_fusion(branches, limit=120)
        if not ids:
            # Broad/category-only searches need price and catalog coverage, too.
            return legacy_shortlist(filters, query, count)
        by_id = {str(p['site_id']): p for p in load_catalog(filters)}
        result = []
        seen = set()
        for site_id in ids:
            p = by_id.get(str(site_id))
            if p and passes_hard_filters(p, filters):
                dedup = (norm(p.get('vendor')), norm(p.get('name')))
                if dedup not in seen:
                    result.append(p)
                    seen.add(dedup)
            if len(result) >= count:
                break
        # Supplement vector/keyword hits when mandatory conditions trim candidates.
        if len(result) < count:
            for p in legacy_shortlist(filters, query, count):
                key = (norm(p.get('vendor')), norm(p.get('name')))
                if key not in seen:
                    result.append(p)
                    seen.add(key)
                if len(result) >= count:
                    break
        return result[:count]
    except Exception as ex:
        LOG.warning('Hybrid unavailable; using non-vector lexical fallback: %s', type(ex).__name__)
        return legacy_shortlist(filters, query, count)


def sales_reply(purpose: str, message: str, products: list[dict], filters: dict,
                history: list[dict], fallback, buyer_profile: dict | None = None) -> str:
    enriched = dict(filters)
    enriched["buyer_profile"] = clean_profile(buyer_profile)
    return natural_reply(gemini, model_parts, purpose, message, products, enriched,
                         history, fallback)

def get_ids(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    ids = [str(i) for i in ids[:10]]
    connection = db_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT site_id,sku,name,vendor,category,price,available,url,image,description,attributes "
                        "FROM zorro_products_v2 WHERE site_id = ANY(%s) AND available=TRUE AND price>0 "
                        "AND url<>'' AND category=ANY(%s)", (ids, list(CATEGORIES.values())))
            items = {str(p["site_id"]): dict(p) for p in cur.fetchall()}
        return [items[i] for i in ids if i in items]
    finally:
        connection.close()


def find_current_product(title: str | None) -> dict | None:
    if not title:
        return None
    connection = db_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT site_id,sku,name,vendor,category,price,available,url,image,description,attributes "
                        "FROM zorro_products_v2 WHERE LOWER(name)=LOWER(%s) "
                        "AND available=TRUE AND price>0 AND url<>'' LIMIT 1", (title[:220],))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        connection.close()


def tool_declarations() -> list[dict]:
    # The same generic preference update is available in *every* action.
    # An objection or new preference must not be lost just because Gemini
    # chose to compare/recommend rather than run a fresh product search.
    profile_delta = {"type": "OBJECT", "properties": {
        "reset": {"type": "BOOLEAN", "description": "True only for a completely new shopping goal"},
        "goal": {"type": "STRING", "description": "Buyer use case, e.g. hiking, gift, everyday work"},
        "reject_ids": {"type": "ARRAY", "items": {"type": "STRING"}, "description":
                       "Previous shown IDs that the buyer explicitly rejected, only from previous_shown_ids"},
        "changes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "feature": {"type": "STRING"},
            "status": {"type": "STRING", "enum": ["must", "prefer", "avoid", "indifferent", "remove"]}
        }, "required": ["feature", "status"]}}
    }}
    declarations = [

        {"name": "search_catalog", "description": "Start a new search or refine the existing watch search. Use this for all shopping and product-discovery requests. Set mode=new if the buyer switches to an unrelated category or asks for a fresh selection. mode=refine to keep the existing needs. Previous filters must NOT be copied into arguments unless explicitly changed in the latest message.",
         "parameters": {"type": "OBJECT", "properties": {
             "mode": {"type": "STRING", "enum": ["new", "refine"]},
             "category": {"type": "STRING", "description": "men,women,kids,smart,sport,unisex or leave absent"},
             "max_price": {"type": "INTEGER"}, "min_price": {"type": "INTEGER"},
             "brand": {"type": "STRING"}, "sku": {"type": "STRING"},
             "strap": {"type": "STRING", "description": "steel,leather,silicone,rubber,fabric,titanium,ceramic,metal,plastic"},
             "exclude_leather": {"type": "BOOLEAN"}, "calling": {"type": "BOOLEAN", "description": "True only if explicitly requested calling; False only if explicitly rejects calling"},
             "mechanism": {"type": "STRING", "description": "mechanical or quartz"},
             "sim": {"type": "BOOLEAN", "description": "True only for explicitly required physical SIM"},
             "keywords": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Up to 4 semantic synonyms/descriptions matching style or buyer purpose"},
             "search_query": {"type": "STRING", "description": "Short focused semantic search phrasing based strictly on the buyer's need; preserve purpose and style. For followups retain previous intent from current filters or recent dialogue."},
             "remove_filters": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Names of previous restrictions expressly canceled"}
         }, "required": ["mode"]}},
        {"name": "compare_watches", "description": "Compare previously recommended products. Use positions 1,2,3 or IDs in previous_shown_ids, never a fresh search.",
         "parameters": {"type": "OBJECT", "properties": {
             "positions": {"type": "ARRAY", "items": {"type": "INTEGER"}},
             "ids": {"type": "ARRAY", "items": {"type": "STRING"}}
         }}},
        {"name": "describe_watch", "description": "Describe one of the previously shown watches (by position or id) or the product opened on this page.",
         "parameters": {"type": "OBJECT", "properties": {
             "position": {"type": "INTEGER"}, "id": {"type": "STRING"}
         }}},
        {"name": "store_information", "description": "Questions about store delivery, warranties, returns, payment, opening hours, or contacts. Do not invent policies.",
         "parameters": {"type": "OBJECT", "properties": {"topic": {"type": "STRING"}}}
        },
        {"name": "ask_customer", "description": "Use for greeting, general help, or a very vague shopping wish. Ask ONE helpful clarification BEFORE searching thousands of watches. If buyer already gave category, budget, or specific requirements, use search_catalog instead.",
         "parameters": {"type": "OBJECT", "properties": {
             "question": {"type": "STRING", "description": "One brief warm Ukrainian clarification question about who, budget or intended use."},
             "reason": {"type": "STRING", "enum": ["greeting", "clarification"]}
         }, "required": ["question"]}},
        {"name": "recommend_shown", "description": "The buyer asks which of the previously suggested models YOU recommend, which to choose, or which is better for their particular needs. Recommend strictly among the previously shown IDs.",
         "parameters": {"type": "OBJECT", "properties": {"reason": {"type": "STRING"}}}}

    ]
    for declaration in declarations:
        params = declaration.get("parameters", {})
        params.setdefault("properties", {})["profile_update"] = profile_delta
        if declaration["name"] in ("compare_watches", "recommend_shown"):
            params["properties"]["also_search"] = {"type": "BOOLEAN", "description":
                "True only if the buyer also explicitly requests new alternatives; comparison and search may co-occur"}
    return [{"functionDeclarations": declarations}]


def gemini(contents: list[dict], system: str, tools: list[dict] | None = None, json_mode: bool = False) -> dict:
    if not KEY:
        raise RuntimeError("GEMINI_API_KEY missing")
    options = {"temperature": .12 if tools else .2, "maxOutputTokens": 900}
    if json_mode:
        options["responseMimeType"] = "application/json"
    payload = {"systemInstruction": {"parts": [{"text": system}]},
               "contents": contents, "generationConfig": options}
    if tools:
        payload["tools"] = tools
        payload["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames":
                            ["search_catalog", "compare_watches", "describe_watch", "store_information", "ask_customer", "recommend_shown"]}}
    resp = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                         headers={"x-goog-api-key": KEY}, json=payload, timeout=28)
    resp.raise_for_status()
    return resp.json()


def model_parts(answer: dict) -> list[dict]:
    return answer["candidates"][0]["content"]["parts"]


def select_action(question: str, history: list[dict], state: dict, current: str | None):
    system = ("You are the intent router for Zorro, a Ukrainian watch store. Call exactly ONE provided tool. "
              "New watch category or unrelated question -> search_catalog(mode=new), clear old calling/strap/etc. "
              "'Such same ones but for children' -> refine keeping relevant conditions and changing category. "
              "Keep existing filters for 'cheaper', 'without leather', 'instead steel' and other followups. "
              "Use compare_watches when comparing previously shown models; describe_watch for a particular model. "
              "Use ask_customer for greetings, vague initial requests or when one clarification will meaningfully improve a recommendation. "
              "Use recommend_shown when they ask which existing recommendation you would personally pick or which is best for them. "
              "Do not ask again for preferences that the shopper already supplied. "
              "Never invent inventory, prices, capabilities or store policies. "
              "Specify ONLY changes explicitly expressed in the latest user message; do not echo all old filters. "
              "For ALL tools, use profile_update.changes for ANY new or canceled customer preference. "
              "must=essential, prefer=desirable, avoid=customer rejects having feature, "
              "indifferent=feature not important (NOT the same as without!), remove=cancel previous instruction. "
              "For instance 'compass not needed' means indifferent, NOT avoid. 'without compass' means avoid. "
              "Do not invent updates without clear user evidence. Preserve prior customer shopping goal. "
              "If buyer explicitly rejects a previously shown product, add its ID to profile_update.reject_ids. "
              "If comparing and customer ALSO asks for other models set also_search=true. "
              "For general purpose/style requests, generate 1-4 descriptive synonyms as soft keywords, not false hard facts. "
              "Write search_query with actual customer purpose, not a bland product category. "
              "For 'without SIM but calling', set calling=true and sim=false not keywords 'SIM'. "
              "If the user cancels an earlier constraint, use remove_filters. "
              "Return a function call, never executable SQL.")
    payload = {"latest_message": question, "current_product_title": current,
               "current_filters": state.get("filters") or {}, "buyer_profile": state.get("buyer_profile") or {},
               "previous_shown_ids": state.get("product_ids") or [],
               "recent_dialogue": history[-8:]}
    answer = gemini([{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
                    system, tool_declarations())
    for part in model_parts(answer):
        if isinstance(part.get("functionCall"), dict):
            call = part["functionCall"]
            return call.get("name"), call.get("args") or {}, answer
    raise ValueError("Gemini returned no function call")


FILTER_FIELDS = {"category", "min_price", "max_price", "brand", "sku", "strap", "exclude_leather",
                 "calling", "mechanism", "sim", "keywords"}


def determine_category(query: str) -> str | None:
    return category_from_text(query)


def merge_filter_state(previous: dict, raw: dict, question: str) -> dict:
    text = norm(question)
    prior = {k: v for k, v in (previous or {}).items() if k in FILTER_FIELDS}
    explicit_category = determine_category(question)
    category_change = bool(explicit_category and prior.get("category") and explicit_category != prior["category"])
    preserve = bool(re.search(r"такі сам|такого ж|аналогічн|з такими ж|такий сам", text))
    reset = bool(re.search(r"новий пошук|почн\w* спочатку|забудь попередн", text)) or (category_change and not preserve)
    # A model misclassifying a clear follow-up must not discard preferences.
    clear_followup = bool(re.search(r"^(а |але |тепер |без |ще |дешевш|дорожч|замість )", text))
    if raw.get("mode") == "new" and not preserve and (not clear_followup or explicit_category):
        reset = True
    result = {} if reset else dict(prior)
    # A category switch to a new type resets stale hard requirements even if Gemini mislabels mode.
    patch = {k: v for k, v in raw.items() if k in FILTER_FIELDS}
    if explicit_category:
        patch["category"] = explicit_category
    for k in raw.get("remove_filters", []) if isinstance(raw.get("remove_filters"), list) else []:
        if k in FILTER_FIELDS:
            result.pop(k, None)
    if "без обмеження бюджету" in text or "будь яка ціна" in text:
        result.pop("max_price", None)
        result.pop("min_price", None)
        patch.pop("max_price", None)
        patch.pop("min_price", None)
    for k, v in patch.items():
        if k in ("max_price", "min_price"):
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v < 0 or v > 10000000:
                continue
        if k == "category":
            v = CATEGORIES.get(norm(v)) or canonical_category(v)
            if not v:
                continue
        if k == "strap":
            v = norm(v)
            if v not in MATERIAL:
                v = next((key for key, pattern in MATERIAL.items() if re.search(pattern, v)), None)
            if not v:
                continue
        if k == "mechanism" and v not in ("mechanical", "quartz"):
            continue
        if k in ("exclude_leather", "calling", "sim") and not isinstance(v, bool):
            continue
        if k == "keywords":
            v = [norm(x)[:60] for x in v[:4] if isinstance(x, str)] if isinstance(v, list) else []
        if k in ("brand", "sku"):
            if not isinstance(v, str):
                continue
            v = v[:90]
        result[k] = v
    # Deterministic overrides for clear explicit customer requests.
    budget = re.search(r"\bдо\s*(\d[\d ]*)\s*(?:грн|₴)?", text)
    if budget:
        result["max_price"] = int(budget.group(1).replace(" ", ""))
    if re.search(r"без\s+(?:шкір|шкіря|кожан)", text):
        result["exclude_leather"] = True
        result.pop("strap", None)
    if re.search(r"сталев|стальн|\bсталь\b", text):
        result["strap"] = "steel"
        result.pop("exclude_leather", None)
    if re.search(r"механіч|mechanical", text):
        result["mechanism"] = "mechanical"
    if re.search(r"кварцов|quartz", text):
        result["mechanism"] = "quartz"
    if re.search(r"дзвінк|розмовлят|дзвонит|bluetooth.call", text) and not re.search(r"без.{0,20}(дзвінк|розмов)", text):
        result["calling"] = True
    if "без sim" in text or "без сім" in text:
        result["sim"] = False
    if re.search(r"тепер.*(?:можна|дозволяю).*шкір", text):
        result.pop("exclude_leather", None)
    # A new unrelated category must not inherit hard conditions which the
    # model might accidentally echo from the previous dialogue.
    if reset:
        evidence = {
            "calling": ("дзвін", "розмов", "дзвон"),
            "mechanism": ("механ", "кварц", "quartz"),
            "strap": ("ремін", "браслет", "стал", "метал", "шкір", "силік"),
            "exclude_leather": ("шкір", "кожан"),
            "sim": ("sim", "сім"),
        }
        for k, cues in evidence.items():
            if k in result and not any(cue in text for cue in cues):
                result.pop(k, None)
        if not re.search(r"\bдо\s*\d|\bвід\s*\d", text):
            result.pop("max_price", None)
            result.pop("min_price", None)
        if "brand" in result and norm(result["brand"]) not in text:
            result.pop("brand", None)
        # Stylistic suggestions from the previous category are not hard facts.
        if "keywords" in result and not any(norm(k) in text for k in result["keywords"]):
            result.pop("keywords", None)
    return result


def candidate_fact(p: dict) -> dict:
    attrs = p.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    favored = ["Матеріал ремінця", "Тип механізму", "Можливість розмовляти", "Стиль",
               "Водостійкість", "Скло", "Функції", "Матеріал корпусу", "Розміри, мм", "GPS", "Wi-Fi"]
    compact = {key: str(attrs[key])[:95] for key in favored if key in attrs and attrs[key] not in (None, "")}
    if len(compact) < 4:
        for key, val in attrs.items():
            if val is not None and key not in compact and len(compact) < 6:
                compact[key] = str(val)[:95]
    return {"id": str(p["site_id"]), "name": str(p["name"])[:110],
            "brand": str(p.get("vendor") or ""), "price_uah": float(p["price"]), "attributes": compact}


# One grounded Gemini round trip handles BOTH recommendation order and natural
# conversational wording. The API is called only for shortlisted, verified stock.
def clean_sales_line(raw: Any, maximum: int = 170) -> str:
    """Allow conversational phrasing, not unsupported catalog assertions."""
    if not isinstance(raw, str):
        return ""
    text = re.sub(r"[\r\n*#<>]+", " ", raw).strip()
    text = re.sub(r"\s+", " ", text)[:maximum].strip(" ,;:-")
    # Prices, numerical specs, absolute/safety claims are always server-rendered
    # from catalog data, never trusted from model-written free text.
    if (re.search(r"\d", text) or re.search(
        r"водостійк|водонепроник|плаван|купат|медичн|глюкоз|тиск у кров|" 
        r"гарантован|100\s*%|ударостійк|не б\w*ться|найкращ|" 
        r"\b(?:gps|wi.fi|sim)\b", norm(text)
    )):
        return ""
    return text


def choose_with_gemini(first: dict, tool_name: str, data: dict, candidates: list[dict],
                       action: str, with_advice: bool = False):
    """Return selected IDs; optionally structured natural advice in same API call.

    Never trust arbitrary model IDs/attributes. Strings from the model may only
    be short, non-factual connective language; catalog facts are rendered below.
    """
    if not candidates:
        return {"ids": [], "lead": "", "question": "", "focus": {}} if with_advice else []
    allowed = [str(p["site_id"]) for p in candidates]
    fallback = {"ids": allowed[:3], "lead": "", "question": "", "focus": {}}
    try:
        response_obj = {"tool": tool_name, "action": action,
                        "candidate_count": len(candidates),
                        "candidate_facts": [candidate_fact(p) for p in candidates]}
        contents = [
            {"role": "user", "parts": [{"text": json.dumps(data, ensure_ascii=False)}]},
            first["candidates"][0]["content"],
            {"role": "user", "parts": [{"functionResponse": {
                "name": tool_name, "response": {"result": response_obj}}}]}
        ]
        system = ("You are a thoughtful Ukrainian watch shop sales consultant at Zorro. "
                  "You already called the database search tool. Choose 1-3 IDs strictly "
                  "from candidate_facts, considering the buyer's need, budget, and constraints. "
                  "Produce ONLY JSON with fields: selected_ids (array of strings), "
                  "lead (ONE short natural Ukrainian sentence acknowledging the buyer's "
                  "request, MAX 115 characters), question (ONE relevant, brief, optional "
                  "follow-up question, MAX 110 characters), focus (object mapping each "
                  "chosen ID to 1-2 exact attribute field NAMES from that item's "
                  "candidate_facts.attributes; array of strings). "
                  "Use varied, helpful, warm, non-pushy language. Never claim "
                  "specific features, superiority, durability, waterproofing, health "
                  "benefits, warranty, prices, discounts or delivery conditions in lead "
                  "or question. The server will render ALL actual facts itself. "
                  "Do not include any numerical values in lead/question. "
                  "No markdown, no URLs, no explanations outside JSON. If budget "
                  "and preferences are clear, suggest a meaningful comparison "
                  "question instead of repeating an irrelevant budget question.")
        result = gemini(contents, system, json_mode=True)
        raw = "".join(p.get("text", "") for p in model_parts(result))
        plan = json.loads(raw)
        if not isinstance(plan, dict):
            raise ValueError("recommendation plan not object")
        chosen = plan.get("selected_ids") or []
        if not isinstance(chosen, list):
            chosen = []
        chosen = list(dict.fromkeys(str(x) for x in chosen if str(x) in allowed))[:3]
        if not chosen:
            chosen = allowed[:3]
        focus = {}
        supplied = {str(p["site_id"]): (p.get("attributes") or {}) for p in candidates}
        for pid in chosen:
            requested = (plan.get("focus") or {}).get(pid, []) if isinstance(plan.get("focus"), dict) else []
            if isinstance(requested, list) and isinstance(supplied[pid], dict):
                focus[pid] = [name for name in requested[:2]
                              if isinstance(name, str) and supplied[pid].get(name) not in (None, "")]
        result_plan = {"ids": chosen,
                       "lead": clean_sales_line(plan.get("lead"), 130),
                       "question": clean_sales_line(plan.get("question"), 125),
                       "focus": focus}
        return result_plan if with_advice else chosen
    except Exception as ex:
        LOG.warning("Gemini recommendation fallback: %s", type(ex).__name__)
        return fallback if with_advice else fallback["ids"]


def facts_for_customer(p: dict, buyer_filters: dict | None = None,
                       preferred: list[str] | None = None, limit: int = 2) -> list[str]:
    """Only return verbatim verified facts. Avoid irrelevant Wi-Fi/GPS negatives."""
    attrs = p.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    filters = buyer_filters or {}
    order = []
    if filters.get("calling") is True:
        order += ["Можливість розмовляти"]
    if filters.get("strap") or filters.get("exclude_leather"):
        order += ["Матеріал ремінця"]
    if filters.get("mechanism"):
        order += ["Тип механізму", "Механізм"]
    order += list(preferred or [])
    order += ["Стиль", "Матеріал ремінця", "Тип механізму", "Механізм",
              "Можливість розмовляти", "Функції", "Матеріал корпусу", "Скло"]
    found = []
    for k in dict.fromkeys(order):
        value = attrs.get(k)
        if value is None or not str(value).strip():
            continue
        # Avoid filling irrelevant features with negatives. A buyer explicitly
        # seeking calling still needs its verified "Так" attribute.
        if norm(str(value)) in NEGATIVE and k not in (preferred or []) and not filters.get("calling"):
            continue
        found.append("%s — %s" % (k, str(value).replace("\n", " ")[:100]))
        if len(found) >= limit:
            break
    return found


def human_search_reply(selected: list[dict], filters: dict, plan: dict) -> str:
    """Natural sales framing + server-verified details, small enough for chat UI."""
    lead = plan.get("lead") or "Підібрав кілька варіантів під ваш запит."
    # Optional model focus only influences which catalog fields to show;
    # attribute values and prices cannot be invented.
    first = selected[0]
    first_id = str(first["site_id"])
    notes = facts_for_customer(first, filters, plan.get("focus", {}).get(first_id), 2)
    if notes:
        line = "Я б почав із «%s» — %s ₴. %s." % (
            first["name"], int(first["price"]), "; ".join(notes))
    else:
        line = "Я б почав із «%s» — %s ₴." % (first["name"], int(first["price"]))
    if len(selected) > 1:
        line += " Інші відповідні варіанти — у картках нижче."
    question = plan.get("question") or "Хочете порівняти моделі чи уточнити побажання?"
    if "?" not in question:
        question = question.rstrip(".") + "?"
    return "\n\n".join((lead, line, question))


def conversation_frame(purpose: str, query: str, products: list[dict],
                       filters: dict | None = None) -> dict:
    """Optional one Gemini call for natural phrasing of comparison/description.

    No product claims are accepted from Gemini; only greetings/questions. The
    code renders all attribute-based content separately from the database.
    """
    fallback = {"lead": "", "question": "", "pick_id": "", "why_keys": []}
    if not KEY:
        return fallback
    context = {"purpose": purpose, "latest_customer_message": query,
               "preferences": filters or {},
               "products": [candidate_fact(p) for p in products]}
    system = ("You are an attentive Ukrainian sales consultant helping someone "
              "choose a watch. Respond with ONLY JSON containing 'lead' and "
              "'question'. 'lead' should be ONE warm, genuinely helpful Ukrainian "
              "sentence (max 110 chars) acknowledging the user's intention. "
              "'question' should be ONE sensible, short follow-up question "
              "(max 110 chars), tailored to the customer; it may be empty. "
              "For purpose='recommend' also return 'pick_id' (one ID from products) "
              "and 'why_keys' (1-2 exact attribute field names from THAT item) "
              "reflecting the customer's stated preferences. For other purposes "
              "omit these fields. "
              "NO product details, prices, numeric values, names, health claims, "
              "unverified quality claims, policies, Markdown or URLs. The server "
              "will assemble comparisons strictly from the verified catalog.")
    try:
        answer = gemini([{"role": "user", "parts": [{"text": json.dumps(context, ensure_ascii=False)}]}],
                        system, json_mode=True)
        raw = "".join(x.get("text", "") for x in model_parts(answer))
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return fallback
        ids = {str(p["site_id"]): p for p in products}
        pick_id = str(parsed.get("pick_id") or "")
        pick = ids.get(pick_id)
        why_keys = parsed.get("why_keys") or []
        valid_keys = ((pick.get("attributes") or {}) if pick else {})
        if not isinstance(valid_keys, dict):
            valid_keys = {}
        why_keys = [k for k in why_keys[:2]
                    if isinstance(k, str) and valid_keys.get(k) not in (None, "")] if isinstance(why_keys, list) else []
        return {"lead": clean_sales_line(parsed.get("lead"), 135),
                "question": clean_sales_line(parsed.get("question"), 120),
                "pick_id": pick_id if pick else "", "why_keys": why_keys}
    except Exception as ex:
        LOG.warning("Gemini conversational fallback: %s", type(ex).__name__)
        return fallback


def finish_human_reply(lead: str, body: str, question: str, default_question: str) -> str:
    lead = lead or ""
    question = question or default_question
    if question and "?" not in question:
        question = question.rstrip(".") + "?"
    return "\n\n".join(part for part in (lead, body, question) if part)


def render_product(p: dict) -> dict:
    attrs = p.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    preferred = ["Матеріал ремінця", "Тип механізму", "Можливість розмовляти", "Функції", "Стиль", "Водостійкість"]
    specs = [{"name": k, "value": str(attrs[k])[:150]} for k in preferred if attrs.get(k) not in (None, "")]
    if len(specs) < 3:
        specs += [{"name": k, "value": str(v)[:150]} for k, v in attrs.items()
                  if v not in (None, "") and k not in preferred][:3-len(specs)]
    link = str(p.get("url") or "")
    if urlparse(link).scheme != "https" or urlparse(link).hostname not in ("zorro.com.ua", "www.zorro.com.ua"):
        link = "https://zorro.com.ua/"
    image = str(p.get("image") or "")
    if urlparse(image).scheme != "https" or urlparse(image).hostname not in ("zorro.com.ua", "www.zorro.com.ua"):
        image = ""
    return {"id": str(p["site_id"]), "name": p["name"], "brand": p.get("vendor") or "",
            "sku": p.get("sku") or "", "category": p.get("category") or "", "price": float(p["price"]),
            "url": link, "image": image, "attributes": specs}


def compare_text(items: list[dict], question: str = "", filters: dict | None = None) -> str:
    if not items:
        return "Спочатку підберімо моделі. Який тип годинника та бюджет цікавить?"
    frame = conversation_frame("compare", question, items, filters)
    # Price + only two informative differentiators per product.
    common = ["Можливість розмовляти", "Тип механізму", "Механізм",
              "Матеріал ремінця", "Матеріал корпусу", "Стиль", "Функції", "Скло"]
    def value(p, key):
        a = p.get("attributes") or {}
        return norm(a.get(key)) if isinstance(a, dict) else ""
    different = [key for key in common if len({value(p, key) for p in items}) > 1]
    differences = different or common
    lines = []
    for p in items[:3]:
        facts = facts_for_customer(p, {}, differences, 2)
        text = "%s — %s ₴" % (p["name"], int(p["price"]))
        if facts:
            text += ". " + "; ".join(facts) + "."
        lines.append(text)
    body = "\n".join(lines)
    return finish_human_reply(frame["lead"], body, frame["question"],
                              "Що для вас важливіше: ціна, стиль чи певна функція?")


def describe_text(p: dict, question: str = "", filters: dict | None = None) -> str:
    frame = conversation_frame("describe", question, [p], filters)
    facts = facts_for_customer(p, filters, limit=3)
    body = "%s — %s ₴." % (p["name"], int(p["price"]))
    if facts:
        body += " За каталогом: " + "; ".join(facts) + "."
    else:
        body += " Додаткових підтверджених характеристик у каталозі замало."
    return finish_human_reply(frame["lead"], body, frame["question"],
                              "Хочете порівняти цю модель з іншими?")


def personal_recommendation(items: list[dict], question: str,
                            filters: dict | None = None) -> str:
    if not items:
        return "Підберімо кілька годинників, а тоді я поясню, який із них краще відповідає вашим побажанням."
    frame = conversation_frame("recommend", question, items, filters)
    chosen = next((p for p in items if str(p["site_id"]) == frame.get("pick_id")), items[0])
    facts = facts_for_customer(chosen, filters, frame.get("why_keys"), 2)
    body = "Із показаних я б насамперед розглянув «%s» — %s ₴." % (chosen["name"], int(chosen["price"]))
    if facts:
        body += " У каталозі підтверджено: " + "; ".join(facts) + "."
    if len(items) > 1:
        body += " Можемо зіставити його з іншими за тим, що важливо саме вам."
    return finish_human_reply(frame["lead"], body, frame["question"],
                              "Вам важливіші ціна, зовнішній вигляд чи функції?")


def no_results_message(filters: dict, question: str = "") -> str:
    # Do not claim products outside exact filters exist.
    frame = conversation_frame("no_results", question, [], filters)
    category = filters.get("category") or "годинники"
    body = "За всіма поточними вимогами не знайшов підтверджено доступних товарів у категорії «%s»." % category
    return finish_human_reply(frame["lead"], body, frame["question"],
                              "Що для вас можна змінити: бюджет чи одну з характеристик?")


def chat_controller(data: ChatIn) -> dict:
    current = data.state if isinstance(data.state, dict) else {}
    state = {"filters": current.get("filters") if isinstance(current.get("filters"), dict) else {},
             "product_ids": [str(i) for i in current.get("product_ids", [])[:10]] if isinstance(current.get("product_ids"), list) else [],
             "search_intent": str(current.get("search_intent") or "")[:350],
             "buyer_profile": clean_profile(current.get("buyer_profile"))}
    history = [{"role": p.role[:10], "content": re.sub(r"<[^>]*>", " ", p.content)[:450]}
               for p in data.history[-10:] if p.role in ("user", "assistant")]
    message = data.message.strip()
    context = {"latest_message": message, "current_product_title": data.current_product,
               "current_filters": state["filters"], "buyer_profile": state["buyer_profile"],
               "previous_shown_ids": state["product_ids"],
               "recent_dialogue": history[-8:]}
    first, tool, args = None, None, {}
    try:
        tool, args, first = select_action(message, history, state, data.current_product)
    except Exception as ex:
        LOG.warning("Gemini intent fallback: %s", type(ex).__name__)
        lower = norm(message)
        if re.search(r"який.{0,35}(?:кращ|порад|вибрат)|що.{0,25}взяти|котрий.{0,25}порад", lower) and state["product_ids"]:
            tool = "recommend_shown"
        elif re.search(r"порівн|порівняй", lower):
            tool = "compare_watches"
        elif re.search(r"опис|характеристик|детальн", lower) and state["product_ids"]:
            tool = "describe_watch"
        elif re.search(r"достав|оплат|гарант|повернен", lower):
            tool = "store_information"
        else:
            tool, args = "search_catalog", {"mode": "refine"}
    proposed_update = args.get("profile_update", {}) if isinstance(args, dict) else {}
    if tool != "search_catalog":
        state["buyer_profile"] = merge_profile(state["buyer_profile"], proposed_update,
                                                 previously_shown=state["product_ids"])
    if tool == "store_information":
        return {"reply": "Умови доставки, оплати, повернення та гарантії перевіряйте на сторінках магазину Zorro — не хочу повідомляти непідтверджені дані.",
                "products": [], "state": state}
    if tool == "ask_customer":
        question = clean_sales_line(args.get("question") if isinstance(args, dict) else "", 130)
        if not question:
            question = "Для кого підбираємо годинник і який приблизно бюджет?"
        if "?" not in question:
            question = question.rstrip(".") + "?"
        return {"reply": "З радістю допоможу з вибором. " + question,
                "products": [], "state": state}
    # Buyer preference to hear an opinion is distinct from asking for
    # a full comparison. Never choose a product not previously shown.
    if state["product_ids"] and re.search(
        r"який.{0,35}(?:кращ|порад|вибрат)|що.{0,25}взяти|котрий.{0,25}порад", norm(message)
    ):
        tool = "recommend_shown"
    if tool == "recommend_shown":
        products = [p for p in get_ids(state["product_ids"])
                    if str(p["site_id"]) not in state["buyer_profile"]["rejected_ids"]]
        if not products:
            return {"reply": "Ці моделі більше не розглядаємо. Підібрати інші за вашими побажаннями?",
                    "products": [], "state": state}
        return {"reply": sales_reply("recommend", message, products, state["filters"], history, lambda: personal_recommendation(products, message, {**state["filters"], "buyer_profile": state["buyer_profile"]}), state["buyer_profile"]),
                "products": [], "state": state}
    if tool == "compare_watches":
        ids = state["product_ids"]
        positions = args.get("positions") if isinstance(args, dict) else None
        chosen_ids = args.get("ids") if isinstance(args, dict) else None
        if isinstance(positions, list) and positions:
            selected = [ids[i-1] for i in positions if type(i) is int and 1 <= i <= len(ids)]
        elif isinstance(chosen_ids, list) and chosen_ids:
            selected = [str(i) for i in chosen_ids if str(i) in ids]
        else:
            selected = list(ids)
        text = norm(message)
        if re.search(r"перш.*трет", text) and len(ids) >= 3:
            selected = [ids[0], ids[2]]
        elif re.search(r"перш.*друг", text) and len(ids) >= 2:
            selected = [ids[0], ids[1]]
        products = get_ids(selected)
        reply = sales_reply("compare", message, products, state["filters"], history,
                            lambda: compare_text(products, message, {**state["filters"], "buyer_profile": state["buyer_profile"]}),
                            state["buyer_profile"])
        if isinstance(args, dict) and args.get("also_search") is True:
            query = (state.get("search_intent") or "") + " " + message + " " + retrieval_context(state["buyer_profile"])
            alternatives = [p for p in shortlist(state["filters"], query, 120)
                            if str(p["site_id"]) not in ids
                            and str(p["site_id"]) not in state["buyer_profile"]["rejected_ids"]
                            and not dislikes_product(p, state["buyer_profile"])][:3]
            if alternatives:
                reply += "\n\nТакож знайшов інші варіанти за вашими побажаннями — вони у картках нижче."
                state["product_ids"] = [str(p["site_id"]) for p in alternatives]
                return {"reply": reply, "products": [render_product(p) for p in alternatives], "state": state}
        return {"reply": reply, "products": [], "state": state}
    if tool == "describe_watch":
        ids = state["product_ids"]
        target = str(args.get("id") or "") if isinstance(args, dict) else ""
        position = args.get("position") if isinstance(args, dict) else None
        if not target and type(position) is int and 1 <= position <= len(ids):
            target = ids[position-1]
        if not target:
            # The GTM card button sends a product NAME, not its ID.
            previous_products = get_ids(ids)
            for previous in previous_products:
                if norm(previous.get("name")) and norm(previous["name"]) in norm(message):
                    target = str(previous["site_id"])
                    break
        if not target:
            text = norm(message)
            if "перш" in text and ids: target = ids[0]
            elif "друг" in text and len(ids) >= 2: target = ids[1]
            elif "трет" in text and len(ids) >= 3: target = ids[2]
            elif len(ids) == 1: target = ids[0]
        products = get_ids([target]) if target in ids else []
        if not products and data.current_product:
            hit = find_current_product(data.current_product)
            products = [hit] if hit else []
        if not products:
            return {"reply": "Про яку саме модель із показаних розповісти? Напишіть її назву або номер.",
                    "products": [], "state": state}
        return {"reply": sales_reply("describe", message, products, state["filters"], history, lambda: describe_text(products[0], message, {**state["filters"], "buyer_profile": state["buyer_profile"]}), state["buyer_profile"]), "products": [], "state": state}
    if tool != "search_catalog":
        tool, args = "search_catalog", {"mode": "refine"}
    filters = merge_filter_state(state["filters"], args if isinstance(args, dict) else {}, message)
    new_topic = (bool(state["filters"].get("category") and filters.get("category")
                      and state["filters"]["category"] != filters["category"])
                 or bool(re.search(r"новий пошук|почн\w* спочатку|забудь попередн", norm(message))))
    state["buyer_profile"] = merge_profile(state["buyer_profile"], proposed_update, new_topic,
                                             previously_shown=state["product_ids"])
    if (state["product_ids"] and re.search(r"дешевш|подешевше|бюджетніш", norm(message))
            and not re.search(r"\bдо\s*\d|\bвід\s*\d", norm(message))):
        recent = get_ids(state["product_ids"])
        if recent:
            cheaper_than = max(1, int(min(float(p["price"]) for p in recent)) - 1)
            current_bound = filters.get("max_price")
            filters["max_price"] = min(int(current_bound), cheaper_than) if current_bound is not None else cheaper_than
    if not filters and not re.search(r"годин|watch|смарт|подарун|skmei", norm(message)):
        return {"reply": "Які годинники шукаєте та який бажаний бюджет?", "products": [], "state": state}
    # Semantic query keeps the buyer's true intention during brief follow-ups.
    prior_intent = state.get("search_intent") or ""
    changes_only = bool(re.search(r"^(?:а )?(дешевш|подешевш|дорожч|без |ще |із |з |тільки |менш )", norm(message)))
    semantic_query = (prior_intent + " " + message).strip() if changes_only and prior_intent and args.get("mode") != "new" and not determine_category(message) else message
    if isinstance(args.get("search_query"), str) and args["search_query"].strip():
        proposed = re.sub(r"[\r\n]+", " ", args["search_query"]).strip()[:240]
        # Gemini is allowed to paraphrase query semantics, not bypass catalog filters.
        semantic_query = (prior_intent + " " + proposed).strip() if changes_only and prior_intent else proposed
    if filters.get("category") and norm(filters["category"]) not in norm(semantic_query):
        semantic_query += " " + filters["category"]
    # Gemini-proposed soft synonyms expand vector retrieval; mandatory criteria
    # still come exclusively from PostgreSQL fields, never from synonyms.
    expansion = [str(k).strip()[:45] for k in (filters.get("keywords") or [])[:4]
                 if isinstance(k, str) and str(k).strip()]
    retrieval_query = (semantic_query + " " + " ".join(expansion) + " " +
                       retrieval_context(state["buyer_profile"])).strip()[:500]
    # Retrieve broadly before applying free-form dislikes, so the first page of
    # vector results cannot crowd out valid alternatives without that feature.
    candidates = shortlist(filters, retrieval_query, 120)
    # A missing attribute is UNKNOWN, never evidence of absence.
    candidates = [p for p in candidates
                  if str(p["site_id"]) not in state["buyer_profile"]["rejected_ids"]
                  and not dislikes_product(p, state["buyer_profile"])][:36]
    if not candidates:
        return {"reply": no_results_message(filters, message), "products": [],
                "state": {"filters": filters, "product_ids": [], "search_intent": semantic_query[:350],
                          "buyer_profile": state["buyer_profile"]}}
    context["buyer_profile"] = state["buyer_profile"]
    plan = (choose_with_gemini(first, tool, context, candidates, "search", with_advice=True)
            if first else {"ids": [str(p["site_id"]) for p in candidates[:3]],
                           "lead": "", "question": "", "focus": {}})
    if isinstance(plan, list):  # compatibility with earlier v5 callers
        plan = {"ids": plan, "lead": "", "question": "", "focus": {}}
    by_id = {str(p["site_id"]): p for p in candidates}
    selected = [by_id[i] for i in plan.get("ids", []) if i in by_id][:3]
    if not selected:
        selected = candidates[:3]
    reply = sales_reply("search", message, selected, filters, history,
                        lambda: human_search_reply(selected, filters, plan), state["buyer_profile"])
    return {"reply": reply, "products": [render_product(p) for p in selected],
            "state": {"filters": filters, "product_ids": [str(p["site_id"]) for p in selected],
                       "search_intent": semantic_query[:350], "buyer_profile": state["buyer_profile"]}}


@app.get("/")
def root():
    return {"status": "ok", "name": "Zorro AI", "version": "6.1", "model": MODEL}


@app.get("/api/catalog-status")
def catalog_status():
    connection = db_connection()
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*),COUNT(*) FILTER (WHERE available AND price > 0) FROM zorro_products_v2")
            all_items, available = cur.fetchone()
            result = {"status": "ok", "products": all_items, "available": available}
            try:
                result["hybrid_search"] = index_status()
            except Exception as ex:
                result["hybrid_search"] = {"ready": False, "error": type(ex).__name__}
            return result
    finally:
        connection.close()


@app.post("/api/chat")
def chat(data: ChatIn):
    try:
        return chat_controller(data)
    except Exception:
        LOG.exception("Chat failed")
        return {"reply": "Не вдалося обробити запит. Спробуйте ще раз або уточніть вимоги.",
                "products": [], "state": data.state}


@app.post("/api/callback")
def callback(data: CallbackIn):
    phone = data.phone.strip()
    if not re.fullmatch(r"\+?[0-9() \-]{9,22}", phone):
        return {"status": "error", "reply": "Будь ласка, перевірте номер телефону."}
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return {"status": "error", "reply": "Зворотний дзвінок поки не підключений. Скористайтеся контактами магазину."}
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": "Заявка Zorro AI: " + phone}, timeout=10)
        r.raise_for_status()
        return {"status": "success", "reply": "Дякуємо! Заявку передано менеджеру."}
    except requests.RequestException:
        LOG.exception("Callback delivery failed")
        return {"status": "error", "reply": "Не вдалося передати заявку. Скористайтеся контактами магазину."}
