import os
import re
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

XML_URL = "https://support.best-time.biz/api/feed/drops/ua"

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

def sync_xml_to_db():
    conn = get_db_connection()
    if not conn:
        return 0

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(XML_URL, headers=headers, timeout=40)
        if response.status_code != 200:
            conn.close()
            return 0

        root = ET.fromstring(response.content)
        items = root.findall(".//product") or root.findall(".//offer") or root.findall(".//item")

        count = 0
        with conn.cursor() as cur:
            for item in items:
                vendor_code = item.get("id") or item.findtext("vendorCode") or item.findtext("code") or ""
                name = item.findtext("name") or item.findtext("title") or ""
                price_str = item.findtext("price") or "0"
                vendor = item.findtext("vendor") or item.findtext("brand") or ""
                description = item.findtext("description") or ""
                url = item.findtext("url") or ""
                image = item.findtext("picture") or item.findtext("image") or ""

                if description:
                    description = re.sub(r'<[^>]+>', ' ', description)
                    description = " ".join(description.split())

                try:
                    price = float(price_str.replace(" ", "").replace(",", "."))
                except ValueError:
                    price = 0.0

                if name and vendor_code:
                    cur.execute("""
                        INSERT INTO products (vendor_code, name, price, vendor, description, url, image)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (vendor_code) DO UPDATE 
                        SET name = EXCLUDED.name,
                            price = EXCLUDED.price,
                            vendor = EXCLUDED.vendor,
                            description = EXCLUDED.description,
                            url = EXCLUDED.url,
                            image = EXCLUDED.image;
                    """, (vendor_code, name, price, vendor, description, url, image))
                    count += 1

            conn.commit()
        conn.close()
        return count
    except Exception as e:
        print(f"Помилка імпорту XML: {e}")
        if conn:
            conn.close()
        return 0

def search_products_in_db(query: str, limit: int = 5):
    conn = get_db_connection()
    if not conn:
        return []

    words = re.findall(r'\w+', query.lower())
    stop_words = {"годинник", "годинники", "хочу", "порадь", "купити", "шукаю", "підкажи", "є", "до", "ціна"}
    search_terms = [w for w in words if w not in stop_words and len(w) > 2]

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if search_terms:
            conditions = []
            params = []
            for term in search_terms:
                conditions.append("(LOWER(name) LIKE %s OR LOWER(description) LIKE %s OR LOWER(vendor) LIKE %s)")
                param = f"%{term}%"
                params.extend([param, param, param])
            
            sql = f"SELECT name, price, vendor, description, url, image FROM products WHERE {' OR '.join(conditions)} LIMIT %s"
            params.append(limit)
            cur.execute(sql, tuple(params))
        else:
            cur.execute("SELECT name, price, vendor, description, url, image FROM products LIMIT %s", (limit,))
        
        results = cur.fetchall()
    conn.close()
    return results

def send_telegram_notification(phone: str, message: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    text = f"📞 *НОВА ЗАЯВКА НА ДЗВІНОК!*\n\n*Телефон:* `{phone}`"
    if message:
        text += f"\n*Коментар:* {message}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
        return True
    except Exception:
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
        "generationConfig": {"temperature": 0.3}
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    errors_log = []
    for api_ver in ["v1beta", "v1"]:
        for model in CANDIDATE_MODELS:
            clean_model = model.replace("models/", "")
            url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{clean_model}:generateContent?key={GEMINI_API_KEY}"
            try:
                res = requests.post(url, json=payload, timeout=12)
                if res.status_code == 200:
                    res_json = res.json()
                    return res_json['candidates'][0]['content']['parts'][0]['text']
                else:
                    err_data = res.json().get('error', {})
                    errors_log.append(f"[{api_ver}/{clean_model}]: {err_data.get('message', 'error')}")
            except Exception as e:
                errors_log.append(f"[{api_ver}/{clean_model}]: {str(e)}")

    return f"Усі моделі повернули помилку: {'; '.join(errors_log[:2])}"

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

@app.get("/api/sync")
def trigger_sync():
    """Ендпоїнт для завантаження товарів із XML у базу PostgreSQL"""
    count = sync_xml_to_db()
    return {"status": "success", "imported_products": count}

@app.post("/api/callback")
async def callback(data: CallbackRequest):
    if not data.phone:
        return {"status": "error", "reply": "Введіть номер телефону."}
    if send_telegram_notification(data.phone, data.message):
        return {"status": "success", "reply": "Дякуємо! Менеджер зателефонує вам найближчим часом."}
    return {"status": "error", "reply": "Помилка відправки в Telegram."}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    matched_products = search_products_in_db(data.message, limit=4)
    
    catalog_context = ""
    if matched_products:
        lines = []
        for p in matched_products:
            short_desc = p['description'][:250] + "..." if p['description'] else ""
            lines.append(f"Модель: {p['name']} | Ціна: {p['price']} грн | Бренд: {p['vendor']} | Опис: {short_desc}")
        catalog_context = "\n".join(lines)
    else:
        catalog_context = "Точних збігів за ключовими словами не знайдено."

    system_instruction = f"""
    Ти — досвідчений, привітний та ввічливий консультант магазину смарт-годинників ZORRO.
    
    ОСЬ ЗНАЙДЕНІ ТОВАРИ З БАЗИ ПІД ЗАПИТ КЛІЄНТА:
    {catalog_context}
    
    ПРАВИЛА РОБОТИ:
    1. Відповідай спираючись ЛИШЕ на знайдені товари. Ніколи не вигадуй характеристик чи цін.
    2. Якщо знайдені товари підходять під запит — коротко розкажи про них, назви ціну та головну перевагу.
    3. Якщо товари не відповідають точному запиту, ввічливо уточни деталі (наприклад: потрібен годинник для спорту, з можливістю дзвінків чи класичний?).
    4. Якщо клієнт вагається — запропонуй залишити номер телефону для дзвінка менеджера.
    5. Спілкуйся українською мовою, стисло, приємно та по суті.
    """

    history_formatted = [{"role": m.role, "content": m.content} for m in data.history] if data.history else []
    reply = call_gemini_api(data.message, history_formatted, system_instruction)
    return {"reply": reply}
    
