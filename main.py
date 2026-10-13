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
            
            sql = f"SELECT name, price, vendor, description, url, image FROM products WHERE {' OR '.join(conditions)} LIMIT %s"
            params.append(limit)
            cur.execute(sql, tuple(params))
        else:
            cur.execute("SELECT name, price, vendor, description, url, image FROM products ORDER BY RANDOM() LIMIT %s", (limit,))
        
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
    if matched_products:
        lines = []
        for p in matched_products:
            search_query = urllib.parse.quote_plus(p['name'])
            prod_url = f"https://zorro.com.ua/search/?q={search_query}"
            short_desc = p['description'][:140] + "..." if p['description'] else "Сучасний та надійний аксесуар."
            lines.append(f"Назва: {p['name']} | Ціна: {p['price']} грн | Посилання: {prod_url} | Опис: {short_desc}")
        catalog_context = "\n---\n".join(lines)
    else:
        catalog_context = "Точних моделей за ключовими словами не знайдено, запропонуй звернутися до каталогу або уточнити критерії."

    system_instruction = f"""
    Ти — найкращий онлайн-консультант і менеджер з продажу інтернет-магазину годинників ZORRO (zorro.com.ua).
    Твоя головна мета — допомогти клієнту вибрати ідеальний годинник, зацікавити його покупкою та виявити його потреби.

    ДАНІ ПРО РЕКОМЕНДОВАНІ ТОВАРИ З КАТАЛОГУ:
    {catalog_context}

    ПРАВИЛА ОФОРМЛЕННЯ ТА ПРОДАЖІВ:
    1. Не пиши складний markdown із зірочками на кшталт **'Jaragar'**. Пиши чисто, живо й красиво.
    2. Якщо пропонуєш товари, обов'язково оформи кожен пункт так:
       👉 <b>Назва моделі</b> — Ціна грн
       Коротка ключова перевага моделі (водостійкість, дзвінки, автономність, дизайн).
       <a href="ПОСИЛАННЯ_З_ДАННИХ" target="_blank" style="color:#0070f3; text-decoration:underline;">👉 Подивитися на сайті</a>
    3. Обов'язково став активне уточнююче запитання наприкінці, щоб виявити потребу:
       - «Ви підбираєте годинник для себе чи на подарунок?»
       - «Який стиль вам ближче: масивний тактичний чи акуратний класичний?»
       - «Чи важлива для вас функція дзвінків з годинника або водонепроникність?»
    4. Запропонуй залишити номер телефону для швидкої консультації менеджера або бронювання моделі.
    5. Спілкуйся виключно українською мовою, ввічливо, енергійно та позитивно.
    """

    history_formatted = [{"role": m.role, "content": m.content} for m in data.history] if data.history else []
    reply = call_gemini_api(data.message, history_formatted, system_instruction)
    return {"reply": reply}
