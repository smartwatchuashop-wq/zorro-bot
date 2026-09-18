import os
import re
import time
import xml.etree.ElementTree as ET
import requests
import google.generativeai as genai
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
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

XML_URL = "https://support.best-time.biz/api/feed/drops/ua"

CACHED_CATALOG_TEXT = ""
LAST_FETCH_TIME = 0
CACHE_TTL = 7200  # 2 години

def load_and_format_xml_catalog():
    """Завантажує весь XML-каталог та готує його для Gemini"""
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

@app.get("/")
def root():
    return {"status": "ok", "catalog_loaded": len(CACHED_CATALOG_TEXT) > 0}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    if not GEMINI_API_KEY:
        return {"reply": "API-ключ GEMINI_API_KEY не налаштовано на сервері."}
    
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
    3. Якщо покупець шукає поєднання двох або більше функцій (наприклад, SIM-карта + ліхтарик чи Wi-Fi + ліхтарик), уважно перевір увесь каталог. Якщо жодної такої моделі немає з обома функціями одночасно — чесно та природно дай відповідь (наприклад: "На жаль, моделей, де є і SIM-карта, і ліхтарик одночасно, зараз немає в наявності. Але у нас є чудові варіанти окремо з SIM-картою або окремо з ліхтариком").
    4. Уважно стеж за контекстом розмови (на запитання "а бувають такі?", "ціна?", "а для хлопчика?" відповідай з урахуванням попередніх реплік клієнта).
    5. Пропонуючи конкретний товар, називай його повну назву, ціну та коротко виділяй потрібну характеристику.
    6. Спілкуйся українською мовою, легко, коротко та без шаблонних вигадок.
    """

    try:
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=system_instruction
        )
        
        chat_session = model.start_chat(history=[])
        
        # Передаємо історію листування
        if data.history:
            for msg in data.history[-6:]:
                role = "user" if msg.role == "user" else "model"
                chat_session.history.append({"role": role, "parts": [msg.content]})
        
        response = chat_session.send_message(data.message)
        return {"reply": response.text}
        
    except Exception as e:
        return {"reply": f"Помилка Gemini API: {str(e)}"}
