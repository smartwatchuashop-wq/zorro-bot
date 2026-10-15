
import os, re, json, html, logging, threading, time
import xml.etree.ElementTree as ET
from decimal import Decimal
import requests, psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

LOG = logging.getLogger('zorro')
logging.basicConfig(level=logging.INFO)

SHOP_XML = 'https://zorro.com.ua/content/export/c929bc168711aedbbcad10f5bb726db0.xml'
BEST_XML = 'https://support.best-time.biz/api/feed/drops/ua'

WATCHES = {
    'Чоловічі годинники',
    'Жіночі годинники',
    'Дитячі годинники',
    'Смарт годинники',
    'Спортивні годинники',
    'Унісекс годинники'
}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        'https://zorro.com.ua',
        'https://www.zorro.com.ua'
    ],
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

        try:
            price = Decimal(
                (z.findtext('price') or '').replace(',', '.')
            )
        except Exception:
            continue

        item = (
            z.get('id'),
            sid,
            (s.findtext('vendorCode') if s is not None else '') or '',
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

    if len(result) < 100:
        raise RuntimeError(
            'Catalog suspiciously small; refusing synchronization'
        )

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
                    VALUES (
                        %s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s::jsonb
                    )
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

        LOG.info(
            'Catalog synchronized: %d products',
            len(rows)
        )

    finally:
        conn.close()


def worker():

    while True:
        try:
            refresh()
        except Exception:
            LOG.exception(
                'Catalog synchronization failed; retaining previous data'
            )

        time.sleep(6 * 3600)


@app.on_event('startup')
def start():

    if os.getenv('DATABASE_URL'):
        threading.Thread(
            target=worker,
            daemon=True
        ).start()
    else:
        LOG.error('DATABASE_URL missing')


def intent_from(text):

    q = text.casefold()

    m = re.search(
        r'(?:до|не більше|максимум)\s*(\d[\d\s]*)\s*(?:грн|₴)?',
        q
    )

    category = (
        'Дитячі годинники'
        if re.search(r'дитяч|для дитини', q)
        else 'Жіночі годинники'
        if re.search(r'жіноч|для жінки', q)
        else 'Чоловічі годинники'
        if re.search(r'чоловіч|для чоловіка', q)
        else None
    )

    strap = (
        'steel'
        if re.search(
            r'металев\w*\s+браслет|сталев\w*\s+браслет|браслет\w*\s+з\s+метал',
            q
        )
        else 'leather'
        if re.search(r'шкірян\w*\s+ремін', q)
        else None
    )

    return {
        'max_price': int(m.group(1).replace(' ', '')) if m else None,
        'category': category,
        'strap': strap,
        'smart': bool(
            re.search(r'смарт|розумн\w*\s+годин', q)
        ),
        'mechanical': bool(re.search(r'механіч', q))
    }


def get_intent(data):

    result = {}

    for m in [
        *data.history[-12:],
        Msg(role='user', content=data.message)
    ]:

        if m.role not in ('user', 'human'):
            continue

        q = m.content.casefold()

        if re.search(r'без обмеження бюджету', q):
            result.pop('max_price', None)

        if re.search(
            r'(неважливо|не обов.язково).{0,20}(браслет|ремін)',
            q
        ):
            result.pop('strap', None)

        for key, value in intent_from(m.content).items():

            if value is not None and value is not False:
                result[key] = value

    return result


def is_metal(a):

    val = (
        a.get('Матеріал ремінця', '') + ' ' +
        a.get('Матеріал браслета', '')
    ).casefold()

    return bool(
        re.search(r'сталев|сталь|метал|нержаві', val)
    )


def select_products(rows, intent, limit=3):

    scored = []

    for p in rows:

        if (
            not p['available']
            or p['category'] not in WATCHES
            or not p['url']
            or p['price'] is None
        ):
            continue

        if (
            intent.get('max_price') is not None
            and p['price'] > intent['max_price']
        ):
            continue

        if (
            intent.get('category')
            and p['category'] != intent['category']
        ):
            continue

        if (
            intent.get('smart')
            and p['category'] != 'Смарт годинники'
        ):
            continue

        a = p['attributes'] or {}

        if (
            intent.get('strap') == 'steel'
            and not is_metal(a)
        ):
            continue

        if (
            intent.get('strap') == 'leather'
            and 'шкір' not in a.get(
                'Матеріал ремінця', ''
            ).casefold()
        ):
            continue

        if (
            intent.get('mechanical')
            and 'механ' not in (
                a.get('Механізм', '') + ' ' +
                a.get('Тип механізму', '')
            ).casefold()
        ):
            continue

        scored.append((len(a), p))

    scored.sort(
        key=lambda t: (
            -t[0],
            t[1]['price'],
            t[1]['name']
        )
    )

    return [p for _, p in scored[:limit]]


def gemini(message, products):

    key = os.getenv('GEMINI_API_KEY')

    if not key:
        return ''

    facts = [
        {
            'name': p['name'],
            'price': float(p['price']),
            'attributes': {
                k: v
                for k, v in p['attributes'].items()
                if k in (
                    'Матеріал ремінця',
                    'Водостійкість',
                    'Механізм',
                    'Функції',
                    'Колір корпусу',
                    'Основний колір циферблата'
                )
            }
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
                        'text':
                        'Ти консультант Zorro, відповідай українською. '
                        'Використовуй лише надані факти, '
                        'не вигадуй функції або характеристики. '
                        'Не пропонуй інших моделей. '
                        'Коротко поясни вибір, '
                        'постав одне доречне запитання.'
                    }]
                },
                'contents': [{
                    'role': 'user',
                    'parts': [{
                        'text': message
                        + '\nПеревірені товари: '
                        + json.dumps(
                            facts,
                            ensure_ascii=False
                        )
                    }]
                }]
            },
            timeout=18
        )

        r.raise_for_status()

        return ''.join(
            t.get('text', '')
            for t in r.json()['candidates'][0]['content']['parts']
        ).strip()

    except Exception:
        LOG.exception('Gemini error')
        return ''


def product_html(p):

    name = html.escape(p['name'])
    url = html.escape(p['url'], quote=True)
    img = html.escape(p['image'] or '', quote=True)

    return (
        f'<div style="border:1px solid #ddd;'
        f'border-radius:12px;padding:12px;margin-top:10px">'
        f'<a href="{url}" target="_self">'
        f'<img src="{img}" alt="{name}" '
        f'style="width:78px;height:78px;object-fit:contain">'
        f'</a>'
        f'<div><b>{name}</b></div>'
        f'<div>{int(p["price"])} ₴</div>'
        f'<a href="{url}" target="_self">'
        f'Переглянути товар</a>'
        f'</div>'
    )


@app.get('/')
def root():

    return {
        'status': 'ok',
        'version': 'v2'
    }


@app.get('/api/catalog-status')
def catalog_status():

    try:
        conn = db()

        try:
            with conn.cursor() as cur:

                cur.execute(
                    '''
                    SELECT
                        COUNT(*),
                        COUNT(*) FILTER (WHERE available),
                        MAX(synced_at)
                    FROM zorro_products_v2
                    '''
                )

                count, available, updated = cur.fetchone()

            return {
                'status': 'ok',
                'products': count,
                'available': available,
                'last_sync': (
                    updated.isoformat() if updated else None
                )
            }

        finally:
            conn.close()

    except Exception:
        return {
            'status': 'loading_or_error',
            'message':
            'Catalog not ready; inspect Render Logs'
        }


@app.post('/api/chat')
def chat(data: ChatInput):

    try:
        conn = db()

        try:
            with conn.cursor(
                cursor_factory=RealDictCursor
            ) as cur:

                cur.execute(
                    '''
                    SELECT
                        name, category, price, available,
                        vendor, description, url, image,
                        attributes
                    FROM zorro_products_v2
                    WHERE available=TRUE
                    '''
                )

                rows = [
                    dict(x)
                    for x in cur.fetchall()
                ]

        finally:
            conn.close()

    except Exception:
        LOG.exception('Database unavailable')

        return {
            'reply':
            'Каталог зараз оновлюється. '
            'Спробуйте ще раз за хвилину.'
        }

    if not rows:
        return {
            'reply':
            'Каталог зараз завантажується. '
            'Спробуйте через хвилину.'
        }

    picks = select_products(
        rows,
        get_intent(data)
    )

    if not picks:
        return {
            'reply':
            'Не знайшов товарів з підтвердженими '
            'характеристиками за всіма умовами. '
            'Яку вимогу можемо змінити?'
        }

    answer = (
        gemini(data.message, picks)
        or 'Ось годинники, що відповідають '
           'підтвердженим вимогам. '
           'Який із них вам подобається найбільше?'
    )

    return {
        'reply':
        '<div>'
        + html.escape(answer).replace('\n', '<br>')
        + '</div>'
        + ''.join(
            product_html(p)
            for p in picks
        )
    }


@app.post('/api/callback')
def callback(data: CallbackInput):

    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')

    if not token or not chat_id:
        return {
            'status': 'error',
            'reply': 'Заявка тимчасово недоступна.'
        }

    try:

        msg = (
            f'📞 Новий запит ZORRO: {data.phone}\n'
            f'{data.message}'
        )

        r = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={
                'chat_id': chat_id,
                'text': msg
            },
            timeout=8
        )

        r.raise_for_status()

        if not r.json().get('ok'):
            raise ValueError(
                'Telegram rejected request'
            )

        return {
            'status': 'success',
            'reply':
            'Дякуємо! Ваш номер передано менеджеру.'
        }

    except Exception:
        LOG.exception('Telegram callback failed')

        return {
            'status': 'error',
            'reply':
            'Не вдалося передати номер. '
            'Зверніться до магазину напряму.'
        }

