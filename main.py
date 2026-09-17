import os
import re
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

def fetch_all_products():
    """Завантажує та парсить УСІ товари з прайсу без обмежень"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(XML_URL, headers=headers, timeout=25)
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
            
            # Повна очистка опису від HTML
            if description:
                description = re.sub(r'<[^>]+>', ' ', description)
                description = " ".join(description.split())
            
            if name and price:
                parsed_products.append({
                    "name": name,
                    "price": price,
                    "vendor": vendor,
                    "description": description,
                    "full_text": f"{name} {vendor} {description}".lower()
                })
                
        return parsed_products
    except Exception:
        return []

def search_relevant_products(query: str, all_products: list, limit: int = 25):
    """Шукає найвідповідніші товари за ключовими словами у всьому прайсі"""
    query_lower = query.lower()
    
    # Словник синонімів для точного пошуку характеристик
    synonyms = {
        "сим": ["sim", "4g", "3g", "gsm", "слот", "дзвінк"],
        "камер": ["камер", "camera", "photo", "відео", "фото"],
        "водонепроникн": ["ip67", "ip68", "waterproof", "водозахист", "3atm", "5atm"],
        "дитяч": ["дитяч", "kids", "baby", "дитин"],
        "смарт": ["smart", "смарт", "сенсорн"]
    }
    
    search_terms = re.findall(r'\w+', query_lower)
    expanded_terms = set(search_terms)
    
    for term in search_terms:
        for key, syn_list in synonyms.items():
            if key in term:
                expanded_terms.update(syn_list)

    matched_products = []
    for prod in all_products:
        score = sum(1 for term in expanded_terms if term in prod["full_text"])
        if score > 0:
            matched_products.append((score, prod))
            
    # Сортуємо за релевантністю
    matched_products.sort(key=lambda x: x[0], reverse=True)
    
    # Якщо знайшли за ключовими словами — повертаємо їх, якщо ні — перші товари з прайсу
    results = [p[1] for p in matched_products[:limit]]
    if not results:
        results = all_products[:limit]
        
    return results

class ChatRequest(BaseModel):
    message: str

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    if not client:
        return {"reply": "API-ключ OpenAI не налаштовано."}
    
    all_products = fetch_all_products()
    
    if not all_products:
        return {"reply": "Вибачте, каталог товарів тимчасово недоступний."}
    
    # Знаходимо товари під конкретний запит користувача
    relevant_products = search_relevant_products(data.message, all_products)
    
    catalog_context = []
    for p in relevant_products:
        catalog_context.append(
            f"Назва: {p['name']} | Ціна: {p['price']} грн | Бренд: {p['vendor']}\n"
            f"Повні характеристики: {p['description']}\n"
            "---"
        )
    
    context_str = "\n".join(catalog_context)
    
    system_prompt = f"""
    Ти — консультант інтернет-магазину годинників ZORRO.
    Твоє завдання — допомагати покупцям обирати годинники з наявного асортименту.
    
    Ось відфільтровані з повного прайсу товари, які найкраще відповідають запиту користувача:
    {context_str}
    
    ПРАВИЛА ВІДПОВІДІ:
    1. Пропонуй ТІЛЬКИ ті моделі, які є в наведеному списку вище.
    2. Якщо у списку є потрібні функції (наприклад, SIM-карта, камера, водонепроникність) — обов'язково назви конкретні моделі та їхні ціни.
    3. Якщо серед знайдених товарів немає потрібних функцій, чесно скажи про це.
    4. Відповідай ввічливо, коротко та українською мовою.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": data.message}
            ],
            max_tokens=400
        )
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        return {"reply": f"Помилка сервера: {str(e)}"}
