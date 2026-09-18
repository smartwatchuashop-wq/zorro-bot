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
CACHE_TTL = 7200  # 2 години в секундах

# Словник розширених синонімів для точного пошуку характеристик
SYNONYMS_MAP = {
    "ліхтарик": ["ліхтарик", "ліхтар", "фонарик", "фонарь", "torch", "flashlight", "led-підсвічування", "led підсвічування", "світлодіод"],
    "сим": ["sim", "сим", "4g", "3g", "gsm", "слот", "дзвінк"],
    "камера": ["камер", "camera", "photo", "відео", "фото"],
    "тиск": ["тиск", "тонометр", "pressure"],
    "водонепроникний": ["ip67", "ip68", "waterproof", "водозахист", "3atm", "5atm", "водостійк"],
    "дитячий": ["дитяч", "kids", "baby", "дитин"],
}

def fetch_products_from_xml():
    """Завантажує та парсить УСІ товари з XML-прайсу Best-Time"""
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
    """Отримує товари з кешу або оновлює кеш кожні 2 години"""
    global CACHED_PRODUCTS, LAST_FETCH_TIME
    current_time = time.time()
    
    if not CACHED_PRODUCTS or (current_time - LAST_FETCH_TIME) > CACHE_TTL:
        new_products = fetch_products_from_xml()
        if new_products:
            CACHED_PRODUCTS = new_products
            LAST_FETCH_TIME = current_time
            
    return CACHED_PRODUCTS

def search_relevant_products(query: str, all_products: list, limit: int = 50):
    """Гнучкий пошук товарів за запитом користувача з урахуванням синонімів"""
    query_clean = query.lower()
    
    # Збираємо всі ключові слова для пошуку, включаючи синоніми
    search_terms = set(re.findall(r'\w+', query_clean))
    
    for key, syn_list in SYNONYMS_MAP.items():
        if any(syn in query_clean for syn in syn_list) or key in query_clean:
            search_terms.update(syn_list)

    matched_products = []
    for prod in all_products:
        score = 0
        text = prod["full_text"]
        
        for term in search_terms:
            if len(term) > 2 and term in text:
                score += 1
                
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
            f"Опис та характеристики: {p['description']}\n"
            "---"
        )
    
    context_str = "\n".join(catalog_context)
    
    system_prompt = f"""
    Ти — професійний продавець-консультант інтернет-магазину годинників ZORRO.
    
    Ось відібраний список товарів з прайсу під запит клієнта:
    {context_str}
    
    ПРАВИЛА ВІДПОВІДІ:
    1. Якщо у списках є товари з запитуваною функцією (наприклад, ліхтарик, SIM-карта, водозахист тощо), ПЕРЕРАХУЙ УСІ ЗНАЙДЕНІ МОДЕЛІ з цього списку та їх ціни.
    2. Уважно перевіряй опис товару на наявність синонімів цієї функції (наприклад, "ліхтарик", "фонарик", "LED", "підсвічування", "torch").
    3. Не обмежуйся 2-3 моделями, якщо у списку їх більше.
    4. Пам'ятай попередній контекст розмови.
    5. Відповідай ввічливо, структуровано та українською мовою.
    """
    
    # Формування історії діалогу для пам'яті
    messages = [{"role": "system", "content": system_prompt}]
    
    if data.history:
        for msg in data.history[-6:]:  # Зберігаємо останні 6 реплік для контексту
            messages.append({"role": msg.role, "content": msg.content})
            
    messages.append({"role": "user", "content": data.message})
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=600
        )
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        return {"reply": f"Помилка сервера: {str(e)}"}
