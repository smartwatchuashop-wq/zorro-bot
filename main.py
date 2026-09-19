import os
import re
import time
import xml.etree.ElementTree as ET
import requests
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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

XML_URL = "https://support.best-time.biz/api/feed/drops/ua"

CACHED_CATALOG_TEXT = ""
LAST_FETCH_TIME = 0
CACHE_TTL = 7200  # 2 години

def send_telegram_notification(phone: str, message: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    
    text = f"📞 *НОВА ЗАЯВКА НА ДЗВІНОК!*\n\n*Телефон:* `{phone}`"
    if message:
        text += f"\n*Коментар:* {message}"
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
        return True
    except Exception:
        return False

def load_and_format_xml_catalog():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(XML_URL, headers=headers, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code != 200:
            return ""

        root = ET.fromstring(response.content)
        items = root.findall(".//product") or root.findall(".//offer") or root.findall(".//item")
        
        catalog_lines = []
        for item in items:
            name = item.findtext("name") or item.findtext("title") or ""
            price = item.findtext("price") or ""
            description = item.findtext("description") or ""
            vendor = item.findtext("vendor") or item.findtext("brand") or ""
            
            if description:
                description = re.sub(r'<[^>]+>', ' ', description)
                description = " ".join(description.split())
            
            if name and price:
                catalog_lines.append(
                    f"Товар: {name} | Ціна: {price} грн | Бренд: {vendor}\n"
                    f"Опис: {description}\n"
                    f"------------------------------------"
                )
                
        return "\n".join(catalog_lines)
    except Exception:
        return ""

def get_full_catalog():
    global CACHED_CATALOG_TEXT, LAST_FETCH_TIME
    current_time = time.time()
    
    if not CACHED_CATALOG_TEXT or (current_time - LAST_FETCH_TIME) > CACHE_TTL:
        new_catalog = load_and_format_xml_catalog()
        if new_catalog:
            CACHED_CATALOG_TEXT = new_catalog
            LAST_FETCH_TIME = current_time
            
    return CACHED_CATALOG_TEXT

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
    return {"status": "ok", "catalog_loaded": len(CACHED_CATALOG_TEXT) > 0}

@app.post("/api/callback")
async def callback(data: CallbackRequest):
    if not data.phone:
        return {"status": "error", "reply": "Введіть номер телефону."}
    
    success = send_telegram_notification(data.phone, data.message)
    if success:
        return {"status": "success", "reply": "Дякуємо! Менеджер зателефонує вам найближчим часом."}
    else:
        return {"status": "error", "reply": "Помилка відправки. Перевірте налаштування Telegram."}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    if not GEMINI_API_KEY:
        return {"reply": "API-ключ GEMINI_API_KEY не налаштовано."}
    
    full_catalog = get_full_catalog()
    if not full_catalog:
        return {"reply": "Вибачте, каталог товарів тимчасово недоступний."}

    system_instruction = f"""
    Ти — досвідчений, привітний та чесний менеджер-консультант інтернет-магазину годинників ZORRO.
    
    ОСЬ ПОВНИЙ КАТАЛОГ УСІХ НАШИХ ТОВАРІВ:
    {full_catalog}
    
    ПРАВИЛА РОБОТИ:
    1. Відповідай ТІЛЬКИ на основі даних із наданого каталогу товарів.
    2. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати характеристики, яких немає в описі товару!
    3. Якщо покупець шукає поєднання кількох функцій, яких немає разом в одній моделі — чесно скажи про це і запропонуй варіанти окремо.
    4. Уважно стеж за контекстом розмови.
    5. Пропонуючи товар, називай назву, ціну та ключову фішку.
    6. Якщо клієнт вагається — пропонуй залишити номер телефону для швидкої консультації менеджера.
    7. Спілкуйся українською мовою, коротко та дружньо.
    """

    # Використовуємо актуальну модель gemini-2.5-flash
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    contents = []
    if data.history:
        for msg in data.history[-6:]:
            role = "user" if msg.role == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg.content}]})
            
    contents.append({"role": "user", "parts": [{"text": data.message}]})

    payload = {
        "contents": contents,
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "generationConfig": {
            "temperature": 0.3
        }
    }

    try:
        res = requests.post(url, json=payload, timeout=25)
        res_json = res.json()
        
        if res.status_code == 200:
            reply_text = res_json['candidates'][0]['content']['parts'][0]['text']
            return {"reply": reply_text}
        else:
            err_msg = res_json.get('error', {}).get('message', 'Невідома помилка')
            return {"reply": f"Помилка Google API: {err_msg}"}
            
    except Exception as e:
        return {"reply": f"Помилка з'єднання: {str(e)}"}
