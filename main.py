import os
import re
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

XML_URL = "https://zorro.com.ua/content/export/c929bc168711aedbbcad10f5bb726db0.xml?1791864654444"

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
        print(f"Помилка підключення до БД: {e}")
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
                vendor_code = offer.get('id') or (offer.findtext('vendorCode') or '').strip()
                name = (offer.findtext('name') or offer.findtext('model') or '').strip()
                price_str = offer.findtext('price') or '0'
                try:
                    price = float(price_str.replace(',', '.'))
                except ValueError:
                    price = 0.0

                vendor = (offer.findtext('vendor') or '').strip()
                raw_desc = offer.findtext('description') or ''
                description = clean_html(raw_desc)
                
                # Забираємо прямий URL товару, який сформував Хорошоп
                url = (offer.findtext('url') or '').strip()
                if not url or url.rstrip('/') == 'https://zorro.com.ua':
                    url = f"https://zorro.com.ua/search/?search_string={urllib.parse.quote_plus(name)}"

                image = (offer.findtext('picture') or offer.findtext('image') or '').strip()

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

def search_products_in_db(query: str, limit: int = 3):
    conn = get_db_connection()
    if not conn:
        return []

    words = re.findall(r'\w+', query.lower())
    stop_words = {"годинник", "годинники", "хочу", "порадь", "купити", "шукаю", "підкажи", "є", "до", "ціна", "з", "для"}
    search_terms = [w for w in words if w not in stop_words and len(w) > 2]

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if search_terms:
            conditions = []
            params = []
            for term in search_terms:
                conditions.append("(LOWER(name) LIKE %s OR LOWER(description) LIKE %s OR LOWER(vendor) LIKE %s)")
                param = f"%{term}%"
                params.extend([param, param, param])
            
            sql = f"SELECT vendor_code, name, price, vendor, description, url, image FROM products WHERE {' OR '.join(conditions)} LIMIT %s"
            params.append(limit)
            cur.execute(sql, tuple(params))
        else:
            cur.execute("SELECT vendor_code, name, price, vendor, description, url, image FROM products ORDER BY RANDOM() LIMIT %s", (limit,))
        
        results = cur.fetchall()
    conn.close()
    return results

def send_telegram_notification(phone: str, message: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    text = f"📞 <b>НОВА ЗАЯВКА НА ДЗВІНОК З ЧАТУ ZORRO!</b>\n\n<b>Телефон:</b> <code>{phone}</code>"
    if message:
        text += f"\n<b>Контекст діалогу:</b> {message}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
        return True
    except Exception as e:
        print(f"Помилка надсилання в Telegram: {e}")
        return False

def call_gemini_api(prompt_text: str, history_list: list = [], system_instruction: str = ""):
    if not GEMINI_API_KEY:
        return "API-ключ GEMINI_API_KEY не налаштовано."

    contents = []
    if history_list:
        for msg in history_list[-6:]:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})
            
    contents.append({"role": "user", "parts": [{"text": prompt_text}]})

    payload = {
        "contents": contents,
        "generationConfig": {"temperature": 0.4}
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    errors_log = []
    for api_ver in ["v1beta", "v1"]:
        for model in CANDIDATE_MODELS:
            clean_model = model.replace("models/", "")
            url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{clean_model}:generateContent?key={GEMINI_API_KEY}"
            try:
                res = requests.post(url, json=payload, timeout=14)
                if res.status_code == 200:
                    res_json = res.json()
                    return res_json['candidates'][0]['content']['parts'][0]['text']
                else:
                    err_data = res.json().get('error', {})
                    errors_log.append(f"[{api_ver}/{clean_model}]: {err_data.get('message', 'error')}")
            except Exception as e:
                errors_log.append(f"[{api_ver}/{clean_model}]: {str(e)}")

    return f"Помилка зв'язку: {'; '.join(errors_log[:1])}"

class MessageItem(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
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
    reply_text = (
        "Чудово! Номер отримано, наш менеджер зв'яжеться з вами найближчим часом. "
        "Підкажіть, яка з моделей вас зацікавила найбільше, або який стиль годинника ви шукаєте?"
    )
    return {"status": "success", "reply": reply_text}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    matched_products = search_products_in_db(data.message, limit=3)
    
    catalog_context = ""
    products_html = ""
    
    if matched_products:
        lines = []
        html_cards = []
        for p in matched_products:
            prod_url = (p.get('url') or '').strip()
            if not prod_url or prod_url.rstrip('/') == 'https://zorro.com.ua':
                prod_url = f"https://zorro.com.ua/search/?search_string={urllib.parse.quote_plus(p['name'])}"

            short_desc = p['description'][:140] + "..." if p['description'] else "Сучасний та стильний годинник."
            lines.append(f"Модель: {p['name']} | Ціна: {p['price']} грн | Переваги: {short_desc}")
            
            card = (
                f"👉 <b>{p['name']}</b> — {p['price']} грн<br>"
                f"{short_desc}<br>"
                f"<a href=\"{prod_url}\" target=\"_blank\" style=\"color:#0070f3; font-weight:bold; text-decoration:underline;\">👉 Переглянути товар на сайті</a>"
            )
            html_cards.append(card)
            
        catalog_context = "\n---\n".join(lines)
        products_html = "<br><br>" + "<br><br>".join(html_cards)
    else:
        catalog_context = "Товарів не знайдено за точним запитом. Запропонуй звернутися до менеджера або уточнити критерії пошуку."

    system_instruction = f"""
    Ти — професійний онлайн-консультант інтернет-магазину годинників ZORRO (zorro.com.ua).
    Твоє завдання — ввічливо та експертно проконсультувати клієнта щодо вибору годинника.

    ЗНАЙДЕНІ ТОВАРИ:
    {catalog_context}

    ВАЖЛИВІ ПРАВИЛА:
    1. Не пиши жодних HTML-посилань у своїй відповіді — список карток із посиланнями буде додано автоматично!
    2. Коротко поясни, чому ці моделі чудово підійдуть під запит покупця.
    3. Обов'язково постав 1-2 влучні запитання для виявлення потреби (наприклад: для себе чи на подарунок, який стиль подобається більше, чи потрібні смарт-функції).
    4. Запропонуй залишити номер телефону для бронювання за спеціальною ціною або дзвінка менеджера.
    5. Спілкуйся виключно українською мовою.
    """

    history_formatted = [{"role": m.role, "content": m.content} for m in data.history] if data.history else []
    ai_reply = call_gemini_api(data.message, history_formatted, system_instruction)
    
    final_reply = ai_reply + products_html if products_html else ai_reply
    return {"reply": final_reply}
