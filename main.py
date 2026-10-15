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

                # Забираємо зображення з фіду
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

def universal_smart_search(user_query: str, limit: int = 3):
    """ШІ аналізує абсолютно будь-які запити покупців без жорстких обмежень"""
    conn = get_db_connection()
    if not conn:
        return []

    parse_prompt = f"""
    Проаналізуй запит покупця інтернет-магазину годинників: "{user_query}"
    Сформуй JSON (чистий JSON без бектиків):
    {{
        "max_price": число або null,
        "features": ["окремі", "пошукові", "слова", "синоніми", "характеристики"]
    }}
    Приклад: "з ліхтариком і тиском" -> "features": ["ліхтар", "flash", "тиск", "тонометр", "пульс", "smart"]
    """
    ai_json = call_gemini(parse_prompt, temperature=0.1)
    
    max_price = None
    features = []
    try:
        match = re.search(r'\{.*\}', ai_json, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            max_price = data.get("max_price")
            features = data.get("features") or []
    except Exception:
        pass

    if not features:
        features = [w for w in re.findall(r'\w+', user_query.lower()) if len(w) > 2 and w not in ["годинник", "годинники", "хочу", "порадь"]]

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        score_parts = []
        params = []
        
        for feat in features[:8]:
            p = f"%{feat.lower()}%"
            score_parts.append(f"""
                (CASE WHEN LOWER(name) LIKE %s THEN 3 
                      WHEN LOWER(description) LIKE %s THEN 1 
                      WHEN LOWER(vendor) LIKE %s THEN 2
                      ELSE 0 END)
            """)
            params.extend([p, p, p])

        score_sql = " + ".join(score_parts) if score_parts else "0"
        
        where_parts = []
        where_params = []
        if max_price:
            where_parts.append("price <= %s")
            where_params.append(float(max_price))

        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        
        # Сортуємо: спочатку максимум збігів за ознаками, потім наявність картинки
        full_params = params + where_params + [limit]
        query = f"""
            SELECT vendor_code, name, price, vendor, description, url, image,
                   ({score_sql}) as score
            FROM products
            {where_sql}
            ORDER BY score DESC, (image IS NOT NULL AND image != '') DESC, price ASC
            LIMIT %s;
        """
        cur.execute(query, tuple(full_params))
        results = cur.fetchall()

        if not results:
            cur.execute("SELECT vendor_code, name, price, vendor, description, url, image FROM products ORDER BY (image != '') DESC, RANDOM() LIMIT %s", (limit,))
            results = cur.fetchall()

    conn.close()
    return results

def render_rozetka_card(p: dict) -> str:
    """Генерує графічну картку у стилі Rozetka AI з кнопками 'Схожі товари' та 'Опис товару'"""
    prod_url = (p.get('url') or '').strip()
    if not prod_url or prod_url.rstrip('/') == 'https://zorro.com.ua':
        prod_url = f"https://zorro.com.ua/katalog/search/?q={urllib.parse.quote_plus(p['name'])}"

    img_src = p.get('image')
    if not img_src or not img_src.startswith('http'):
        img_src = 'https://zorro.com.ua/content/images/no-image.png'

    short_desc = p['description'][:110] + "..." if p.get('description') else "Оригінальний годинник з гарантією."
    price_val = int(p['price']) if p.get('price') else 0
    safe_name = p['name'].replace('"', '&quot;')

    return f"""
    <div style="background:#ffffff; border:1px solid #e2e5e8; border-radius:12px; padding:10px; margin-top:8px; box-shadow:0 1px 4px rgba(0,0,0,0.05); text-align:left;">
      <div style="display:flex; gap:10px; align-items:center;">
        <a href="{prod_url}" target="_self" style="flex-shrink:0;">
          <img src="{img_src}" alt="{safe_name}" style="width:62px; height:62px; object-fit:contain; border-radius:8px; background:#f9fafb; border:1px solid #f0f0f0;">
        </a>
        <div style="flex:1; min-width:0;">
          <a href="{prod_url}" target="_self" style="color:#222; text-decoration:none; font-weight:600; font-size:12.5px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;">
            {p['name']}
          </a>
          <div style="display:flex; align-items:center; justify-content:space-between; margin-top:5px;">
            <span style="font-size:15px; font-weight:700; color:#111;">{price_val} ₴</span>
            <a href="{prod_url}" target="_self" style="background:#00a046; color:#ffffff; padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; text-decoration:none; display:inline-block;">Купити</a>
          </div>
        </div>
      </div>
      
      <!-- Текст опису/переваг -->
      <div style="font-size:11.5px; color:#555; margin-top:7px; line-height:1.35; border-top:1px dashed #eee; padding-top:6px;">
        {short_desc}
      </div>

      <!-- Кнопки в стилі Rozetka: Схожі товари та Опис товару -->
      <div style="display:flex; gap:8px; margin-top:6px; padding-top:4px;">
        <span class="ai-card-action" data-action="similar" data-name="{safe_name}" style="cursor:pointer; font-size:11px; color:#777; display:flex; align-items:center; gap:3px;">
          🔍 Схожі товари
        </span>
        <span class="ai-card-action" data-action="desc" data-name="{safe_name}" style="cursor:pointer; font-size:11px; color:#777; display:flex; align-items:center; gap:3px;">
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
        "reply": "Дякуємо! Номер зафіксовано, наш менеджер зв'яжеться з вами найближчим часом для консультації. Яку модель для вас відкласти або перевірити наявність?"
    }

@app.post("/api/chat")
async def chat(data: ChatRequest):
    matched_products = universal_smart_search(data.message, limit=3)
    
    catalog_context = ""
    cards_html = []
    
    for p in matched_products:
        short_desc = p['description'][:100] + "..." if p.get('description') else "Оригінальний годинник із гарантією."
        catalog_context += f"• Модель: {p['name']} | Ціна: {p['price']} грн | Опис: {short_desc}\n"
        cards_html.append(render_rozetka_card(p))

    system_instruction = f"""
    Ти — професійний, турботливий продавець-консультант інтернет-магазину годинників ZORRO (zorro.com.ua).
    Поточний товар на сторінці (якщо є): {data.current_product or "головна сторінка/каталог"}.

    ТОВАРИ З КАТАЛОГУ:
    {catalog_context if catalog_context else "Точного збігу за всіма словами немає."}

    ПРАВИЛА РОБОТИ ПРОДАВЦЯ:
    1. Не пиши ціни і посилання вручну в тексті — графічні картки Rozetka додаються автоматично.
    2. Якщо клієнт шукає складну або рідкісну комбінацію (наприклад, ліхтарик + тиск):
       - Не кажи "немає"!
       - Поясни, що це рідкісне поєднання функцій, тому ти підібрав найкращі варіанти окремо: моделі для контролю тиску та смарт-функцій, і надійні тактичні моделі з ліхтариком.
       - Запитай покупця, що для нього в пріоритеті або запропонуй швидку консультацію менеджера.
    3. Відповідай дружньо, коротко (2-3 речення), українською мовою.
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

    return {"reply": full_output}
