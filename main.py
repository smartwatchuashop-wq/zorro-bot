import os
import xml.etree.ElementTree as ET
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI()

# Дозволяємо запити з будь-якого сайту (усуває CORS-помилку)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

XML_URL = "https://best-time.biz/prom-import.xml"

def get_products_context():
    try:
        response = requests.get(XML_URL, timeout=10)
        root = ET.fromstring(response.content)
        products = []
        
        # Беремо товари з XML-фіду
        for offer in root.findall(".//offer")[:60]:
            name = offer.findtext("name", "")
            price = offer.findtext("price", "")
            description = offer.findtext("description", "")[:150] if offer.findtext("description") else ""
            
            if name and price:
                products.append(f"Товар: {name} | Ціна: {price} грн | Опис: {description}")
                
        return "\n".join(products)
    except Exception:
        return "Каталог тимчасово недоступний."

class ChatRequest(BaseModel):
    message: str

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/api/chat")
async def chat(data: ChatRequest):
    if not client:
        return {"reply": "API-ключ OpenAI не налаштовано на сервері."}
    
    catalog = get_products_context()
    
    system_prompt = f"""
    Ти — консультант інтернет-магазину годинників ZORRO.
    Твоє завдання — допомагати покупцям підбирати годинники з наявного асортименту.
    Відповідай ввічливо, коротко, українською мовою.
    
    Ось актуальний каталог товарів магазину:
    {catalog}
    
    Рекомендуй лише ті товари, які є в каталозі. Якщо запитують про функції (наприклад, ліхтарик, Wi-Fi, водонепроникність), шукай їх в описі товарів.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": data.message}
            ],
            max_tokens=300
        )
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        return {"reply": f"Помилка сервера: {str(e)}"}
