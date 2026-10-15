import os
import re
import json
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

ZORRO_XML_URL = "https://zorro.com.ua/content/export/c929bc168711aedbbcad10f5bb726db0.xml"
BESTTIME_XML_URL = "https://best-time.biz/ua.xml"

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
        print(f"Помилка БД: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn:
        return
    with conn.cursor() as cur:
        # Універсальна таблиця з виділеними фасетами для будь-яких категорій
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products_universal (
                id SERIAL PRIMARY KEY,
                vendor_code VARCHAR(100) UNIQUE,
                name TEXT NOT NULL,
                price NUMERIC(10, 2),
                vendor VARCHAR(100),
                description TEXT,
                url TEXT,
                image TEXT,
                category_type VARCHAR(50),      -- smart, mechanical, quartz, accessory
                target_audience VARCHAR(50),    -- men, women, kids, unisex
                style VARCHAR(50),              -- classic, sport, tactical, casual
                strap_material VARCHAR(50),     -- leather, steel, rubber, fabric
                waterproof_level VARCHAR(50),   -- rain, swimming, none
                has_calls BOOLEAN DEFAULT FALSE,
                has_gps BOOLEAN DEFAULT FALSE,
                is_skeleton BOOLEAN DEFAULT FALSE,
                raw_params TEXT
            );
        """)
        conn.commit()
    conn.close()

init_db()

def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    cleanr = re.compile(r'<.*?>')
    return re.sub(cleanr, '', raw_html).strip()

def sync_all_catalogs():
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "message": "БД не підключена"}

    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # 1. Завантаження структурованих параметрів Best Time
    besttime_map = {}
    try:
        res_bt = requests.get(BESTTIME_XML_URL, headers=headers, timeout=60)
        if res_bt.status_code == 200:
            root_bt = ET.fromstring(res_bt.content)
            for offer in root_bt.findall('.//offer'):
                vcode = (offer.findtext('vendorCode') or offer.get('id') or '').strip()
                if not vcode:
                    continue
                
                params = {}
                for p in offer.findall('param'):
                    name = p.get('name', '').strip()
                    val = (p.text or '').strip()
                    if name and val:
                        params[name.lower()] = val

                desc = (offer.findtext('description_ua') or offer.findtext('description') or '')
                besttime_map[vcode] = {"params": params, "desc": desc}
    except Exception as e:
        print(f"Помилка завантаження Best Time: {e}")

    # 2. Завантаження каталогу ZORRO та фасетний розбір
    count = 0
    try:
        res_zorro = requests.get(ZORRO_XML_URL, headers=headers, timeout=60)
        res_zorro.raise_for_status()
        root_z = ET.fromstring(res_zorro.content)
        offers = root_z.findall('.//offer') or root_z.findall('.//item')

        with conn.cursor() as cur:
            for offer in offers:
                internal_id = offer.get('id') or ''
                vcode = (offer.findtext('vendorCode') or internal_id).strip()
                name = (offer.findtext('name') or offer.findtext('model') or '').strip()
                price_str = offer.findtext('price') or '0'
                try:
                    price = float(price_str.replace(',', '.'))
                except ValueError:
                    price = 0.0

                vendor = (offer.findtext('vendor') or '').strip()
                desc = clean_html(offer.findtext('description') or '')

                raw_url = (offer.findtext('url') or '').strip()
                if raw_url.startswith('http') and raw_url.rstrip('/') != 'https://zorro.com.ua':
                    url = raw_url
                elif raw_url.startswith('/'):
                    url = f"https://zorro.com.ua{raw_url}"
                elif name:
                    url = f"https://zorro.com.ua/katalog/search/?q={urllib.parse.quote_plus(name)}"
                else:
                    url = "https://zorro.com.ua"

                pic = offer.find('picture') or offer.find('image')
                image = pic.text.strip() if (pic is not None and pic.text) else ''

                # Об'єднуємо параметри з обох джерел
                bt_data = besttime_map.get(vcode, {})
                bt_params = bt_data.get("params", {})
                
                # Додаємо параметри Хорошопу, якщо є
                for p in offer.findall('param'):
                    pn = p.get('name', '').strip().lower()
                    pv = (p.text or '').strip()
                    if pn and pv:
                        bt_params[pn] = pv

                all_text = f"{name} {desc} {' '.join(bt_params.values())}".lower()

                # --- УНІВЕРСАЛЬНА КЛАСИФІКАЦІЯ ТОВАРУ ---

                # 1. Цільова аудиторія
                target_aud = "unisex"
                if any(k in all_text for k in ['дитяч', 'для дітей', 'хлопчик', 'дівчат', 'школ']):
                    target_aud = "kids"
                elif any(k in all_text for k in ['жіноч', 'для жінок', 'жіночий']):
                    target_aud = "women"
                elif any(k in all_text for k in ['чоловіч', 'для чоловіків', 'чоловічий']):
                    target_aud = "men"

                # 2. Категорія / Тип механізму
                cat_type = "quartz"
                if any(k in all_text for k in ['smart', 'смарт', 'розумний годинник', 'фітнес']):
                    cat_type = "smart"
                elif any(k in all_text for k in ['механ', 'автозавод', 'механічний']):
                    cat_type = "mechanical"
                elif any(k in all_text for k in ['ремінець', 'коробка', 'браслет', 'скринька']):
                    cat_type = "accessory"

                # 3. Стиль
                style = "casual"
                if any(k in all_text for k in ['спорт', 'sport', 'секундомір']):
                    style = "sport"
                elif any(k in all_text for k in ['такточн', 'тактичний', 'військов', 'мілітарі', 'армійськ']):
                    style = "tactical"
                elif any(k in all_text for k in ['класич', 'елегант', 'ділов', 'під костюм']):
                    style = "classic"

                # 4. Матеріал ремінця
                strap_mat = "other"
                if any(k in all_text for k in ['шкір', 'кожан', 'leather']):
                    strap_mat = "leather"
                elif any(k in all_text for k in ['стал', 'метал', 'сталев', 'титан']):
                    strap_mat = "steel"
                elif any(k in all_text for k in ['каучук', 'силікон', 'полімер', 'rubber']):
                    strap_mat = "rubber"

                # 5. Водозахист
                water_lvl = "rain"
                if any(k in all_text for k in ['плавання', 'басейн', '5 атм', '10 атм', '50 м', '100 м', 'водонепроникний']):
                    water_lvl = "swimming"
                elif '3 атм' in all_text or 'дощ' in all_text or 'мити руки' in all_text:
                    water_lvl = "rain"

                # 6. Ознаки функцій
                has_calls = any(k in all_text for k in ['дзвін', 'виклики', 'розмов', 'можливість розмовляти', 'sim', 'сім', 'мікрофон', 'bluetooth call'])
                has_gps = 'gps' in all_text
                is_skeleton = any(k in all_text for k in ['скелетон', 'skeleton', 'прозора задня кришка', 'турбілліон'])

                raw_params_str = " | ".join([f"{k}: {v}" for k, v in bt_params.items()])

                if not vcode:
                    vcode = name

                if name:
                    cur.execute("""
                        INSERT INTO products_universal (
                            vendor_code, name, price, vendor, description, url, image,
                            category_type, target_audience, style, strap_material, waterproof_level,
                            has_calls, has_gps, is_skeleton, raw_params
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (vendor_code) DO UPDATE SET
                            name = EXCLUDED.name,
                            price = EXCLUDED.price,
                            vendor = EXCLUDED.vendor,
                            description = EXCLUDED.description,
                            url = EXCLUDED.url,
                            image = EXCLUDED.image,
                            category_type = EXCLUDED.category_type,
                            target_audience = EXCLUDED.target_audience,
                            style = EXCLUDED.style,
                            strap_material = EXCLUDED.strap_material,
                            waterproof_level = EXCLUDED.waterproof_level,
                            has_calls = EXCLUDED.has_calls,
                            has_gps = EXCLUDED.has_gps,
                            is_skeleton = EXCLUDED.is_skeleton,
                            raw_params = EXCLUDED.raw_params;
                    """, (
                        vcode, name, price, vendor, desc, url, image,
                        cat_type, target_aud, style, strap_mat, water_lvl,
                        has_calls, has_gps, is_skeleton, raw_params_str
                    ))
                    count += 1

            conn.commit()
        conn.close()
        return {"status": "success", "imported": count}
    except Exception as e:
        if conn:
            conn.close()
        return {"status": "error", "message": str(e)}

def call_gemini(prompt_text: str, system_instruction: str = "", temperature: float = 0.2):
    if not GEMINI_API_KEY:
        return ""
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": temperature}
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    for api_ver in ["v1beta", "v1"]:
        for model in CANDIDATE_MODELS:
            clean_model = model.replace("models/", "")
            url = f"https://generativelanguage.googleapis.com/{api_ver}/models/{clean_model}:generateContent?key={GEMINI_API_KEY}"
            try:
                res = requests.post(url, json=payload, timeout=12)
                if res.status_code == 200:
                    return res.json()['candidates'][0]['content']['parts'][0]['text']
            except Exception:
                continue
    return ""

def universal_catalog_search(user_query: str, limit: int = 3):
    conn = get_db_connection()
    if not conn:
        return []

    # ШІ-парсер витягує сутнісні фільтри для БУДЬ-ЯКОГО запиту
    parse_prompt = f"""
    Ти — пошуковий класифікатор каталогу годинників. Проаналізуй запит: "{user_query}"
    Поверни JSON строго без бектиків і тексту:
    {{
        "target_audience": "men" | "women" | "kids" | null,
        "category_type": "smart" | "mechanical" | "quartz" | null,
        "style": "classic" | "sport" | "tactical" | null,
        "strap_material": "leather" | "steel" | "rubber" | null,
        "waterproof_level": "swimming" | "rain" | null,
        "has_calls": true/false (тільки якщо явно запитують про дзвінки/розмови/sim),
        "is_skeleton": true/false (якщо просять скелетон або видимий механізм),
        "max_price": число або null,
        "keywords": ["конкретні", "слова", "бренд", "колір", "назва"]
    }}
    """
    ai_json = call_gemini(parse_prompt, temperature=0.1)

    target_aud = None
    cat_type = None
    style = None
    strap_mat = None
    water_lvl = None
    has_calls = False
    is_skeleton = False
    max_price = None
    keywords = []

    try:
        match = re.search(r'\{.*\}', ai_json, re.DOTALL)
        if match:
            p = json.loads(match.group(0))
            target_aud = p.get("target_audience")
            cat_type = p.get("category_type")
            style = p.get("style")
            strap_mat = p.get("strap_material")
            water_lvl = p.get("waterproof_level")
            has_calls = p.get("has_calls", False)
            is_skeleton = p.get("is_skeleton", False)
            max_price = p.get("max_price")
            keywords = p.get("keywords") or []
    except Exception:
        pass

    # Страховка базовими словами
    q_low = user_query.lower()
    if 'дитяч' in q_low or 'дитині' in q_low: target_aud = "kids"
    elif 'жіноч' in q_low or 'дівчин' in q_low: target_aud = "women"
    elif 'чоловіч' in q_low or 'хлопц' in q_low: target_aud = "men"

    if 'дзвін' in q_low or 'розмов' in q_low: 
        has_calls = True
        cat_type = "smart"
    elif 'смарт' in q_low or 'smart' in q_low: 
        cat_type = "smart"
    elif 'механ' in q_low: 
        cat_type = "mechanical"

    if not keywords:
        clean = re.findall(r'\w+', q_low)
        stop = {"годинник", "годинники", "хочу", "порадь", "купити", "які", "який", "підкажи", "модель", "моделі"}
        keywords = [w for w in clean if len(w) > 2 and w not in stop]

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Перевіряємо чи існує нова універсальна таблиця
            cur.execute("SELECT to_regclass('products_universal');")
            if cur.fetchone()['to_regclass'] is None:
                # Якщо ще не встигли синхронізувати нову таблицю
                conn.close()
                return []

            where_clauses = ["image IS NOT NULL AND image != ''"]
            params = []

            # Жорсткі фасетні обмеження
            if target_aud:
                where_clauses.append("target_audience = %s")
                params.append(target_aud)

            if cat_type:
                where_clauses.append("category_type = %s")
                params.append(cat_type)

            if has_calls:
                where_clauses.append("has_calls = TRUE")

            if is_skeleton:
                where_clauses.append("is_skeleton = TRUE")

            if water_lvl == "swimming":
                where_clauses.append("waterproof_level = 'swimming'")

            if max_price:
                where_clauses.append("price <= %s")
                params.append(float(max_price))

            where_sql = "WHERE " + " AND ".join(where_clauses)

            # Вибираємо кандидатів, які пройшли жорсткий фільтр
            sql = f"""
                SELECT vendor_code, name, price, vendor, description, url, image,
                       raw_params, category_type, target_audience, style, has_calls
                FROM products_universal
                {where_sql}
                ORDER BY price DESC
                LIMIT 50;
            """
            cur.execute(sql, tuple(params))
            candidates = cur.fetchall()

            # Якщо через надто строгий фільтр нічого не знайдено (наприклад, просили конкретний ремінець, якого нема в цьому стилі),
            # робимо м'яке послаблення, але зберігаємо категорію та аудиторію!
            if not candidates and (target_aud or cat_type):
                soft_where = ["image IS NOT NULL AND image != ''"]
                soft_params = []
                if target_aud:
                    soft_where.append("target_audience = %s")
                    soft_params.append(target_aud)
                if cat_type:
                    soft_where.append("category_type = %s")
                    soft_params.append(cat_type)
                
                cur.execute(f"SELECT vendor_code, name, price, vendor, description, url, image, raw_params, category_type, target_audience, style, has_calls FROM products_universal WHERE {' AND '.join(soft_where)} ORDER BY price DESC LIMIT 30;", tuple(soft_params))
                candidates = cur.fetchall()

            if not candidates:
                conn.close()
                return []

            # Ранжування по ключових словах
            scored = []
            for item in candidates:
                name_l = (item['name'] or '').lower()
                desc_l = (item['description'] or '').lower()
                params_l = (item.get('raw_params') or '').lower()
                vend_l = (item.get('vendor') or '').lower()
                score = 0

                for kw in keywords:
                    k = kw.lower()
                    if k in name_l: score += 6
                    if k in params_l: score += 4
                    if k in vend_l: score += 3
                    if k in desc_l: score += 1

                if strap_mat and strap_mat in params_l:
                    score += 5
                if style and item.get('style') == style:
                    score += 4

                scored.append((score, item))

            scored.sort(key=lambda x: x[0], reverse=True)

            # Диверсифікація: показуємо найкращі моделі різних брендів
            selected = []
            seen_vendors = set()

            for score, item in scored:
                v = (item.get('vendor') or 'zorro').lower().strip()
                if v not in seen_vendors:
                    selected.append(item)
                    seen_vendors.add(v)
                if len(selected) >= limit:
                    break

            if len(selected) < limit:
                for score, item in scored:
                    if item not in selected:
                        selected.append(item)
                    if len(selected) >= limit:
                        break

            conn.close()
            return selected
    except Exception as e:
        print(f"Помилка універсального пошуку: {e}")
        if conn:
            conn.close()
        return []

def render_rozetka_card(p: dict) -> str:
    prod_url = (p.get('url') or '').strip()
    if not prod_url or prod_url.rstrip('/') == 'https://zorro.com.ua':
        prod_url = f"https://zorro.com.ua/katalog/search/?q={urllib.parse.quote_plus(p['name'])}"

    img_src = p.get('image') or 'https://zorro.com.ua/content/images/no-image.png'
    
    # Витягуємо короткі гарні характеристики
    raw_p = p.get('raw_params') or ''
    if raw_p and len(raw_p) > 10:
        clean_parts = raw_p.split(' | ')[:3]
        short_desc = " • ".join(clean_parts)
    else:
        short_desc = p['description'][:100] + "..." if p.get('description') else "Оригінальний годинник із офіційною гарантією 12 міс."

    price_val = int(p['price']) if p.get('price') else 0
    brand_label = p.get('vendor') or 'ZORRO'
    safe_name = p['name'].replace('"', '&quot;')

    return f"""
    <div style="box-sizing:border-box; width:100%; background:#ffffff; border:1px solid #e2e5e8; border-radius:12px; padding:12px; margin-top:10px; box-shadow:0 1px 4px rgba(0,0,0,0.06); text-align:left;">
      <div style="display:flex; gap:12px; align-items:flex-start;">
        <a href="{prod_url}" target="_self" style="flex-shrink:0;">
          <img src="{img_src}" alt="{safe_name}" style="width:78px; height:78px; object-fit:contain; border-radius:8px; background:#f9fafb; border:1px solid #eee;">
        </a>
        <div style="flex:1; min-width:0;">
          <div style="font-size:10.5px; text-transform:uppercase; color:#888; font-weight:700; letter-spacing:0.4px; margin-bottom:2px;">{brand_label}</div>
          <a href="{prod_url}" target="_self" style="color:#222; text-decoration:none; font-weight:600; font-size:13.5px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;">
            {p['name']}
          </a>
          <div style="display:flex; align-items:center; justify-content:space-between; margin-top:8px;">
            <span style="font-size:16px; font-weight:700; color:#111;">{price_val} ₴</span>
            <a href="{prod_url}" target="_self" style="background:#00a046; color:#ffffff; padding:5px 12px; border-radius:6px; font-size:12px; font-weight:600; text-decoration:none; display:inline-block;">Купити</a>
          </div>
        </div>
      </div>
      
      <div style="font-size:11.5px; color:#555; margin-top:8px; line-height:1.35; border-top:1px dashed #e5e5e5; padding-top:6px;">
        {short_desc}
      </div>

      <div style="display:flex; gap:14px; margin-top:8px; padding-top:2px;">
        <span class="ai-card-action" data-action="similar" data-name="{safe_name}" style="cursor:pointer; font-size:11.5px; color:#00a046; font-weight:500; display:flex; align-items:center; gap:4px;">
          🔍 Схожі товари
        </span>
        <span class="ai-card-action" data-action="desc" data-name="{safe_name}" style="cursor:pointer; font-size:11.5px; color:#666; display:flex; align-items:center; gap:4px;">
          📄 Опис товару
        </span>
      </div>
    </div>
    """

def send_telegram_notification(phone: str, message: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    text = f"📞 <b>ЗАМОВЛЕННЯ ДЗВІНКА З ЧАТУ ZORRO!</b>\n\n<b>Телефон:</b> <code>{phone}</code>"
    if message:
        text += f"\n<b>Діалог/Товар:</b> {message}"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
        return True
    except Exception:
        return False

class MessageItem(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    current_product: Optional[str] = None
    history: Optional[List[MessageItem]] = []

class CallbackRequest(BaseModel):
    phone: str
    message: Optional[str] = ""

@app.get("/")
def root():
    return {"status": "ok", "architecture": "universal-faceted-search"}

@app.get("/api/sync-catalog")
def sync_catalog():
    return sync_all_catalogs()

@app.post("/api/callback")
async def callback(data: CallbackRequest):
    if not data.phone:
        return {"status": "error", "reply": "Вкажіть номер телефону."}
    send_telegram_notification(data.phone, data.message)
    return {
        "status": "success", 
        "reply": f"Дякуємо! Номер {data.phone} передано черговому консультанту. Очікуйте дзвінка найближчим часом!"
    }

@app.post("/api/chat")
async def chat(data: ChatRequest):
    matched_products = universal_catalog_search(data.message, limit=3)
    
    catalog_context = ""
    cards_html = []
    
    for p in matched_products:
        p_txt = p.get('raw_params') or p['description'][:100]
        catalog_context += f"• Модель: {p['name']} (Бренд: {p.get('vendor')}) | Ціна: {p['price']} грн | Параметри: {p_txt}\n"
        cards_html.append(render_rozetka_card(p))

    chips_html = """
    <div style="display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; padding-top:6px; border-top:1px solid #f0f0f0;">
      <button class="ai-chip" data-msg="Розкажи детальніше опис та характеристики цих моделей">📄 Опис моделей</button>
      <button class="ai-chip" data-msg="Порівняй ці моделі між собою">⚖️ Порівняти моделі</button>
      <button class="ai-chip" data-action="call-modal" style="background:#00a046; color:#fff; border-color:#00a046; font-weight:600;">📞 Замовити дзвінок менеджера</button>
    </div>
    """

    system_instruction = f"""
    Ти — професійний онлайн-консультант інтернет-магазину годинників ZORRO (zorro.com.ua).
    Поточний товар на сторінці (якщо є): {data.current_product or "каталог"}.

    ПІДІБРАНІ ВАРІАНТИ З БАЗИ КАТАЛОГУ:
    {catalog_context if catalog_context else "За даними критеріями товарів не знайдено."}

    СУВОРІ ПРАВИЛА КОНСУЛЬТАЦІЇ:
    1. Відповідай строго на тему запиту покупця. Якщо питають про жіночі годинники — розкажи про жіночі, якщо про дитячі — про дитячі, якщо про смарт — про смарт.
    2. Якщо товари знайдено — напиши 1-2 живих речення з порадою продавця, чому саме ці моделі найкраще підходять під запит.
    3. У тексті відповіді НЕ дублюй ціни та посилання (вони вже є в інтерактивних картках Rozetka нижче).
    4. Завжди завершуй відповідь одним відкритим питанням (наприклад: про переваги у кольорі, матеріалі ремінця чи допомогу з оформленням).
    5. Мова спілкування — виключно українська.
    """

    history_formatted = [{"role": m.role, "content": m.content} for m in data.history] if data.history else []
    ai_reply = call_gemini(
        prompt_text=f"Запит клієнта: {data.message}\nІсторія: {json.dumps(history_formatted, ensure_ascii=False)}",
        system_instruction=system_instruction,
        temperature=0.2
    )

    full_output = f"<div style='line-height:1.4;'>{ai_reply}</div>"
    if cards_html:
        full_output += "".join(cards_html)
    full_output += chips_html

    return {"reply": full_output}
