import os
import re
import time
import xml.etree.ElementTree as ET
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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

# Глобальні змінні для кешування товарів у пам'яті
CACHED_PRODUCTS = []
LAST_FETCH_TIME = 0
CACHE_TTL = 7200  # 2 години в секундах (2 * 60 * 60)

def fetch_products_from_xml():
    """Завантажує та парсить УСІ товари з XML-прайсу Best-Time"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(XML_URL, headers=headers, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code != 200:
            print(f"Помилка завантаження XML, статус-код: {response.status_code}")
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
            
            # Повна очистка опису від HTML-тегів
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
                
        print(f"Успішно завантажено та закешовано {len(parsed_products)} товарів.")
        return parsed_products
    except Exception as e:
        print(f"Помилка парсингу XML: {e}")
        return []

def get_all_products():
    """Отримує товари з кешу або оновлює кеш, якщо минуло більше 2 годин"""
    global CACHED_PRODUCTS, LAST_FETCH_TIME
    current_time = time.time()
    
    # Якщо кеш порожній або минуло більше 2 годин — оновлюємо дані
    if not CACHED_PRODUCTS or (current_time - LAST_FETCH_TIME) > CACHE_TTL:
        print("Оновлення кешу товарів з XML Best-Time...")
        new_products = fetch_products_from_xml()
        if new_products:
            CACHED_PRODUCTS = new_products
            LAST_FETCH_TIME = current_time
            
    return CACHED_PRODUCTS

def search_relevant_products(query: str, all_products: list, limit: int = 35):
    """Швидкий пошук за закешованими товарами в пам'яті"""
    query_clean = re.sub(r'[^\w\s]', '', query.lower())
    words = [w for w in query_clean.split() if len(w) > 2]
    
    if not words:
        return all_products[:limit]
        
    stems = [w[:-1] if len(w) > 4 else w for w in words]

    matched_products = []
    for prod in all_products:
        score = 0
        text = prod["full_text"]
        
        for stem in stems:
            if stem in text:
                score += 1
                
        if score > 0:
            matched_products.append((score, prod))
            
    matched_products.sort(key=lambda x: x[0], reverse=True)
    results = [p[1] for p in matched_products[:limit]]
    
    if not results:
        results = all_products[:limit]
        
    return results

class ChatRequest(BaseModel):
    message: str

@app.get("/")
def root():
    return {"status": "ok", "cached_products": len(CACHED_PRODUCTS)}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    if not client:
        return {"reply": "API-ключ OpenAI не налаштовано."}
    
    # Беремо товари МИТТЄВО з пам'яті сервера
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
    
    Ось відібрані товари з нашого актуального прайсу:
    {context_str}
    
    ПРАВИЛА РОБОТИ:
    1. Пропонуй конкретні моделі та обов'язково вказуй їх ціну.
    2. Уважно шукай характеристики (ліхтарик, SIM-карта, камера, водозахист, захисне скло, матеріал ремінця тощо) у наданому описі.
    3. Якщо запитувана функція є в описі товару — обов'язково запропонуй його покупцеві.
    4. Відповідай ввічливо, коротко та українською мовою.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": data.message}
            ],
            max_tokens=450
        )
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        return {"reply": f"Помилка сервера: {str(e)}"}
