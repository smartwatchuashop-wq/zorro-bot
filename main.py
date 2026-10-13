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
    text = f"📞 <b>НОВА ЗАЯВКА НА ДЗВІНОК З ЧАТУ!</b>\n\n<b>Телефон:</b> <code>{phone}</code>"
    if message:
        text += f"\n<b>Повідомлення клієнта:</b> {message}"
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

    return f"Помилка Gemini: {'; '.join(errors_log[:1])}"

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

@app.post("/api/callback")
async def callback(data: CallbackRequest):
    if not data.phone:
        return {"status": "error", "reply": "Введіть номер телефону."}
    
    sent = send_telegram_notification(data.phone, data.message)
    if sent:
        return {"status": "success", "reply": "Дякуємо! Номер отримано. Наш менеджер зателефонує вам найближчим часом."}
    else:
        return {"status": "success", "reply": "Дякуємо! Заявку прийнято, скоро зв'яжемося."}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    matched_products = search_products_in_db(data.message, limit=4)
    
    catalog_context = ""
    if matched_products:
        lines = []
        for p in matched_products:
            short_desc = p['description'][:180] + "..." if p['description'] else ""
            lines.append(f"Модель: {p['name']} | Ціна: {p['price']} грн | Бренд: {p['vendor']} | Опис: {short_desc}")
        catalog_context = "\n---\n".join(lines)
    else:
        catalog_context = "Точних моделей за прямим запитом не знайдено."

    system_instruction = f"""
    Ти — професійний і доброзичливий консультант інтернет-магазину годинників ZORRO.
    
    ЗНАЙДЕНІ ТОВАРИ З БАЗИ ПІД ЗАПИТ:
    {catalog_context}
    
    ВИМОГИ ДО ВІДПОВІДІ:
    1. Запропонуй до 3-4 варіантів у вигляді гарного списку.
    2. Для кожного товару вкажи:
       - Назву моделі
       - Ціну в грн
       - Головну фішку (1 речення: водостійкість, автономність, функції).
    3. Оформлюй текст чисто, роби відступи між пунктами.
    4. Наприкінці запропонуй: «Якщо бажаєте, напишіть свій номер телефону, і наш менеджер зателефонує для детальної консультації!».
    5. Мова відповідей — українська.
    """

    history_formatted = [{"role": m.role, "content": m.content} for m in data.history] if data.history else []
    reply = call_gemini_api(data.message, history_formatted, system_instruction)
    return {"reply": reply}
