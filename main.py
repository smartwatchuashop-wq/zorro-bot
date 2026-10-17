
import os
import re
import json
import logging
from urllib.parse import urlparse

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# =====================================================
# ZORRO AI — CONSULTANT
# Gemini + PostgreSQL + FastAPI
# =====================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zorro-ai")

DATABASE_URL = os.getenv("DATABASE_URL", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL", "gemini-3.5-flash-lite"
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

app = FastAPI(title="Zorro AI Consultant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


# =====================================================
# DATA MODELS
# =====================================================

class Message(BaseModel):
    role: str
    content: str


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[Message] = Field(default_factory=list)
    state: dict = Field(default_factory=dict)
    current_product: str | None = None


class CallbackInput(BaseModel):
    phone: str
    message: str = ""


# =====================================================
# DATABASE
# =====================================================

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не налаштований")

    conn = psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10
    )

    # Консультант тільки читає товари.
    # Імпорт XML тут не запускається.
    conn.set_session(readonly=True, autocommit=True)

    return conn


CATEGORIES = {
    "men": "Чоловічі годинники",
    "women": "Жіночі годинники",
    "smart": "Смарт годинники",
    "kids": "Дитячі годинники",
    "sports": "Спортивні годинники",
    "unisex": "Унісекс годинники"
}


def norm(value):
    return re.sub(
        r"\s+", " ", str(value or "").casefold()
    ).strip()


def canonical_category(value):
    s = norm(value).replace("-", " ")

    if "смарт" in s or "smart" in s:
        return CATEGORIES["smart"]

    if "чолов" in s or "муж" in s or s == "men":
        return CATEGORIES["men"]

    if "жіноч" in s or "жен" in s or s == "women":
        return CATEGORIES["women"]

    if "дитяч" in s or "детск" in s or s == "kids":
        return CATEGORIES["kids"]

    if "спорт" in s or "sport" in s:
        return CATEGORIES["sports"]

    if "унісекс" in s or "unisex" in s:
        return CATEGORIES["unisex"]

    return None


# =====================================================
# ATTRIBUTE SEARCH
# =====================================================

MATERIALS = {
    "leather": r"шкір|кожан|leather|замш",
    "steel": r"сталев|сталь|стальн|нержавіюч|stainless|steel",
    "silicone": r"силікон|силикон|silicon",
    "rubber": r"каучук|гумов|резин|rubber",
    "fabric": r"тканин|тканев|текстил|нейлон|nylon|fabric",
    "plastic": r"пластик|пластмасс|plastic",
    "metal": r"метал|metal",
    "titanium": r"титан|titanium",
    "ceramic": r"керамік|керамик|ceramic"
}


def material_kinds(value):
    s = norm(value)

    if not s:
        return set()

    return {
        name
        for name, pattern in MATERIALS.items()
        if re.search(pattern, s)
    }


def attribute_match(attributes, field, wanted):
    if not isinstance(attributes, dict):
        return False

    actual = norm(attributes.get(field))
    expected = norm(wanted)

    if not actual or not expected:
        return False

    if actual in (
        "ні", "нет", "немає", "відсутній",
        "відсутня", "false", "0", "-"
    ):
        return False

    if actual.startswith((
        "без ", "не ", "without ", "no "
    )):
        return False

    field_name = norm(field)

    if "матеріал ремін" in field_name or \
       "матеріал браслет" in field_name:

        expected_materials = material_kinds(expected)
        actual_materials = material_kinds(actual)

        if expected_materials:
            if "metal" in expected_materials:
                return bool(
                    actual_materials &
                    {"metal", "steel", "titanium"}
                )

            return bool(
                expected_materials & actual_materials
            )

    if expected in ("так", "yes", "true", "є"):
        return actual in (
            "так", "yes", "true", "є",
            "підтримується", "присутня"
        )

    if "механізм" in field_name:
        if "механ" in expected:
            return (
                "механ" in actual or
                "автозавод" in actual or
                "автопідзавод" in actual
            )

        if "кварц" in expected or "quartz" in expected:
            return (
                "кварц" in actual or
                "quartz" in actual
            )

    return (
        actual == expected or
        bool(re.search(
            r"(?<!\w)" + re.escape(expected) + r"(?!\w)",
            actual
        ))
    )


def normalize_filters(raw):
    if not isinstance(raw, dict):
        return {}

    filters = {}

    for key in ("max_price", "min_price"):
        value = raw.get(key)

        if value is None or isinstance(value, bool):
            continue

        try:
            price = int(str(value).replace(" ", ""))

            if 0 <= price <= 10000000:
                filters[key] = price
        except (ValueError, TypeError):
            pass

    for key in (
        "category", "brand", "sku", "watch_type",
        "audience"
    ):
        value = raw.get(key)

        if isinstance(value, str):
            filters[key] = value[:100]

    if filters.get("category"):
        filters["category"] = (
            canonical_category(filters["category"])
            or filters["category"]
        )

    for key in ("must_have", "exclude"):
        value = raw.get(key)

        if not isinstance(value, dict):
            continue

        result = {}

        for field, wanted in list(value.items())[:12]:
            if not isinstance(field, str):
                continue

            if key == "must_have":
                if isinstance(wanted, list) and len(wanted) == 1:
                    wanted = wanted[0]

                if isinstance(wanted, str):
                    result[field] = wanted

            else:
                if isinstance(wanted, str):
                    wanted = [wanted]

                if isinstance(wanted, list):
                    result[field] = [
                        v for v in wanted
                        if isinstance(v, str)
                    ]

        filters[key] = result

    if isinstance(raw.get("include"), list):
        filters["include"] = [
            str(x)[:80]
            for x in raw["include"][:6]
            if isinstance(x, str)
        ]

    return filters


# =====================================================
# APPLY BUYER REQUIREMENTS
# =====================================================

def apply_message_rules(message, filters):
    text = norm(message)

    if re.search(
        r"новий пошук|почнемо спочатку|забудь попередн",
        text
    ):
        filters.clear()

    if "без обмеження бюджету" in text or \
       "будь-яка ціна" in text:
        filters.pop("max_price", None)
        filters.pop("min_price", None)

    max_price = re.search(
        r"\bдо\s*(\d[\d ]*)\s*(?:грн|₴)?",
        text
    )

    if max_price:
        filters["max_price"] = int(
            max_price.group(1).replace(" ", "")
        )

    min_price = re.search(
        r"\bвід\s*(\d[\d ]*)\s*(?:грн|₴)?",
        text
    )

    if min_price:
        filters["min_price"] = int(
            min_price.group(1).replace(" ", "")
        )

    if re.search(
        r"чоловіч|для чоловік|для тата|мужск",
        text
    ):
        filters["category"] = CATEGORIES["men"]

    elif re.search(
        r"жіноч|для жінк|для мами|женск",
        text
    ):
        filters["category"] = CATEGORIES["women"]

    elif re.search(
        r"дитяч|для дитини|детск",
        text
    ):
        filters["category"] = CATEGORIES["kids"]

    elif re.search(r"смарт|smart", text):
        filters["category"] = CATEGORIES["smart"]

    elif re.search(r"спортивн|спорт годин", text):
        filters["category"] = CATEGORIES["sports"]

    if re.search(r"\bskmei\b", text):
        filters["brand"] = "Skmei"

    if re.search(r"механічн|механическ|mechanical", text):
        filters["watch_type"] = "mechanical"
        filters.setdefault(
            "must_have", {}
        )["Тип механізму"] = "Механічний"

    if re.search(r"кварцов|кварцев|quartz", text):
        filters["watch_type"] = "quartz"
        filters.setdefault(
            "must_have", {}
        )["Тип механізму"] = "Кварцовий"

    if re.search(r"сталев|сталлю|сталь|стальн", text):
        filters.setdefault(
            "must_have", {}
        )["Матеріал ремінця"] = "Сталевий"

    if re.search(
        r"без\s+(?:шкір|шкіря|кожан)|не\s+шкіря",
        text
    ):
        filters.setdefault(
            "exclude", {}
        )["Матеріал ремінця"] = ["Шкіряний"]

    if re.search(
        r"тепер (можна|хочу).*шкір",
        text
    ):
        filters.get(
            "exclude", {}
        ).pop("Матеріал ремінця", None)

    if re.search(
        r"розмовлят|дзвінк|дзвонит|bluetooth.?дзвін",
        text
    ) and not re.search(
        r"(?:без|не потрібн|не треба).*?(?:дзвінк|розмов)",
        text
    ):
        filters.setdefault(
            "must_have", {}
        )["Можливість розмовляти"] = "Так"

    # Важливі характеристики не дублюємо як
    # неточні ключові слова для пошуку.
    if isinstance(filters.get("include"), list):
        words = [
            w for w in filters["include"]
            if not re.search(
                r"механіч|сталев|стальн|\bskmei\b",
                norm(w)
            )
        ]

        if words:
            filters["include"] = words
        else:
            filters.pop("include", None)

    return filters


def merge_filters(previous, incoming, message):
    old = normalize_filters(previous)
    new = normalize_filters(incoming)

    if re.search(
        r"новий пошук|почнемо спочатку|забудь попередн",
        norm(message)
    ):
        old = {}

    for key, value in new.items():
        if key in ("must_have", "exclude"):
            old[key] = {
                **(old.get(key) or {}),
                **value
            }
        else:
            old[key] = value

    if "без обмеження бюджету" in norm(message) or \
       "будь-яка ціна" in norm(message):
        old.pop("max_price", None)
        old.pop("min_price", None)

    return apply_message_rules(message, old)


# =====================================================
# SEARCH REAL PRODUCTS IN POSTGRESQL
# =====================================================

PRODUCT_COLUMNS = """
site_id, sku, name, vendor, category, price,
available, url, image, description, attributes
"""


def search_products(filters, limit=3):
    clauses = [
        "available = TRUE",
        "price > 0",
        "url IS NOT NULL",
        "url <> ''",
        "category = ANY(%s)"
    ]

    params = [list(CATEGORIES.values())]

    if filters.get("category"):
        category = canonical_category(
            filters["category"]
        )

        if not category:
            return []

        clauses.append("category = %s")
        params.append(category)

    if filters.get("max_price") is not None:
        clauses.append("price <= %s")
        params.append(filters["max_price"])

    if filters.get("min_price") is not None:
        clauses.append("price >= %s")
        params.append(filters["min_price"])

    if filters.get("brand"):
        clauses.append("LOWER(vendor) = LOWER(%s)")
        params.append(filters["brand"])

    if filters.get("sku"):
        clauses.append("LOWER(sku) = LOWER(%s)")
        params.append(filters["sku"])

    sql = f"""
        SELECT {PRODUCT_COLUMNS}
        FROM zorro_products_v2
        WHERE {" AND ".join(clauses)}
        ORDER BY price ASC
        LIMIT 5000
    """

    conn = db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(sql, params)
            rows = [
                dict(row)
                for row in cursor.fetchall()
            ]
    finally:
        conn.close()

    results = []

    required = filters.get("must_have") or {}
    excluded = filters.get("exclude") or {}
    include = filters.get("include") or []

    for product in rows:
        attrs = product.get("attributes") or {}

        if not isinstance(attrs, dict):
            attrs = {}

        product_type = norm(
            product.get("category")
        )

        watch_type = norm(
            filters.get("watch_type")
        )

        if watch_type == "smart" and \
           "смарт" not in product_type:
            continue

        if watch_type in ("mechanical", "quartz") and \
           "смарт" in product_type:
            continue

        if any(
            not attribute_match(attrs, field, wanted)
            for field, wanted in required.items()
        ):
            continue

        reject = False

        for field, unwanted_values in excluded.items():
            actual = attrs.get(field)

            if not actual:
                reject = True
                break

            if any(
                attribute_match(attrs, field, unwanted)
                for unwanted in unwanted_values
            ):
                reject = True
                break

        if reject:
            continue

        searchable = norm(" ".join([
            str(product.get("name") or ""),
            str(product.get("vendor") or ""),
            str(product.get("sku") or ""),
            str(product.get("description") or ""),
            " ".join(
                f"{k} {v}"
                for k, v in attrs.items()
            )
        ]))

        if include and not all(
            norm(word) in searchable
            for word in include
        ):
            continue

        results.append(product)

    if not results:
        return []

    # При широкому бюджеті показуємо різні
    # цінові рівні, а не три найдешевші моделі.
    if (
        len(results) >= 6
        and filters.get("max_price") is not None
        and not filters.get("sku")
        and not include
    ):
        positions = (0.2, 0.5, 0.8)

        selected = [
            results[
                round((len(results) - 1) * fraction)
            ]
            for fraction in positions
        ]

        unique = {
            str(p["site_id"]): p
            for p in selected
        }

        return list(unique.values())[:limit]

    return results[:limit]


def products_by_ids(ids):
    if not ids:
        return []

    ids = [
        str(pid)
        for pid in ids[:10]
    ]

    conn = db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                f"""
                SELECT {PRODUCT_COLUMNS}
                FROM zorro_products_v2
                WHERE site_id = ANY(%s)
                  AND available = TRUE
                  AND price > 0
                  AND url <> ''
                  AND category = ANY(%s)
                """,
                (
                    ids,
                    list(CATEGORIES.values())
                )
            )

            rows = {
                str(p["site_id"]): dict(p)
                for p in cursor.fetchall()
            }
    finally:
        conn.close()

    return [
        rows[pid]
        for pid in ids
        if pid in rows
    ]


def product_by_name(name):
    if not name:
        return None

    conn = db()

    try:
        with conn.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                f"""
                SELECT {PRODUCT_COLUMNS}
                FROM zorro_products_v2
                WHERE LOWER(name) = LOWER(%s)
                  AND available = TRUE
                  AND price > 0
                  AND url <> ''
                LIMIT 1
                """,
                (name.strip()[:250],)
            )

            row = cursor.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


# =====================================================
# GEMINI API
# =====================================================

def gemini_request(system_prompt, payload, json_mode=False):
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY відсутній")
        return None

    generation = {
        "temperature": 0.1 if json_mode else 0.25,
        "maxOutputTokens": 1100
    }

    if json_mode:
        generation["responseMimeType"] = "application/json"

    body = {
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": [{
            "role": "user",
            "parts": [{
                "text": json.dumps(
                    payload,
                    ensure_ascii=False
                )
            }]
        }],
        "generationConfig": generation
    }

    try:
        response = requests.post(
            (
                "https://generativelanguage.googleapis.com/"
                f"v1beta/models/{GEMINI_MODEL}:generateContent"
            ),
            headers={
                "x-goog-api-key": GEMINI_API_KEY
            },
            json=body,
            timeout=25
        )

        response.raise_for_status()
        data = response.json()

        return "".join(
            part.get("text", "")
            for part in data["candidates"][0][
                "content"
            ]["parts"]
        ).strip()

    except Exception:
        log.exception("Помилка Gemini API")
        return None


INTENT_PROMPT = """
You are a product search planner for the Ukrainian
watch store Zorro.

Read the buyer's latest message and conversation.
Return only JSON.

Required structure:
{
 "action": "search",
 "filters": {},
 "followup": false
}

Allowed actions:
search, compare, describe, question.

Allowed filters:
max_price, min_price, brand, category, sku,
watch_type, must_have, exclude, include.

Real category names:
Чоловічі годинники
Жіночі годинники
Смарт годинники
Дитячі годинники
Спортивні годинники
Унісекс годинники

Use actual catalog attribute names:
Матеріал ремінця
Тип механізму
Можливість розмовляти

Examples:
"без шкіри" =>
exclude: {"Матеріал ремінця": ["Шкіряний"]}

"можна розмовляти" =>
must_have: {"Можливість розмовляти": "Так"}

"механічний" =>
must_have: {"Тип механізму": "Механічний"}

"сталевий ремінець" =>
must_have: {"Матеріал ремінця": "Сталевий"}

Keep previous buyer preferences on followups.
Do not invent product specifications.
Never return SQL or Python code.
"""


SALES_PROMPT = """
You are Zorro AI, a Ukrainian watch shop consultant.

Speak naturally in Ukrainian.
Be helpful, concise and friendly.

Use only verified facts about the provided products.
Never invent specifications, prices, brands,
warranty conditions, discounts or delivery terms.

When comparing products, compare only supplied models.
Explain meaningful verified differences.
Do not claim water resistance permits swimming
unless exact supporting information exists.

If a specification is missing, say it is unknown.
Ask no more than one useful follow-up question.
Return plain text, not HTML.
"""


# =====================================================
# PRODUCT CARDS FOR WIDGET
# =====================================================

def product_card(product):
    attrs = product.get("attributes") or {}

    if not isinstance(attrs, dict):
        attrs = {}

    specifications = [
        {
            "name": str(key),
            "value": str(value)
        }
        for key, value in attrs.items()
        if value is not None and str(value).strip()
    ][:4]

    return {
        "id": str(product["site_id"]),
        "sku": product.get("sku") or "",
        "name": product.get("name") or "",
        "brand": product.get("vendor") or "",
        "category": product.get("category") or "",
        "price": float(product["price"]),
        "url": product.get("url") or "",
        "image": product.get("image") or "",
        "description": (
            product.get("description") or ""
        )[:500],
        "attributes": specifications
    }


# =====================================================
# CHAT CONTROLLER
# =====================================================

def handle_chat(data):
    message = data.message.strip()
    lowered = norm(message)

    raw_state = data.state or {}

    state = (
        dict(raw_state)
        if isinstance(raw_state, dict)
        else {}
    )

    previous_filters = state.get("filters") or {}
    previous_ids = state.get("product_ids") or []

    if not isinstance(previous_ids, list):
        previous_ids = []

    history = [
        {
            "role": h.role,
            "content": h.content[:500]
        }
        for h in data.history[-12:]
    ]

    intent_text = gemini_request(
        INTENT_PROMPT,
        {
            "message": message,
            "history": history
        },
        json_mode=True
    )

    intent = {}

    if intent_text:
        try:
            intent = json.loads(intent_text)
        except (ValueError, TypeError):
            intent = {}

    if not isinstance(intent, dict):
        intent = {}

    action = intent.get("action", "search")

    if action not in (
        "search", "compare", "describe", "question"
    ):
        action = "search"

    if re.search(
        r"порівняй|порівняти|чим відрізня|сравни",
        lowered
    ):
        action = "compare"

    elif re.search(
        r"розкажи|розкажіть|детальніше|опис|характеристик",
        lowered
    ) and previous_ids:
        action = "describe"

    if re.search(
        r"достав|оплат|гарант|повернен|контакт",
        lowered
    ):
        action = "question"

    if action == "question":
        return {
            "reply": (
                "Допоможу з вибором годинника. "
                "Точні умови доставки, оплати, "
                "повернення та гарантії потрібно "
                "дивитися на відповідних сторінках Zorro."
            ),
            "products": [],
            "state": state
        }

    filters = merge_filters(
        previous_filters,
        intent.get("filters") or {},
        message
    )

    if not intent and not filters and action == "search":
        return {
            "reply": (
                "Підкажіть, будь ласка, які годинники "
                "шукаєте: чоловічі, жіночі, дитячі "
                "чи смарт? І який орієнтовний бюджет?"
            ),
            "products": [],
            "state": state
        }

    products = []
    selected_ids = [
        str(pid)
        for pid in previous_ids[:10]
    ]

    if action == "compare":
        products = products_by_ids(selected_ids)

        if re.search(
            r"перш\w*\s+(?:і|та)\s+трет",
            lowered
        ) and len(products) >= 3:
            products = [
                products[0], products[2]
            ]

        elif re.search(
            r"перш\w*\s+(?:і|та)\s+друг",
            lowered
        ) and len(products) >= 2:
            products = [
                products[0], products[1]
            ]

    elif action == "describe":
        products = products_by_ids(selected_ids)

        if not products and data.current_product:
            current = product_by_name(
                data.current_product
            )

            if current:
                products = [current]

        if products:
            matching = [
                p for p in products
                if norm(p["name"]) in lowered
            ]

            if matching:
                products = matching[:1]

            elif "перш" in lowered:
                products = products[:1]

            elif "друг" in lowered and len(products) > 1:
                products = products[1:2]

            elif "трет" in lowered and len(products) > 2:
                products = products[2:3]

            elif len(products) > 1:
                return {
                    "reply": (
                        "Про яку саме модель розповісти? "
                        "Напишіть її назву або номер."
                    ),
                    "products": [],
                    "state": state
                }

    else:
        products = search_products(filters, limit=3)

    if not products:
        return {
            "reply": (
                "Не знайшов годинників, які підтверджено "
                "відповідають усім вашим вимогам. "
                "Можемо змінити бюджет, бренд "
                "або одну з характеристик."
            ),
            "products": [],
            "state": {
                **state,
                "filters": filters
            }
        }

    catalog_facts = [
        {
            "id": p["site_id"],
            "name": p["name"],
            "price": float(p["price"]),
            "brand": p.get("vendor"),
            "attributes": p.get("attributes") or {}
        }
        for p in products
    ]

    answer = gemini_request(
        SALES_PROMPT,
        {
            "message": message,
            "action": action,
            "history": history,
            "verified_products": catalog_facts
        }
    )

    if not answer:
        answer = (
            "Ось моделі з підтвердженими "
            "характеристиками та актуальними "
            "цінами з каталогу Zorro. "
            "Можу розповісти про кожну "
            "або порівняти їх."
        )

    new_state = {
        "filters": filters,
        "product_ids": (
            selected_ids
            if action in ("compare", "describe")
            else [
                str(p["site_id"])
                for p in products
            ]
        )
    }

    return {
        "reply": answer,
        "products": [
            product_card(p)
            for p in products
        ],
        "state": new_state
    }


# =====================================================
# API ENDPOINTS
# =====================================================

@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Zorro AI Consultant",
        "model": GEMINI_MODEL
    }


@app.get("/api/catalog-status")
def catalog_status():
    conn = db()

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT
                    COUNT(*),
                    COUNT(*) FILTER (WHERE available = TRUE)
                FROM zorro_products_v2
            """)

            total, available = cursor.fetchone()

            return {
                "status": "ok",
                "products": total,
                "available": available
            }
    finally:
        conn.close()


@app.post("/api/chat")
def chat(data: ChatInput):
    try:
        return handle_chat(data)

    except Exception:
        log.exception("Помилка роботи консультанта")

        return {
            "reply": (
                "Тимчасово не можу виконати пошук. "
                "Спробуйте ще раз."
            ),
            "products": [],
            "state": data.state
        }


@app.post("/api/callback")
def callback(data: CallbackInput):
    phone = data.phone.strip()

    if not phone:
        return {
            "status": "error",
            "reply": "Вкажіть номер телефону."
        }

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {
            "status": "error",
            "reply": (
                "Заявки на дзвінок ще не підключені. "
                "Будь ласка, скористайтеся контактами магазину."
            )
        }

    try:
        response = requests.post(
            (
                "https://api.telegram.org/bot"
                f"{TELEGRAM_BOT_TOKEN}/sendMessage"
            ),
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": (
                    f"Нова заявка Zorro AI\n"
                    f"Телефон: {phone}\n"
                    f"Повідомлення: {data.message}"
                )
            },
            timeout=10
        )

        response.raise_for_status()

        return {
            "status": "success",
            "reply": (
                "Дякуємо! Заявку передано менеджеру."
            )
        }

    except Exception:
        log.exception("Помилка Telegram")

        return {
            "status": "error",
            "reply": (
                "Не вдалося надіслати заявку. "
                "Зверніться через контакти магазину."
            )
        }


# =====================================================
# RUN
# =====================================================

# Render:
# uvicorn main:app --host 0.0.0.0 --port $PORT
