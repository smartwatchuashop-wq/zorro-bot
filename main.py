
import os
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

def get_products_context():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(XML_URL, headers=headers, timeout=20)
        
        # Парсимо XML фід
        root = ET.fromstring(response.content)
        products = []
        
        # Переглядаємо всі товари (теги offer або item)
        offers = root.findall(".//offer") or root.findall(".//item")
        
        for offer in offers:
            name = offer.findtext("name") or offer.findtext("title") or ""
            price = offer.findtext("price") or ""
            description = offer.findtext("description") or ""
            vendor = offer.findtext("vendor") or ""
            
            # Очищаємо текст від HTML-тегів
            if description:
                description = description.replace("<p>", "").replace("</p>", "").replace("<br>", " ").replace("<br/>", " ")[:150]
            
            if name and price:
                products.append(f"Товар: {name} (Бренд: {vendor}) | Ціна: {price} грн | Опис: {description}")
            
            if len(products) >= 100:  # Беремо 100 актуальних товарів
                break
                
        if not products:
            return "Каталог порожній."
            
        return "\n".join(products)
    except Exception as e:
        return f"Помилка завантаження каталогу: {str(e)}"

class ChatRequest(BaseModel):
    message: str

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    if not client:
        return {"reply": "API-ключ OpenAI не налаштовано."}
    
    catalog = get_products_context()
    
    system_prompt = f"""
    Ти — професійний продавець-консультант інтернет-магазину годинників ZORRO.
    Твоє завдання — допомагати покупцям підбирати годинники з наявного асортименту.
    Відповідай ввічливо, коротко, українською мовою.
    
    Ось актуальний каталог товарів магазину з Best-Time:
    {catalog}
    
    Рекомендуй лише ті товари, які є в каталозі. Якщо запитують про функції (наприклад, водонепроникність, Bluetooth, ліхтарик), шукай їх в описі товарів.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": data.message}
            ],
            max_tokens=350
        )
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        return {"reply": f"Помилка сервера: {str(e)}"}
