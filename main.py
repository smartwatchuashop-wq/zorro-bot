import os
import re
import json
import urllib.parse
import xml.etree.ElementTree as ET
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

XML_URL = "https://zorro.com.ua/content/export/c929bc168711aedbbcad10f5bb726db0.xml"

CANDIDATE_MODELS = [
    "gemini-2.5-flash",
    "gemini-3.6-flash",
    "gemini-2.5-pro",
    "gemini-1.5-flash-8b"
]

def get_db_connection():
    if not DATABASE_URL:
        return None
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"Помилка БД: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                vendor_code VARCHAR(100) UNIQUE,
                name TEXT NOT NULL,
                price NUMERIC(10, 2),
                vendor VARCHAR(100),
                description TEXT,
                url TEXT,
                image TEXT
            );
        """)
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS image TEXT;")
        conn.commit()
    conn.close()

init_db()

def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    cleanr = re.compile(r'<.*?>')
    return re.sub(cleanr, '', raw_html).strip()

def sync_products_from_xml():
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "message": "БД не підключена"}

    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(XML_URL, headers=headers, timeout=60)
        response.raise_for_status()

        root = ET.fromstring(response.content)
        offers = root.findall('.//offer')
        if not offers:
            offers = root.findall('.//item')

        count = 0
        with conn.cursor() as cur:
            for offer in offers:
                internal_id = offer.get('id') or ''
                vendor_code = internal_id or (offer.findtext('vendorCode') or '').strip()
                name = (offer.findtext('name') or offer.findtext('model') or '').strip()
                price_str = offer.findtext('price') or '0'
                try:
                    price = float(price_str.replace(',', '.'))
                except ValueError:
                    price = 0.0

                vendor = (offer.findtext('vendor') or '').strip()
                raw_desc = offer.findtext('description') or ''
                description = clean_html(raw_desc)
                
                raw_url = (offer.findtext('url') or '').strip()
                if raw_url.startswith('http') and raw_url.rstrip('/') != 'https://zorro.com.ua':
                    url = raw_url
                elif raw_url.startswith('/'):
                    url = f"https://zorro.com.ua{raw_url}"
                elif name:
                    url = f"https://zorro.com.ua/katalog/search/?q={urllib.parse.quote_plus(name)}"
                else:
                    url = "https://zorro.com.ua"

                pic = offer.find('picture')
                if pic is None:
                    pic = offer.find('image')
                image = pic.text.strip() if (pic is not None and pic.text) else ''

                if not vendor_code:
                    vendor_code = name

                if name:
                    cur.execute("""
                        INSERT INTO products (vendor_code, name, price, vendor, description, url, image)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (vendor_code) DO UPDATE SET
                            name = EXCLUDED.name,
                            price = EXCLUDED.price,
                            vendor = EXCLUDED.vendor,
                            description = EXCLUDED.description,
                            url = EXCLUDED.url,
                            image = EXCLUDED.image;
                    """, (vendor_code, name, price, vendor, description, url, image))
                    count += 1

            conn.commit()
        conn.close()
        return {"status": "success", "imported": count}
    except Exception as e:
        if conn:
            conn.close()
        return {"status": "error", "message": str(e)}

def call_gemini(prompt_text: str, system_instruction: str = "", temperature: float = 0.2):
    if not GEMINI_API_KEY:
        return ""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": temperature}
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    for api_ver in ["v1beta", "v1"]:
        for model in CANDIDATE_MODELS:
            clean_model = model.replace("models/", "")
            url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{clean_model}:generateContent?key={GEMINI_API_KEY}"
            try:
                res = requests.post(url, json=payload, timeout=12)
                if res.status_code == 200:
                    return res.json()['candidates'][0]['content']['parts'][0]['text']
            except Exception:
                continue
    return ""

def strict_semantic_search(user_query: str, limit: int = 3):
    """
    Семантичний розбір з суворою фільтрацією специфічних вимог (дзвінки, смарт, механіка, стать, бюджет).
    """
    conn = get_db_connection()
    if not conn:
        return []

    parse_prompt = f"""
    Проаналізуй запит покупця годинників ZORRO: "{user_query}"
    Поверни строгий JSON (чистий JSON без коментарів):
    {{
        "requires_calls": true/false (якщо клієнту потрібні дзвінки, розмови, bluetooth-виклик чи sim-карта),
        "only_smart": true/false (якщо запитують виключно смарт-годинники),
        "only_mechanical": true/false (якщо хочуть виключно механіку),
        "max_price": число або null,
        "is_kids": true/false,
        "gender": "men" | "women" | "kids" | null,
        "search_keywords": ["список", "основних", "слів"]
    }}
    """
    ai_json = call_gemini(parse_prompt, temperature=0.1)

    requires_calls = False
    only_smart = False
    only_mechanical = False
    max_price = None
    is_kids = False
    gender = None
    search_keywords = []

    try:
        match = re.search(r'\{.*\}', ai_json, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            requires_calls = parsed.get("requires_calls", False)
            only_smart = parsed.get("only_smart", False) or requires_calls
            only_mechanical = parsed.get("only_mechanical", False)
            max_price = parsed.get("max_price")
            is_kids = parsed.get("is_kids", False)
            gender = parsed.get("gender")
            search_keywords = parsed.get("search_keywords") or []
    except Exception:
        pass

    if not search_keywords:
        words = re.findall(r'\w+', user_query.lower())
        stop_words = {"годинник", "годинники", "які", "який", "порадь", "хочу", "підкажи", "моделі"}
        search_keywords = [w for w in words if len(w) > 2 and w not in stop_words]

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Витягуємо товари тільки з наявними фотографіями
            where_sql = ["image IS NOT NULL AND image != ''"]
            sql_params = []

            # 1. ЖОРСТКИЙ ФІЛЬТР: якщо потрібні дзвінки — відсікаємо все, крім моделей з підтримкою дзвінків
            if requires_calls:
                where_sql.append("""(
                    (LOWER(description) LIKE '%дзвін%' OR LOWER(description) LIKE '%розмов%' OR LOWER(description) LIKE '%виклики%' OR LOWER(description) LIKE '%мікрофон%' OR LOWER(description) LIKE '%блютуз%' OR LOWER(description) LIKE '%bluetooth%')
                    AND (LOWER(name) LIKE '%smart%' OR LOWER(name) LIKE '%смарт%' OR LOWER(description) LIKE '%smart%' OR LOWER(description) LIKE '%смарт%')
                )""")
            elif only_smart:
                where_sql.append("(LOWER(name) LIKE '%smart%' OR LOWER(name) LIKE '%смарт%' OR LOWER(description) LIKE '%смарт%')")
            elif only_mechanical:
                where_sql.append("(LOWER(name) LIKE '%механ%' OR LOWER(name) LIKE '%skeleton%' OR LOWER(description) LIKE '%механ%')")

            if max_price:
                where_sql.append("price <= %s")
                sql_params.append(float(max_price))

            final_where = "WHERE " + " AND ".join(where_sql)

            # Беремо кандидатів
            cur.execute(f"SELECT vendor_code, name, price, vendor, description, url, image FROM products {final_where} ORDER BY price DESC LIMIT 80;", tuple(sql_params))
            candidates = cur.fetchall()

            # Якщо жорсткий фільтр не дав результатів (наприклад, фід не має слова "дзвінки" в описі), шукаємо кращі смарт-моделі
            if not candidates and (requires_calls or only_smart):
                cur.execute("SELECT vendor_code, name, price, vendor, description, url, image FROM products WHERE (LOWER(name) LIKE '%smart%' OR LOWER(name) LIKE '%смарт%') AND image != '' ORDER BY price DESC LIMIT 40;")
                candidates = cur.fetchall()

            # Ранжуємо в Python за релевантністю
            scored = []
            for item in candidates:
                name_l = (item['name'] or '').lower()
                desc_l = (item['description'] or '').lower()
                vend_l = (item['vendor'] or '').lower()
                score = 0

                for kw in search_keywords:
                    k = kw.lower()
                    if k in name_l:
                        score += 6
                    if k in vend_l:
                        score += 3
                    if k in desc_l:
                        score += 2

                if requires_calls:
                    if 'дзвін' in desc_l or 'виклики' in desc_l or 'розмов' in desc_l:
                        score += 10

                if not is_kids and ('дитяч' in name_l or 'game' in name_l or 'для дітей' in desc_l):
                    score -= 15

                if gender == "men" and ('чоловіч' in name_l or 'чоловіч' in desc_l):
                    score += 4
                elif gender == "women" and ('жіноч' in name_l or 'жіноч' in desc_l):
                    score += 4

                scored.append((score, item))

            scored.sort(key=lambda x: x[0], reverse=True)

            # Диверсифікація брендів ТІЛЬКИ СЕРЕД РЕЛЕВАНТНИХ товарів
            selected = []
            seen_vendors = set()

            for score, item in scored:
                v = (item.get('vendor') or 'zorro').lower().strip()
                if v not in seen_vendors:
                    selected.append(item)
                    seen_vendors.add(v)
                if len(selected) >= limit:
                    break

            # Якщо різних брендів не вистачило — добираємо моделі того ж бренду (наприклад, інші смарт UWatch), але не механіку!
            if len(selected) < limit:
                for score, item in scored:
                    if item not in selected:
                        selected.append(item)
                    if len(selected) >= limit:
                        break

            conn.close()
            return selected
    except Exception as e:
        print(f"Помилка пошуку: {e}")
        if conn:
            conn.close()
        return []

def render_rozetka_card(p: dict) -> str:
    prod_url = (p.get('url') or '').strip()
    if not prod_url or prod_url.rstrip('/') == 'https://zorro.com.ua':
        prod_url = f"https://zorro.com.ua/katalog/search/?q={urllib.parse.quote_plus(p['name'])}"

    img_src = p.get('image') or 'https://zorro.com.ua/content/images/no-image.png'
    short_desc = p['description'][:110] + "..." if p.get('description') else "Оригінальний годинник з офіційною гарантією 12 місяців."
    price_val = int(p['price']) if p.get('price') else 0
    brand_label = p.get('vendor') or 'ZORRO'
    safe_name = p['name'].replace('"', '&quot;')

    return f"""
    <div style="box-sizing:border-box; width:100%; background:#ffffff; border:1px solid #e2e5e8; border-radius:12px; padding:12px; margin-top:10px; box-shadow:0 1px 4px rgba(0,0,0,0.06); text-align:left;">
      <div style="display:flex; gap:12px; align-items:flex-start;">
        <a href="{prod_url}" target="_self" style="flex-shrink:0;">
          <img src="{img_src}" alt="{safe_name}" style="width:78px; height:78px; object-fit:contain; border-radius:8px; background:#f9fafb; border:1px solid #eee;">
        </a>
        <div style="flex:1; min-width:0;">
          <div style="font-size:10.5px; text-transform:uppercase; color:#888; font-weight:700; letter-spacing:0.4px; margin-bottom:2px;">{brand_label}</div>
          <a href="{prod_url}" target="_self" style="color:#222; text-decoration:none; font-weight:600; font-size:13.5px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;">
            {p['name']}
          </a>
          <div style="display:flex; align-items:center; justify-content:space-between; margin-top:8px;">
            <span style="font-size:16px; font-weight:700; color:#111;">{price_val} ₴</span>
            <a href="{prod_url}" target="_self" style="background:#00a046; color:#ffffff; padding:5px 12px; border-radius:6px; font-size:12px; font-weight:600; text-decoration:none; display:inline-block;">Купити</a>
          </div>
        </div>
      </div>
      
      <div style="font-size:11.5px; color:#555; margin-top:8px; line-height:1.35; border-top:1px dashed #e5e5e5; padding-top:6px;">
        {short_desc}
      </div>

      <div style="display:flex; gap:14px; margin-top:8px; padding-top:2px;">
        <span class="ai-card-action" data-action="similar" data-name="{safe_name}" style="cursor:pointer; font-size:11.5px; color:#00a046; font-weight:500; display:flex; align-items:center; gap:4px;">
          🔍 Схожі товари
        </span>
        <span class="ai-card-action" data-action="desc" data-name="{safe_name}" style="cursor:pointer; font-size:11.5px; color:#666; display:flex; align-items:center; gap:4px;">
          📄 Опис товару
        </span>
      </div>
    </div>
    """

def send_telegram_notification(phone: str, message: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    text = f"📞 <b>ЗАМОВЛЕННЯ ДЗВІНКА З ЧАТУ ZORRO!</b>\n\n<b>Номер покупця:</b> <code>{phone}</code>"
    if message:
        text += f"\n<b>Запит або товар:</b> {message}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
        return True
    except Exception:
        return False

class MessageItem(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    current_product: Optional[str] = None
    history: Optional[List[MessageItem]] = []

class CallbackRequest(BaseModel):
    phone: str
    message: Optional[str] = ""

@app.get("/")
def root():
    return {"status": "ok", "database_connected": DATABASE_URL is not None}

@app.get("/api/sync-catalog")
def sync_catalog():
    return sync_products_from_xml()

@app.post("/api/callback")
async def callback(data: CallbackRequest):
    if not data.phone:
        return {"status": "error", "reply": "Будь ласка, введіть номер телефону."}
    send_telegram_notification(data.phone, data.message)
    return {
        "status": "success", 
        "reply": f"Дякуємо! Номер {data.phone} успішно передано менеджеру. Ми зателефонуємо вам протягом 10 хвилин для консультації!"
    }

@app.post("/api/chat")
async def chat(data: ChatRequest):
    matched_products = strict_semantic_search(data.message, limit=3)
    
    catalog_context = ""
    cards_html = []
    
    for p in matched_products:
        short_desc = p['description'][:100] + "..." if p.get('description') else "Оригінальний годинник."
        catalog_context += f"• Модель: {p['name']} (Бренд: {p.get('vendor')}) | Ціна: {p['price']} грн | Опис: {short_desc}\n"
        cards_html.append(render_rozetka_card(p))

    chips_html = """
    <div style="display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; padding-top:6px; border-top:1px solid #f0f0f0;">
      <button class="ai-chip" data-msg="Розкажи детальніше опис та характеристики цих моделей">📄 Опис моделей</button>
      <button class="ai-chip" data-msg="Порівняй ці моделі між собою: автономність та захист">⚖️ Порівняти моделі</button>
      <button class="ai-chip" data-action="call-modal" style="background:#00a046; color:#fff; border-color:#00a046; font-weight:600;">📞 Замовити дзвінок менеджера</button>
    </div>
    """

    system_instruction = f"""
    Ти — професійний онлайн-консультант магазину годинників ZORRO (zorro.com.ua).
    Поточний товар на сторінці: {data.current_product or "каталог"}.

    ПІДІБРАНІ ВАРІАНТИ З КАТАЛОГУ:
    {catalog_context if catalog_context else "Точних збігів не знайдено."}

    СУВОРІ ПРАВИЛА:
    1. Якщо запит стосується функції дзвінків, смарт-годинників чи зв'язку — консультуй ВИКЛЮЧНО щодо цих можливостей. Не пропонуй класику чи механіку, якщо клієнт шукає зв'язок!
    2. У тексті відповіді НЕ пиши ціни, назви та посилання — великі графічні картки додаються автоматично нижче.
    3. Поясни у 1–2 лаконічних реченнях, чим зручні ці моделі для дзвінків та що вони вміють.
    4. В кінці відповіді постав відкрите коротке питання: наприклад, чи важливий розмір екрана або захист від води.
    5. Мова спілкування — виключно українська.
    """

    history_formatted = [{"role": m.role, "content": m.content} for m in data.history] if data.history else []
    ai_reply = call_gemini(
        prompt_text=f"Запит клієнта: {data.message}\nІсторія: {json.dumps(history_formatted, ensure_ascii=False)}",
        system_instruction=system_instruction,
        temperature=0.2
    )

    full_output = f"<div style='line-height:1.4;'>{ai_reply}</div>"
    if cards_html:
        full_output += "".join(cards_html)
    full_output += chips_html

    return {"reply": full_output}
