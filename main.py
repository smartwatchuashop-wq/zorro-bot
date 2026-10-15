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

def diverse_smart_search(user_query: str, limit: int = 3):
    conn = get_db_connection()
    if not conn:
        return []

    parse_prompt = f"""
    Проаналізуй запит покупця інтернет-магазину годинників: "{user_query}"
    Виділи параметри строго у форматі JSON (тільки чистий JSON без коментарів):
    {{
        "max_price": число або null,
        "is_kids": true/false,
        "target_gender": "men" | "women" | "kids" | "unisex" | null,
        "search_words": ["список", "ключових", "слів"]
    }}
    """
    ai_json = call_gemini(parse_prompt, temperature=0.1)
    
    max_price = None
    is_kids = False
    target_gender = None
    search_words = []

    try:
        match = re.search(r'\{.*\}', ai_json, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            max_price = parsed.get("max_price")
            is_kids = parsed.get("is_kids", False)
            target_gender = parsed.get("target_gender")
            search_words = parsed.get("search_words") or []
    except Exception:
        pass

    if not search_words:
        clean_words = re.findall(r'\w+', user_query.lower())
        stop_words = {"годинник", "годинники", "порадь", "хочу", "який", "які", "підкажи", "купити", "цікаві", "моделі", "надійні"}
        search_words = [w for w in clean_words if len(w) > 2 and w not in stop_words]

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where_clauses = ["image IS NOT NULL AND image != ''"]
            query_params = []

            if max_price:
                where_clauses.append("price <= %s")
                query_params.append(float(max_price))

            where_str = " AND ".join(where_clauses)
            
            sql = f"""
                SELECT vendor_code, name, price, vendor, description, url, image
                FROM products
                WHERE {where_str}
                ORDER BY price DESC
                LIMIT 60;
            """
            cur.execute(sql, tuple(query_params))
            candidates = cur.fetchall()

            scored_list = []
            for item in candidates:
                name_l = (item['name'] or '').lower()
                desc_l = (item['description'] or '').lower()
                vend_l = (item['vendor'] or '').lower()
                score = 0

                for word in search_words:
                    w = word.lower()
                    if w in name_l:
                        score += 5
                    if w in vend_l:
                        score += 3
                    if w in desc_l:
                        score += 1

                if not is_kids:
                    if 'дитяч' in name_l or 'для дітей' in desc_l or 'game' in name_l:
                        score -= 10

                if target_gender == "men" and ('чоловіч' in name_l or 'чоловіч' in desc_l):
                    score += 4
                elif target_gender == "women" and ('жіноч' in name_l or 'жіноч' in desc_l):
                    score += 4

                scored_list.append((score, item))

            scored_list.sort(key=lambda x: x[0], reverse=True)

            selected = []
            seen_vendors = set()

            for score, item in scored_list:
                v = (item.get('vendor') or 'zorro').lower().strip()
                if v not in seen_vendors:
                    selected.append(item)
                    seen_vendors.add(v)
                if len(selected) >= limit:
                    break

            if len(selected) < limit:
                for score, item in scored_list:
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
    short_desc = p['description'][:100] + "..." if p.get('description') else "Оригінальний годинник із гарантією 12 міс."
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
    text = f"📞 <b>НОВА ЗАЯВКА З ЧАТУ ZORRO!</b>\n\n<b>Телефон:</b> <code>{phone}</code>"
    if message:
        text += f"\n<b>Діалог:</b> {message}"
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
        return {"status": "error", "reply": "Будь ласка, вкажіть ваш номер телефону."}
    send_telegram_notification(data.phone, data.message)
    return {
        "status": "success", 
        "reply": "Дякуємо! Номер збережено, наш консультант зателефонує вам найближчим часом. Яку саме модель для вас зарезервувати?"
    }

@app.post("/api/chat")
async def chat(data: ChatRequest):
    matched_products = diverse_smart_search(data.message, limit=3)
    
    catalog_context = ""
    cards_html = []
    
    for p in matched_products:
        short_desc = p['description'][:95] + "..." if p.get('description') else "Оригінальний годинник."
        catalog_context += f"• Модель: {p['name']} (Бренд: {p.get('vendor')}) | Ціна: {p['price']} грн | Опис: {short_desc}\n"
        cards_html.append(render_rozetka_card(p))

    chips_html = """
    <div style="display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; padding-top:6px; border-top:1px solid #f0f0f0;">
      <button class="ai-chip" data-msg="Розкажи детальніше опис та характеристики">📄 Опис товару</button>
      <button class="ai-chip" data-msg="Порівняй ці моделі між собою">⚖️ Порівняти моделі</button>
      <button class="ai-chip" data-msg="Покажи інші схожі варіанти">🔍 Схожі товари</button>
      <button class="ai-chip" data-msg="Передзвоніть мені для оформлення замовлення">📞 Замовити дзвінок</button>
    </div>
    """

    system_instruction = f"""
    Ти — професійний онлайн-консультант магазину годинників ZORRO (zorro.com.ua).
    Поточний товар на сторінці: {data.current_product or "каталог"}.

    ПІДІБРАНІ ТОВАРИ ДЛЯ ВІДПОВІДІ:
    {catalog_context if catalog_context else "Точних збігів не знайдено."}

    ПРАВИЛА:
    1. Почни відповідь із поради покупцеві (1-2 лаконічні речення), чому ці варіанти підходять під його запит.
    2. У тексті НЕ пиши ціни й посилання — великі картки Rozetka додаються системою нижче.
    3. В кінці відповіді постав одне ввічливе відкрите питання для підтримки діалогу.
    4. Мова — виключно українська.
    """

    history_formatted = [{"role": m.role, "content": m.content} for m in data.history] if data.history else []
    ai_reply = call_gemini(
        prompt_text=f"Запит клієнта: {data.message}\nІсторія: {json.dumps(history_formatted, ensure_ascii=False)}",
        system_instruction=system_instruction,
        temperature=0.3
    )

    full_output = f"<div style='line-height:1.4;'>{ai_reply}</div>"
    if cards_html:
        full_output += "".join(cards_html)
    full_output += chips_html

    return {"reply": full_output}
