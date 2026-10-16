import os, re, json, html, logging, threading, time
import xml.etree.ElementTree as ET
from decimal import Decimal
import requests, psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

LOG = logging.getLogger('zorro')
logging.basicConfig(level=logging.INFO)

SHOP_XML = 'https://zorro.com.ua/content/export/c929bc168711aedbbcad10f5bb726db0.xml'
BEST_XML = 'https://support.best-time.biz/api/feed/drops/ua'

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['GET', 'POST'],
    allow_headers=['*']
)

class Msg(BaseModel):
    role: str
    content: str

class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    current_product: str | None = None
    history: list[Msg] = Field(default_factory=list)

class CallbackInput(BaseModel):
    phone: str
    message: str = ''

def db():
    return psycopg2.connect(
        os.environ['DATABASE_URL'],
        connect_timeout=8
    )

def catalog_rows():
    def xml(url):
        r = requests.get(
            url,
            timeout=90,
            headers={'User-Agent': 'Mozilla/5.0 ZorroCatalog/2.0'}
        )
        r.raise_for_status()
        return ET.fromstring(r.content)

    zr, br = xml(SHOP_XML), xml(BEST_XML)

    categories = {
        c.get('id'): (c.text or '').strip()
        for c in zr.findall('.//categories/category')
    }

    supplier = {
        p.get('id'): p
        for p in br.findall('.//offers/offer')
    }

    result = []

    for z in zr.findall('.//offers/offer'):
        sid = (z.findtext('vendorCode') or '').strip()
        s = supplier.get(sid)

        attrs = {
            p.get('name').strip(): (p.text or '').strip()
            for p in s.findall('param')
            if p.get('name') and (p.text or '').strip()
        } if s is not None else {}

        # Додаємо параметри з Хорошопу, якщо їх немає від постачальника
        for p in z.findall('param'):
            p_name = p.get('name', '').strip()
            p_val = (p.text or '').strip()
            if p_name and p_val and p_name not in attrs:
                attrs[p_name] = p_val

        try:
            price = Decimal((z.findtext('price') or '').replace(',', '.'))
        except Exception:
            continue

        item = (
            z.get('id'),
            sid,
            (s.findtext('vendorCode') if s is not None else '') or sid,
            (z.findtext('name') or '').strip(),
            categories.get(z.findtext('categoryId'), ''),
            price,
            z.get('available') == 'true',
            (z.findtext('vendor') or '').strip(),
            (z.findtext('description') or '').strip(),
            (z.findtext('url') or '').strip(),
            (z.findtext('picture') or '').strip(),
            json.dumps(attrs, ensure_ascii=False)
        )

        if item[0] and item[3]:
            result.append(item)

    if len(result) < 50:
        raise RuntimeError('Каталог підозріло малий, синхронізацію скасовано')

    return result

def refresh():
    rows = catalog_rows()
    conn = db()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS zorro_products_v2 (
                        site_id TEXT PRIMARY KEY,
                        supplier_id TEXT,
                        sku TEXT,
                        name TEXT,
                        category TEXT,
                        price NUMERIC(12,2),
                        available BOOLEAN,
                        vendor TEXT,
                        description TEXT,
                        url TEXT,
                        image TEXT,
                        attributes JSONB,
                        synced_at TIMESTAMPTZ DEFAULT NOW()
                    )
                ''')

                execute_batch(
                    cur,
                    '''
                    INSERT INTO zorro_products_v2
                    (
                        site_id, supplier_id, sku, name,
                        category, price, available, vendor,
                        description, url, image, attributes
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (site_id)
                    DO UPDATE SET
                        supplier_id=EXCLUDED.supplier_id,
                        sku=EXCLUDED.sku,
                        name=EXCLUDED.name,
                        category=EXCLUDED.category,
                        price=EXCLUDED.price,
                        available=EXCLUDED.available,
                        vendor=EXCLUDED.vendor,
                        description=EXCLUDED.description,
                        url=EXCLUDED.url,
                        image=EXCLUDED.image,
                        attributes=EXCLUDED.attributes,
                        synced_at=NOW()
                    ''',
                    rows,
                    page_size=100
                )

                ids = [x[0] for x in rows]
                cur.execute(
                    '''
                    UPDATE zorro_products_v2
                    SET available=FALSE
                    WHERE NOT(site_id=ANY(%s))
                    ''',
                    (ids,)
                )

        LOG.info('Catalog synchronized: %d products', len(rows))
    finally:
        conn.close()

def worker():
    while True:
        try:
            refresh()
        except Exception:
            LOG.exception('Помилка синхронізації каталогу')
        time.sleep(6 * 3600)

@app.on_event('startup')
def start():
    if os.getenv('DATABASE_URL'):
        threading.Thread(target=worker, daemon=True).start()
    else:
        LOG.error('DATABASE_URL відсутня')

def intent_from(text):
    q = text.casefold()

    m = re.search(r'(?:до|не більше|максимум|бюджет)\s*(\d[\d\s]*)\s*(?:грн|₴)?', q)

    audience = None
    if re.search(r'дитяч|для дитин|дитині|хлопчик|дівчат', q):
        audience = 'kids'
    elif re.search(r'жіноч|для жінк|дівчин', q):
        audience = 'women'
    elif re.search(r'чоловіч|для чоловік|хлопц', q):
        audience = 'men'

    category_type = None
    if re.search(r'смарт|smart|розумн', q):
        category_type = 'smart'
    elif re.search(r'механ|автозавод|скелетон', q):
        category_type = 'mechanical'

    style = None
    if re.search(r'спорт|sport', q):
        style = 'sport'
    elif re.search(r'тактич|військов|мілітарі', q):
        style = 'tactical'
    elif re.search(r'класич|елегант', q):
        style = 'classic'

    return {
        'max_price': int(m.group(1).replace(' ', '')) if m else None,
        'audience': audience,
        'category_type': category_type,
        'style': style,
        'calls': bool(re.search(r'дзвін|виклики|розмов|говорити|sim|сім', q)),
        'waterproof': bool(re.search(r'водонепроник|плаван|басейн|купат', q)),
        'skeleton': bool(re.search(r'скелетон|прозор', q)),
        'leather': bool(re.search(r'шкірян|кожан', q)),
        'steel': bool(re.search(r'сталев|метал|сталь', q))
    }

def get_intent(data):
    result = {}
    history_messages = [m.content for m in data.history if m.role in ('user', 'human')]
    history_messages.append(data.message)

    for msg in history_messages[-4:]:
        parsed = intent_from(msg)
        for k, v in parsed.items():
            if v:
                result[k] = v
    return result

def select_products(rows, intent, limit=3):
    scored = []

    for p in rows:
        if not p.get('available') or not p.get('url') or p.get('price') is None:
            continue

        price = float(p['price'])
        if intent.get('max_price') and price > intent['max_price']:
            continue

        name_l = (p.get('name') or '').casefold()
        desc_l = (p.get('description') or '').casefold()
        cat_l = (p.get('category') or '').casefold()
        a = p.get('attributes') or {}
        attrs_str = ' '.join(f"{k} {v}" for k, v in a.items()).casefold()
        full_text = f"{name_l} {desc_l} {cat_l} {attrs_str}"

        # 1. Жорсткі обмеження (Harness)
        if intent.get('audience') == 'kids':
            if not any(k in full_text for k in ['дитяч', 'для дітей', 'дитині', 'хлопчик', 'дівчат', 'baby', 'q100']):
                continue
        elif intent.get('audience') == 'women':
            if any(k in full_text for k in ['чоловіч', 'для чоловіків']) and not any(k in full_text for k in ['жіноч', 'для жінок']):
                continue
        elif intent.get('audience') == 'men':
            if any(k in full_text for k in ['жіноч', 'для жінок']) and not any(k in full_text for k in ['чоловіч', 'для чоловіків']):
                continue

        if intent.get('category_type') == 'smart':
            if not any(k in full_text for k in ['smart', 'смарт', 'watch']):
                continue
        elif intent.get('category_type') == 'mechanical':
            if not any(k in full_text for k in ['механ', 'автозавод']):
                continue

        if intent.get('calls'):
            if not any(k in full_text for k in ['дзвін', 'виклики', 'розмов', 'sim', 'сім', 'мікрофон', 'bluetooth call']):
                continue

        # 2. Бали відповідності
        score = 0
        if intent.get('style') == 'sport' and any(k in full_text for k in ['спорт', 'sport', 's-shock', 'секундомір']):
            score += 10
        if intent.get('style') == 'classic' and any(k in full_text for k in ['класич', 'елегант']):
            score += 10
        if intent.get('style') == 'tactical' and any(k in full_text for k in ['тактич', 'військов', 'мілітарі', 'amst']):
            score += 10

        if intent.get('waterproof') and any(k in full_text for k in ['плавання', 'басейн', '5 атм', '10 атм', '50 м', '100 м', 'водонепроник']):
            score += 15

        if intent.get('skeleton') and any(k in full_text for k in ['скелетон', 'skeleton', 'прозора задня кришка']):
            score += 10

        if intent.get('steel') and any(k in full_text for k in ['сталев', 'метал', 'сталь']):
            score += 8
        if intent.get('leather') and any(k in full_text for k in ['шкірян', 'кожан']):
            score += 8

        scored.append((score, p))

    # Сортування: найбільше балів, диверсифікація
    scored.sort(key=lambda t: (-t[0], t[1]['price']))

    selected = []
    seen_vendors = set()
    for _, item in scored:
        v = (item.get('vendor') or 'zorro').casefold()
        if v not in seen_vendors:
            selected.append(item)
            seen_vendors.add(v)
        if len(selected) >= limit:
            break

    if len(selected) < limit:
        for _, item in scored:
            if item not in selected:
                selected.append(item)
            if len(selected) >= limit:
                break

    return selected

def gemini(message, products):
    key = os.getenv('GEMINI_API_KEY')
    if not key or not products:
        return ''

    facts = [
        {
            'name': p['name'],
            'price': float(p['price']),
            'vendor': p.get('vendor', 'ZORRO'),
            'features': list((p.get('attributes') or {}).items())[:4]
        }
        for p in products
    ]

    try:
        r = requests.post(
            'https://generativelanguage.googleapis.com/v1beta/models/'
            + os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
            + ':generateContent',
            headers={'x-goog-api-key': key},
            json={
                'systemInstruction': {
                    'parts': [{
                        'text': (
                            'Ти досвідчений продавець-консультант магазину годинників ZORRO (zorro.com.ua). '
                            'Відповідай виключно українською мовою. '
                            'Твоя мета — допомогти клієнту визначитися з вибором. '
                            'Опиши в 1-2 реченнях, чому відібрані варіанти підходять під його запит. '
                            'Не перераховуй ціни та назви, оскільки графічні картки додаються системою. '
                            'В кінці обов\'язково постав одне коротке ввічливе відкрите запитання.'
                        )
                    }]
                },
                'contents': [{
                    'role': 'user',
                    'parts': [{
                        'text': f"Запит клієнта: {message}\nПідібрані моделі: {json.dumps(facts, ensure_ascii=False)}"
                    }]
                }]
            },
            timeout=10
        )
        r.raise_for_status()
        return ''.join(t.get('text', '') for t in r.json()['candidates'][0]['content']['parts']).strip()
    except Exception:
        LOG.exception('Помилка Gemini API')
        return ''

def product_html(p):
    name = html.escape(p['name'])
    raw_url = (p.get('url') or '').strip()
    if not raw_url or raw_url.rstrip('/') == 'https://zorro.com.ua':
        raw_url = f"https://zorro.com.ua/katalog/search/?q={urllib.parse.quote_plus(p['name'])}"
    url = html.escape(raw_url, quote=True)
    img = html.escape(p.get('image') or 'https://zorro.com.ua/content/images/no-image.png', quote=True)
    price_val = int(p['price']) if p.get('price') else 0
    vendor = html.escape(p.get('vendor') or 'ZORRO')

    attrs = p.get('attributes') or {}
    short_specs = ' • '.join([f"{k}: {v}" for k, v in list(attrs.items())[:3]]) if attrs else (p.get('description', '')[:90] + '...')

    return f"""
    <div style="box-sizing:border-box; width:100%; background:#ffffff; border:1px solid #e2e5e8; border-radius:12px; padding:12px; margin-top:10px; box-shadow:0 1px 4px rgba(0,0,0,0.06); text-align:left;">
      <div style="display:flex; gap:12px; align-items:flex-start;">
        <a href="{url}" target="_self" style="flex-shrink:0;">
          <img src="{img}" alt="{name}" style="width:78px; height:78px; object-fit:contain; border-radius:8px; background:#f9fafb; border:1px solid #eee;">
        </a>
        <div style="flex:1; min-width:0;">
          <div style="font-size:10.5px; text-transform:uppercase; color:#888; font-weight:700; letter-spacing:0.4px; margin-bottom:2px;">{vendor}</div>
          <a href="{url}" target="_self" style="color:#222; text-decoration:none; font-weight:600; font-size:13.5px; line-height:1.3; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;">
            {name}
          </a>
          <div style="display:flex; align-items:center; justify-content:space-between; margin-top:8px;">
            <span style="font-size:16px; font-weight:700; color:#111;">{price_val} ₴</span>
            <a href="{url}" target="_self" style="background:#00a046; color:#ffffff; padding:5px 12px; border-radius:6px; font-size:12px; font-weight:600; text-decoration:none; display:inline-block;">Купити</a>
          </div>
        </div>
      </div>
      <div style="font-size:11.5px; color:#555; margin-top:8px; line-height:1.35; border-top:1px dashed #e5e5e5; padding-top:6px;">
        {short_specs}
      </div>
      <div style="display:flex; gap:14px; margin-top:8px; padding-top:2px;">
        <span class="ai-card-action" data-action="similar" data-name="{name}" style="cursor:pointer; font-size:11.5px; color:#00a046; font-weight:500; display:flex; align-items:center; gap:4px;">
          🔍 Схожі товари
        </span>
        <span class="ai-card-action" data-action="desc" data-name="{name}" style="cursor:pointer; font-size:11.5px; color:#666; display:flex; align-items:center; gap:4px;">
          📄 Опис товару
        </span>
      </div>
    </div>
    """

@app.get('/')
def root():
    return {'status': 'ok', 'version': 'fast-v3'}

@app.get('/api/sync-catalog')
def sync_endpoint():
    try:
        refresh()
        return {'status': 'success', 'message': 'Каталог успішно синхронізовано'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

@app.post('/api/chat')
def chat(data: ChatInput):
    try:
        conn = db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('''
                    SELECT name, category, price, available, vendor, description, url, image, attributes
                    FROM zorro_products_v2
                    WHERE available=TRUE
                ''')
                rows = [dict(x) for x in cur.fetchall()]
        finally:
            conn.close()
    except Exception:
        LOG.exception('Помилка читання з бази')
        return {'reply': 'Каталог тимчасово недоступний. Спробуйте через хвилину.'}

    if not rows:
        return {'reply': 'Каталог завантажується, будь ласка, зачекайте кілька секунд.'}

    intent = get_intent(data)
    picks = select_products(rows, intent, limit=3)

    chips_html = """
    <div style="display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; padding-top:6px; border-top:1px solid #f0f0f0;">
      <button class="ai-chip" data-msg="Розкажи детальніше опис та характеристики цих моделей">📄 Опис моделей</button>
      <button class="ai-chip" data-msg="Порівняй ці моделі між собою">⚖️ Порівняти моделі</button>
      <button class="ai-chip" data-action="call-modal" style="background:#00a046; color:#fff; border-color:#00a046; font-weight:600;">📞 Замовити дзвінок менеджера</button>
    </div>
    """

    if not picks:
        return {
            'reply': (
                'Не вдалося знайти моделі, які повністю відповідають усім заданим критеріям. '
                'Спробуйте змінити бюджет або уточнити інший стиль, і я підберу чудові варіанти!'
                + chips_html
            )
        }

    ai_text = gemini(data.message, picks) or 'Ось чудові варіанти за вашим запитом. Яка з цих моделей вам найбільше до вподоби?'
    full_output = f"<div style='line-height:1.4;'>{html.escape(ai_text).replace(chr(10), '<br>')}</div>"
    full_output += "".join(product_html(p) for p in picks)
    full_output += chips_html

    return {'reply': full_output}

@app.post('/api/callback')
def callback(data: CallbackInput):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')

    if not token or not chat_id:
        return {'status': 'error', 'reply': 'Сервіс заявок тимчасово ненастроєний.'}

    try:
        msg = f"📞 <b>Новий запит із чату ZORRO:</b> <code>{data.phone}</code>\n<b>Контекст:</b> {data.message}"
        r = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'},
            timeout=8
        )
        r.raise_for_status()
        return {'status': 'success', 'reply': 'Дякуємо! Номер передано менеджеру, ми зателефонуємо вам найближчим часом.'}
    except Exception:
        LOG.exception('Помилка відправки в Telegram')
        return {'status': 'error', 'reply': 'Не вдалося надіслати заявку. Зателефонуйте нам напряму.'}
