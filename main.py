import os
import re
import time
import xml.etree.ElementTree as ET
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from openai import OpenAI

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

XML_URL = "https://support.best-time.biz/api/feed/drops/ua"

CACHED_PRODUCTS = []
LAST_FETCH_TIME = 0
CACHE_TTL = 7200  # 2 години

SYNONYMS_MAP = {
    "ліхтарик": ["ліхтарик", "ліхтар", "фонарик", "фонарь", "torch", "flashlight", "led-підсвічування", "світлодіод"],
    "сим": ["sim", "сим", "4g", "3g", "gsm", "слот для карт", "вставити сим"],
    "камера": ["камер", "camera", "photo", "відео", "фото"],
    "тиск": ["тиск", "тонометр", "pressure"],
    "водонепроникний": ["ip67", "ip68", "waterproof", "водозахист", "3atm", "5atm", "водостійк"],
    "дитячий": ["дитяч", "kids", "baby", "дитин"],
}

def fetch_products_from_xml():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(XML_URL, headers=headers, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code != 200:
            return []

        root = ET.fromstring(response.content)
        items = root.findall(".//product") or root.findall(".//offer") or root.findall(".//item")
        
        parsed_products = []
        for item in items:
            name = item.findtext("name") or item.findtext("title") or ""
            price = item.findtext("price") or ""
            description = item.findtext("description") or ""
            vendor = item.findtext("vendor") or item.findtext("brand") or ""
            category = item.findtext("category") or item.findtext("categoryId") or ""
            
            if description:
                description = re.sub(r'<[^>]+>', ' ', description)
                description = " ".join(description.split())
            
            if name and price:
                full_search_text = f"{name} {vendor} {category} {description}".lower()
                parsed_products.append({
                    "name": name,
                    "price": price,
                    "vendor": vendor,
                    "description": description,
                    "full_text": full_search_text
                })
                
        return parsed_products
    except Exception:
        return []

def get_all_products():
    global CACHED_PRODUCTS, LAST_FETCH_TIME
    current_time = time.time()
    
    if not CACHED_PRODUCTS or (current_time - LAST_FETCH_TIME) > CACHE_TTL:
        new_products = fetch_products_from_xml()
        if new_products:
            CACHED_PRODUCTS = new_products
            LAST_FETCH_TIME = current_time
            
    return CACHED_PRODUCTS

def search_relevant_products(query: str, all_products: list, limit: int = 35):
    query_clean = query.lower()
    
    # Визначаємо, які саме категорії/функції шукає користувач
    requested_groups = []
    for group_name, syn_list in SYNONYMS_MAP.items():
        if any(syn in query_clean for syn in syn_list) or group_name in query_clean:
            requested_groups.append(syn_list)

    matched_products = []
    
    for prod in all_products:
        text = prod["full_text"]
        score = 0
        
        # Перевірка на відповідність кожній із запитуваних груп (наприклад, І sim, І ліхтарик)
        if requested_groups:
            matches_all_groups = True
            for syn_list in requested_groups:
                if any(syn in text for syn in syn_list):
                    score += 1
                else:
                    matches_all_groups = False
            
            # Надаємо максимальний пріоритет товарам, що містять УСІ запитувані функції одразу
            if matches_all_groups:
                score += 100

        if score > 0:
            matched_products.append((score, prod))
            
    matched_products.sort(key=lambda x: x[0], reverse=True)
    results = [p[1] for p in matched_products[:limit]]
    
    if not results:
        results = all_products[:limit]
        
    return results

class MessageItem(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: Optional[List[MessageItem]] = []

@app.get("/")
def root():
    return {"status": "ok", "cached_products": len(CACHED_PRODUCTS)}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    if not client:
        return {"reply": "API-ключ OpenAI не налаштовано."}
    
    all_products = get_all_products()
    if not all_products:
        return {"reply": "Вибачте, каталог товарів тимчасово недоступний."}
    
    relevant_products = search_relevant_products(data.message, all_products)
    
    catalog_context = []
    for p in relevant_products:
        catalog_context.append(
            f"Модель: {p['name']} | Ціна: {p['price']} грн | Бренд: {p['vendor']}\n"
            f"Офіційний опис з бази: {p['description']}\n"
            "---"
        )
    
    context_str = "\n".join(catalog_context)
    
    system_prompt = f"""
    Ти — суворий і чесний консультант інтернет-магазину годинників ZORRO.
    
    Ось відібрані товари з нашої бази даних:
    {context_str}
    
    СУВОРІ ПРАВИЛА:
    1. Відповідай ТІЛЬКИ на основі наведеного "Офіційного опису з бази".
    2. КАТЕГОРИЧНО ЗАБОРОНЕНО вигадувати або додумувати характеристики!
    3. Якщо в описі товару НЕМАЄ прямої згадки про ліхтарик (або його синоніми: фонарик, flashlight, LED) чи SIM-карту — стверджувати, що ця функція є, ЗАБОРОНЕНО.
    4. Якщо покупець шукає поєднання двох функцій (наприклад, SIM + ліхтарик), і в базі немає жодної моделі з двома цими функціями ОДНОЧАСНО — чесно скажи: "На жаль, моделей, де є і SIM-карта, і ліхтарик одночасно, зараз немає в наявності. Але є окремо з SIM-картою або окремо з ліхтариком."
    5. Відповідай коротко, ввічливо, українською мовою.
    """
    
    messages = [{"role": "system", "content": system_prompt}]
    
    if data.history:
        for msg in data.history[-6:]:
            messages.append({"role": msg.role, "content": msg.content})
            
    messages.append({"role": "user", "content": data.message})
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=500
        )
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        return {"reply": f"Помилка сервера: {str(e)}"}
