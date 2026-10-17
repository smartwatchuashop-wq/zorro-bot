import os
import re
import json
import html
import logging
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from decimal import Decimal
import requests
import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

LOG = logging.getLogger('zorro')
logging.basicConfig(level=logging.INFO)

SHOP_XML = 'https://zorro.com.ua/content/export/c929bc168711aedbbcad10f5bb726db0.xml'
BEST_XML_LIST = [
    'https://best-time.biz/ua.xml',
    'https://support.best-time.biz/api/feed/drops/ua'
]

CANDIDATE_MODELS = [
    'gemini-2.5-flash',
    'gemini-2.0-flash',
    'gemini-1.5-flash',
    'gemini-2.5-pro'
]

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
    state: dict = Field(default_factory=dict)

class CallbackInput(BaseModel):
    phone: str
    message: str = ''

def db():
    return psycopg2.connect(
        os.environ['DATABASE_URL'],
        connect_timeout=10
    )

def clean_html_tags(raw_text: str) -> str:
    if not raw_text:
        return ''
    return re.sub(r'<.*?>', ' ', raw_text).strip()

def fetch_xml(url: str):
    r = requests.get(
        url,
        timeout=90,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZorroCatalog/3.0'}
    )
    r.raise_for_status()
    return ET.fromstring(r.content)

def catalog_rows():
    zr = fetch_xml(SHOP_XML)

    br = None
    for b_url in BEST_XML_LIST:
        try:
            br = fetch_xml(b_url)
            if br is not None:
                break
        except Exception as e:
            LOG.warning(f"Не вдалося завантажити {b_url}: {e}")

    categories_z = {
        c.get('id'): (c.text or '').strip()
        for c in zr.findall('.//categories/category')
    }

    categories_b = {}
    supplier_by_id = {}
    supplier_by_vcode = {}

    if br is not None:
        categories_b = {
            c.get('id'): (c.text or '').strip()
            for c in br.findall('.//categories/category')
        }
        for p in br.findall('.//offers/offer'):
            oid = (p.get('id') or '').strip()
            vcode = (p.findtext('vendorCode') or '').strip()
            if oid:
                supplier_by_id[oid] = p
            if vcode:
                supplier_by_vcode[vcode] = p

    result = []

    for z in zr.findall('.//offers/offer'):
        site_id = (z.get('id') or '').strip()
        sid = (z.findtext('vendorCode') or '').strip()

        # Шукаємо відповідність у BestTime і за vendorCode, і за id
        s = supplier_by_vcode.get(sid) or supplier_by_id.get(sid) or supplier_by_id.get(site_id)

        attrs = {}
        if s is not None:
            b_cat_id = (s.findtext('categoryId') or '').strip()
            if b_cat_id in categories_b:
                attrs['Категорія постачальника'] = categories_b[b_cat_id]
            for p in s.findall('param'):
                k = (p.get('name') or '').strip()
                v = (p.text or '').strip()
                if k and v:
                    attrs[k] = v

        # Доповнюємо параметрами із самого сайту Zorro (Хорошоп)
        for p in z.findall('param'):
            k = (p.get('name') or '').strip()
            v = (p.text or '').strip()
            if k and v and k not in attrs:
                attrs[k] = v

        try:
            price = Decimal((z.findtext('price') or '').replace(',', '.'))
        except Exception:
            continue

        name = (z.findtext('name') or z.findtext('name_ua') or '').strip()
        cat_name = categories_z.get((z.findtext('categoryId') or '').strip(), '')
        if not cat_name and 'Категорія постачальника' in attrs:
            cat_name = attrs['Категорія постачальника']

        vendor = (z.findtext('vendor') or (s.findtext('vendor') if s is not None else '') or '').strip()
        desc = clean_html_tags(
            z.findtext('description') or z.findtext('description_ua') or
            (s.findtext('description_ua') if s is not None else '') or ''
        )

        raw_url = (z.findtext('url') or '').strip()
        if not raw_url or raw_url.rstrip('/') == 'https://zorro.com.ua':
            raw_url = f"https://zorro.com.ua/katalog/search/?q={urllib.parse.quote_plus(name)}"

        pic_el = z.find('picture')
        image = pic_el.text.strip() if (pic_el is not None and pic_el.text) else ''
        if not image and s is not None:
            s_pic = s.find('picture')
            if s_pic is not None and s_pic.text:
                image = s_pic.text.strip()

        available = (z.get('available') == 'true')

        if site_id and name:
            result.append((
                site_id,
                sid,
                (s.findtext('vendorCode') if s is not None else '') or sid,
                name,
                cat_name,
                price,
                available,
                vendor,
                desc,
                raw_url,
                image,
                json.dumps(attrs, ensure_ascii=False)
            ))

    if len(result) < 50:
        raise RuntimeError('Каталог занадто малий, синхронізацію скасовано')

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
        LOG.info('Каталог синхронізовано: %d товарів', len(rows))
    finally:
        conn.close()

def worker():
    while True:
        try:
            refresh()
        except Exception:
            LOG.exception('Помилка фонової синхронізації')
        time.sleep(6 * 3600)

@app.on_event('startup')
def start():
    if os.getenv('DATABASE_URL'):
        threading.Thread(target=worker, daemon=True).start()
    else:
        LOG.error('DATABASE_URL відсутня')

# =====================================================================
# ТАРГЕТОВАНИЙ ПОШУКОВИЙ РУШІЙ (МІНУС-СЛОВА + КЛЮЧОВІ СЛОВА + ВАГИ)
# =====================================================================

NEGATIVE_ACCESSORY_WORDS = (
    'зарядн', 'кабель', 'провід', 'шнур', 'док-станц',
    'ремінець', 'браслет до', 'браслет для', 'ремінець для',
    'коробочк', 'коробка', 'скриньк', 'футляр', 'подарунков',
    'корпус до', 'захисне скло', 'плівка', 'рюкзак', 'послуг'
)

BRANDS_MAP = {
    'skmei': 'skmei', 'скмей': 'skmei', 'скмеі': 'skmei',
    'winner': 'winner', 'віннер': 'winner', 'вінер': 'winner',
    'jaragar': 'jaragar', 'джарагар': 'jaragar',
    'forsining': 'forsining', 'форсінінг': 'forsining',
    'geneva': 'geneva', 'женева': 'geneva',
    'amst': 'amst', 'амст': 'amst',
    'weide': 'weide', 'вейде': 'weide',
    'uwatch': 'uwatch', 'ювотч': 'uwatch',
    'curren': 'curren', 'каррен': 'curren', 'карен': 'curren',
    'pagani': 'pagani', 'пагані': 'pagani',
    'north edge': 'north edge', 'норт едж': 'north edge', 'норс едж': 'north edge',
    'lige': 'lige', 'лідж': 'lige', 'ліге': 'lige',
    'lobinni': 'lobinni', 'лобінні': 'lobinni',
    'megalith': 'megalith', 'мегаліт': 'megalith',
    'sanda': 'sanda', 'санда': 'sanda',
    'smael': 'smael', 'смаел': 'smael',
    'crrju': 'crrju', 'cheetah': 'cheetah',
    'ailang': 'ailang', 'айланг': 'ailang',
    'aesop': 'aesop', 'езоп': 'aesop',
    'besta': 'besta', 'беста': 'besta',
    'carnival': 'carnival', 'карнівал': 'carnival',
    'hemsut': 'hemsut', 'хемсут': 'hemsut',
    'guanquin': 'guanquin', 'megir': 'megir', 'мегір': 'megir',
    'naviforce': 'naviforce', 'навіфорс': 'naviforce',
    'sinobi': 'sinobi', 's-shock': 's-shock', 'reginald': 'reginald'
}

STOP_WORDS = {
    'годинник', 'годинники', 'годинника', 'наручний', 'наручні', 'модель', 'моделі',
    'порадь', 'порадьте', 'підкажи', 'підкажіть', 'підбери', 'підберіть', 'покажи',
    'покажіть', 'хочу', 'купити', 'шукаю', 'потрібен', 'потрібні', 'який', 'які',
    'яка', 'мені', 'для', 'під', 'або', 'чи', 'та', 'що', 'це', 'вітаю', 'добрий',
    'день', 'ціна', 'грн', 'варіант', 'варіанти', 'надійний', 'надійні', 'хороший',
    'кращий', 'найкращі', 'стильний', 'стильні', 'будь', 'ласка', 'розкажи', 'детальніше',
    'опис', 'характеристики', 'цих', 'моделей', 'порівняй', 'між', 'собою'
}

def is_followup_question(text: str) -> bool:
    q = text.casefold()
    followup_triggers = (
        'розкажи детальніше', 'опис моделей', 'опис товару',
        'порівняй', 'чим вони відрізняються', 'який з них кращ',
        'а у першого', 'а у другого', 'а у третього', 'про перший', 'про другий'
    )
    return any(t in q for t in followup_triggers)

def parse_single_message(text: str) -> dict:
    q = text.casefold()
    res = {}

    # 1. Чи просить клієнт сам аксесуар (ремінець / зарядку / коробку)?
    if re.search(r'ремінець|ремінці|браслет для|зарядк|зарядн|кабель|коробк|коробочк|скриньк|футляр', q):
        res['wants_accessory'] = True
        if 'ремінець' in q or 'ремінці' in q:
            res['accessory_type'] = 'ремінець'
        elif 'заряд' in q or 'кабель' in q:
            res['accessory_type'] = 'зарядн'
        elif 'короб' in q or 'скриньк' in q:
            res['accessory_type'] = 'короб'
    elif re.search(r'годинник|модел|смарт|механ|кварц|скелетон', q):
        res['wants_accessory'] = False

    # 2. Ціна (від / до)
    m_max = re.search(r'(?:до|не більше|максимум|бюджет|дешевше)\s*(\d[\d\s]*)\s*(?:грн|₴)?', q)
    if m_max:
        res['max_price'] = int(m_max.group(1).replace(' ', ''))
    m_min = re.search(r'(?:від|дорожче|мінімум)\s*(\d[\d\s]*)\s*(?:грн|₴)?', q)
    if m_min:
        res['min_price'] = int(m_min.group(1).replace(' ', ''))

    # 3. Цільова аудиторія (Чоловічі / Жіночі / Дитячі)
    if re.search(r'дитяч|дитині|для дитин|хлопчик|дівчинк|школ|малюк', q):
        res['audience'] = 'kids'
    elif re.search(r'жіноч|для жінк|жінці|дівчині|для дівчат|леді', q):
        res['audience'] = 'women'
    elif re.search(r'чоловіч|для чоловік|чоловіку|хлопцю|шефу|батьку|татові', q):
        res['audience'] = 'men'

    # 4. Тип годинника (Смарт / Механіка / Кварц)
    if re.search(r'смарт|smart|розумн|фітнес|сенсорн', q):
        res['watch_type'] = 'smart'
    elif re.search(r'механ|автозавод|скелетон|турбілліон|турбійон|без батарейк', q):
        res['watch_type'] = 'mechanical'
    elif re.search(r'кварц|стрілочн|на батарейц', q):
        res['watch_type'] = 'quartz'

    # 5. Стиль
    if re.search(r'тактич|військов|мілітарі|армійськ|для зсу|зсу|камуфляж', q):
        res['style'] = 'tactical'
    elif re.search(r'спорт|sport|біг|тренуван|активн|протиудар|ударостійк', q):
        res['style'] = 'sport'
    elif re.search(r'класич|класик|елегант|ділов|під костюм|офісн', q):
        res['style'] = 'classic'
    elif re.search(r'патріот|тризуб|україн', q):
        res['style'] = 'patriotic'

    # 6. Ремінець / Браслет
    if re.search(r'шкірян|шкіра|кожан', q):
        res['strap'] = 'leather'
    elif re.search(r'сталев|сталь|метал|титан', q):
        res['strap'] = 'steel'
    elif re.search(r'каучук|силікон|гумов|полімер|rubber', q):
        res['strap'] = 'rubber'

    # 7. Водозахист
    if re.search(r'плаван|басейн|купат|пірнат|водонепроник|5\s*атм|10\s*атм|50\s*м|100\s*м', q):
        res['waterproof'] = 'swimming'
    elif re.search(r'водостійк|мити руки|дощ|3\s*атм', q):
        res['waterproof'] = 'rain'

    # 8. Спеціальні функції та параметри
    if re.search(r'дзвін|виклик|розмов|говорити|мікрофон|динамік|sim|сім-карт', q):
        res['calls'] = True
        res['watch_type'] = 'smart'
    if re.search(r'\bgps\b|джпс|геолокац|місцезнаходжен|відстежен|sos|прослуховуван', q):
        res['gps'] = True
    if re.search(r'скелетон|прозор\w*\s+кришк|видно механізм', q):
        res['skeleton'] = True
    if re.search(r'автозавод', q):
        res['autowind'] = True
    if re.search(r'пульс|тиск|крокомір|сон|здоров', q):
        res['health'] = True
    if re.search(r'римськ', q):
        res['roman'] = True
    if re.search(r'арабськ', q):
        res['arabic'] = True
    if re.search(r'фосфор|люмінесцент|світяться|підсвітк|підсвічуван', q):
        res['backlight'] = True
    if re.search(r'прямокутн|квадратн', q):
        res['shape'] = 'rectangular'
    elif re.search(r'кругл', q):
        res['shape'] = 'round'
    if re.search(r'японськ|miyota|мійота', q):
        res['japanese'] = True
    if re.search(r'хронограф|секундомір|будильник|таймер|календар|дата', q):
        res['chrono'] = True

    # 9. Колір
    for col_root, col_name in [
        ('чорн', 'чорн'), ('сріб', 'сріб'), ('золот', 'золот'),
        ('біл', 'біл'), ('син', 'син'), ('зелен', 'зелен'),
        ('коричн', 'коричн'), ('рожев', 'рожев'), ('сір', 'сір'), ('червон', 'червон')
    ]:
        if re.search(r'\b' + col_root, q):
            res['color'] = col_name
            break

    # 10. Бренд
    for b_alias, b_canon in BRANDS_MAP.items():
        if re.search(r'\b' + re.escape(b_alias) + r'\b', q):
            res['brand'] = b_canon
            break

    # 11. Ключові слова (для вільного пошуку як у Google)
    words = re.findall(r'[а-яіїєґa-z0-9\-]+', q)
    kws = [w for w in words if len(w) >= 3 and w not in STOP_WORDS and not w.isdigit()]
    if kws:
        res['keywords'] = kws

    return res

def build_intent(data: ChatInput) -> dict:
    intent = {'wants_accessory': False, 'keywords': []}
    user_msgs = [m.content for m in data.history[-8:] if m.role in ('user', 'human')]
    user_msgs.append(data.message)

    for msg in user_msgs:
        q = msg.casefold()
        if is_followup_question(q) and msg == data.message and len(user_msgs) > 1:
            # Якщо це питання "порівняй ці моделі" або "опис моделей" — зберігаємо попередній фільтр!
            intent['is_followup'] = True
            continue

        parsed = parse_single_message(msg)

        if 'без обмеження бюджету' in q or 'будь-яка ціна' in q:
            intent.pop('max_price', None)

        # Розумне скидання несумісних фільтрів при зміні теми:
        if parsed.get('watch_type') and parsed.get('watch_type') != intent.get('watch_type'):
            if parsed['watch_type'] in ('mechanical', 'quartz'):
                intent.pop('calls', None)
                intent.pop('gps', None)
                intent.pop('health', None)
            elif parsed['watch_type'] == 'smart':
                intent.pop('skeleton', None)
                intent.pop('autowind', None)

        if parsed.get('audience') and parsed.get('audience') != intent.get('audience'):
            if parsed['audience'] != 'kids':
                intent.pop('gps', None)

        for k, v in parsed.items():
            if k == 'keywords':
                intent['keywords'] = v
            elif v is not None:
                intent[k] = v

    return intent

def build_positive_index(p: dict) -> tuple[str, dict]:
    """
    Будує чистий пошуковий індекс товару, ВИРІЗАЮЧИ всі характеристики зі значенням
    'Немає', 'Ні', '0 м', 'Відсутня', щоб уникнути хибних спрацьовувань.
    """
    a = p.get('attributes') or {}
    positive_attrs = {}
    NEGATIVE_VALS = {'немає', 'ні', '0 м', 'відсутня', 'відсутня індикація', 'без водозахисту', '-'}

    for k, v in a.items():
        v_clean = str(v).strip()
        v_low = v_clean.casefold()
        if v_low in NEGATIVE_VALS or v_low.startswith('немає'):
            continue
        positive_attrs[k] = v_clean

    pos_attrs_str = ' '.join(f"{k}: {v}" for k, v in positive_attrs.items()).casefold()
    return pos_attrs_str, positive_attrs

def select_products(rows: list[dict], intent: dict, limit: int = 3) -> list[dict]:
    def filter_and_score(strict_mode: bool = True):
        scored = []
        wants_acc = intent.get('wants_accessory', False)

        for p in rows:
            if not p.get('available') or not p.get('url') or p.get('price') is None:
                continue

            price = float(p['price'])
            name_l = (p.get('name') or '').casefold()
            vendor_l = (p.get('vendor') or '').casefold()
            cat_l = (p.get('category') or '').casefold()
            desc_l = (p.get('description') or '').casefold()
            sku_l = (p.get('sku') or '').casefold()

            pos_attrs_str, pos_attrs = build_positive_index(p)
            a_raw = p.get('attributes') or {}

            # 1. ЗАЛІЗНИЙ МІНУС-ФІЛЬТР: Відсікаємо зарядки, кабелі, ремінці та коробки
            is_acc = any(w in name_l or w in cat_l for w in NEGATIVE_ACCESSORY_WORDS)
            if not wants_acc and is_acc:
                continue
            if wants_acc:
                if not is_acc:
                    continue
                acc_t = intent.get('accessory_type')
                if acc_t and acc_t not in name_l and acc_t not in cat_l:
                    continue

            # 2. Фільтр бюджету
            if intent.get('max_price') and price > intent['max_price']:
                continue
            if intent.get('min_price') and price < intent['min_price']:
                continue

            # 3. Цільова аудиторія (Чоловічі / Жіночі / Дитячі)
            aud = intent.get('audience')
            type_field = (
                str(a_raw.get('Тип', '')) + ' ' +
                str(a_raw.get('Стать', '')) + ' ' +
                str(a_raw.get('Для чоловіків', '')) + ' ' +
                str(a_raw.get('Для жінок', '')) + ' ' +
                str(a_raw.get('Хлопчики', '')) + ' ' +
                str(a_raw.get('Дівчата', '')) + ' ' +
                cat_l + ' ' + name_l
            ).casefold()

            if aud == 'kids':
                if not any(w in type_field or w in desc_l for w in ['дитяч', 'дітей', 'хлопчик', 'дівчат', 'baby', 'q100']):
                    continue
            elif aud == 'women':
                if 'дитяч' in type_field:
                    continue
                if not any(w in type_field for w in ['жіноч', ' для жінок', 'унісекс']):
                    continue
            elif aud == 'men':
                if 'дитяч' in type_field:
                    continue
                if 'жіноч' in type_field and 'чоловіч' not in type_field:
                    continue

            # 4. Тип механізму / категорії (Смарт / Механіка / Кварц)
            w_type = intent.get('watch_type')
            mech_field = (
                str(a_raw.get('Механізм', '')) + ' ' +
                str(a_raw.get('Тип механізму', '')) + ' ' +
                str(a_raw.get('Тип пристрою', '')) + ' ' +
                cat_l + ' ' + name_l
            ).casefold()

            is_smart_item = any(w in mech_field or w in pos_attrs_str for w in ['смарт', 'smart', 'розумн', 'фітнес', 'пульсометр', 'bluetooth', 'android'])
            is_mech_item = 'механ' in mech_field or 'автозавод' in mech_field

            if w_type == 'smart' and not is_smart_item:
                continue
            elif w_type == 'mechanical' and (not is_mech_item or is_smart_item):
                continue
            elif w_type == 'quartz' and (is_smart_item or is_mech_item):
                continue

            # 5. Жорсткі функціональні вимоги (у strict_mode)
            if strict_mode:
                if intent.get('brand'):
                    b = intent['brand']
                    if b not in vendor_l and b not in name_l:
                        continue

                if intent.get('calls'):
                    call_ok = any(w in pos_attrs_str or w in desc_l for w in [
                        'дзвін', 'виклик', 'розмов', 'підтримка sim-карти: є',
                        'можливість розмовляти: так', 'окрема сім', 'сім-карт'
                    ])
                    if not call_ok:
                        continue

                if intent.get('gps'):
                    gps_ok = any(w in pos_attrs_str or w in name_l or w in desc_l for w in [
                        'gps', 'місцерозташування', 'відстежування', 'кнопка sos'
                    ])
                    if not gps_ok:
                        continue

                if intent.get('waterproof') == 'swimming':
                    wp_val = str(a_raw.get('Водостійкість', '')).casefold()
                    if not any(w in wp_val or w in desc_l for w in ['плавання', '5 атм', '10 атм', '50 м', '100 м', '5 atm', '10 atm']):
                        continue

                if intent.get('strap'):
                    st_val = (str(a_raw.get('Матеріал ремінця', '')) + ' ' + name_l).casefold()
                    if intent['strap'] == 'leather' and 'шкір' not in st_val:
                        continue
                    if intent['strap'] == 'steel' and not any(w in st_val for w in ['стал', 'метал', 'титан']):
                        continue
                    if intent['strap'] == 'rubber' and not any(w in st_val for w in ['каучук', 'силікон', 'полімер', 'rubber']):
                        continue

            # =========================================================
            # НАРАХУВАННЯ БАЛІВ РЕЛЕВАНТНОСТІ (ВАГОВИЙ ІНДЕКС ЯК У GOOGLE)
            # =========================================================
            score = 0

            # Бренд (100 балів)
            if intent.get('brand') and (intent['brand'] in vendor_l or intent['brand'] in name_l):
                score += 100

            # Стиль (40 балів)
            st = intent.get('style')
            if st == 'tactical' and any(w in pos_attrs_str or w in desc_l or w in name_l for w in ['тактич', 'військов', 'мілітарі', 'amst', 'north edge', 's-shock']):
                score += 45
            elif st == 'sport' and any(w in pos_attrs_str or w in cat_l or w in name_l for w in ['спорт', 'sport', 's-shock', 'skmei', 'секундомір']):
                score += 40
            elif st == 'classic' and any(w in pos_attrs_str or w in cat_l or w in desc_l for w in ['класич', 'елегант']):
                score += 40
            elif st == 'patriotic' and any(w in pos_attrs_str or w in cat_l or w in name_l or w in desc_l for w in ['патріот', 'тризуб', 'україн']):
                score += 50

            # Спеціальні характеристики (по 35 балів)
            if intent.get('calls') and any(w in pos_attrs_str or w in desc_l for w in ['дзвін', 'розмов', 'sim', 'сім']):
                score += 45
            if intent.get('gps') and any(w in pos_attrs_str or w in name_l for w in ['gps', 'sos']):
                score += 45
            if intent.get('skeleton') and any(w in pos_attrs_str or w in desc_l or w in cat_l for w in ['скелетон', 'прозора', 'видно роботу']):
                score += 40
            if intent.get('autowind') and 'автозавод' in pos_attrs_str:
                score += 35
            if intent.get('health') and any(w in pos_attrs_str or w in desc_l for w in ['пульс', 'крокомір', 'сон', 'фітнес']):
                score += 35
            if intent.get('roman') and 'римські' in pos_attrs_str:
                score += 35
            if intent.get('arabic') and 'арабські' in pos_attrs_str:
                score += 35
            if intent.get('backlight') and any(w in pos_attrs_str for w in ['фосфор', 'люмінесцент', 'підсвічування']):
                score += 35
            if intent.get('shape') == 'rectangular' and 'прямокутн' in pos_attrs_str:
                score += 35
            elif intent.get('shape') == 'round' and 'кругл' in pos_attrs_str:
                score += 20
            if intent.get('japanese') and any(w in pos_attrs_str or w in desc_l for w in ['япон', 'miyota']):
                score += 40
            if intent.get('chrono') and any(w in pos_attrs_str for w in ['хронограф', 'секундомір', 'будильник', 'дата']):
                score += 25
            if intent.get('color') and (intent['color'] in pos_attrs_str or intent['color'] in name_l):
                score += 30

            # Точний збіг за ключовими словами запиту
            for kw in intent.get('keywords', []):
                if kw == sku_l:
                    score += 120
                elif kw in name_l or kw in vendor_l:
                    score += 30
                elif kw in pos_attrs_str or kw in cat_l:
                    score += 15
                elif kw in desc_l:
                    score += 5

            # Бонус за повноту заповнених характеристик
            score += min(len(pos_attrs), 15)

            scored.append((score, p))

        return scored

    # Спочатку шукаємо у суворому режимі (Strict)
    candidates = filter_and_score(strict_mode=True)
    # Якщо за всіма жорсткими умовами 0 збігів — послаблюємо другорядні умови, зберігаючи категорію і стать!
    if not candidates:
        candidates = filter_and_score(strict_mode=False)

    candidates.sort(key=lambda t: (-t[0], -float(t[1]['price'])))

    # Диверсифікація брендів (щоб не показувати 3 однакові годинники одного бренду, якщо не просили конкретний бренд)
    selected = []
    seen_vendors = set()
    seen_names = set()

    for score, item in candidates:
        base_name = re.sub(r'\s+(ii|iii|new|\d+)$', '', item['name'].casefold()).strip()
        if base_name in seen_names:
            continue

        v = (item.get('vendor') or item['name'].split()[0]).casefold()
        if not intent.get('brand') and v in seen_vendors and len(candidates) > limit * 2:
            continue

        selected.append(item)
        seen_vendors.add(v)
        seen_names.add(base_name)
        if len(selected) >= limit:
            break

    if len(selected) < limit:
        for score, item in candidates:
            if item not in selected:
                selected.append(item)
            if len(selected) >= limit:
                break

    return selected

# =====================================================================
# ПРОДАВЕЦЬ-КОНСУЛЬТАНТ GEMINI (З ПАМ'ЯТТЮ ДІАЛОГУ ТА АРГУМЕНТАЦІЄЮ)
# =====================================================================

def ask_gemini_consultant(data: ChatInput, products: list[dict], intent: dict) -> str:
    key = os.getenv('GEMINI_API_KEY')
    if not key:
        return ''

    facts = []
    for idx, p in enumerate(products, 1):
        _, pos_attrs = build_positive_index(p)
        # Вибираємо найцінніші характеристики для аргументації продавця
        priority_keys = (
            'Механізм', 'Тип механізму', 'Характеристики механізму',
            'Водостійкість', 'Матеріал корпусу', 'Матеріал ремінця',
            'Скло', 'Функції', 'Особливості', 'Підтримка Sim-карти',
            'Можливість розмовляти', 'Пульсометр', 'Діаметр', 'Розміри, мм',
            ' Колір корпусу', 'Основний колір циферблата', 'Гарантія'
        )
        picked_specs = {k: v for k, v in pos_attrs.items() if k in priority_keys}
        if len(picked_specs) < 5:
            for k, v in list(pos_attrs.items())[:8]:
                picked_specs.setdefault(k, v)

        facts.append({
            'номер': idx,
            'назва': p['name'],
            'бренд': p.get('vendor') or 'ZORRO',
            'ціна_грн': int(p['price']),
            'категорія': p.get('category', ''),
            'характеристики': picked_specs,
            'короткий_опис': (p.get('description') or '')[:220]
        })

    history_text = ''
    if data.history:
        recent = data.history[-6:]
        history_lines = []
        for m in recent:
            role_label = 'Покупець' if m.role in ('user', 'human') else 'Консультант'
            clean_c = clean_html_tags(m.content)[:350]
            history_lines.append(f"{role_label}: {clean_c}")
        history_text = "\n".join(history_lines)

    system_prompt = (
        "Ти — найкращий експерт і продавець-консультант інтернет-магазину годинників ZORRO (zorro.com.ua).\n"
        "Твоє завдання — професійно проконсультувати клієнта та допомогти йому обрати і купити годинник.\n\n"
        "ПРАВИЛА РОБОТИ:\n"
        "1. Спілкуйся живою, привітною українською мовою як справжній фахівець годинникової справи.\n"
        "2. Опирайся ВИКЛЮЧНО на передані 'Перевірені товари та їхні характеристики'. Нічого не вигадуй.\n"
        "3. Коротко і переконливо розкажи про кожну з підібраних моделей (по 1 реченню на модель: для кого підійде, який механізм, водостійкість, скло чи ключова фішка).\n"
        "4. Якщо покупець просить 'Опис моделей' або 'Порівняй моделі' — зроби чітке експертне порівняння цих 3-х варіантів між собою (у чому різниця в захисті, механізмі, матеріалах) і порадь, який обрати.\n"
        "5. Не вставляй URL-посилання (графічні картки з кнопкою 'Купити' автоматично показуються прямо під твоїм текстом).\n"
        "6. Наприкінці відповіді обов'язково постав одне влучне питання для закриття угоди (наприклад, яка з моделей найбільше сподобалась візуально, або запропонуй натиснути 'Купити' чи 'Замовити дзвінок менеджера')."
    )

    user_prompt = (
        f"Історія розмови:\n{history_text or 'Початок діалогу'}\n\n"
        f"Сторінка, яку зараз переглядає клієнт: {data.current_product or 'Каталог магазину'}\n"
        f"Поточне повідомлення клієнта: {data.message}\n\n"
        f"Перевірені товари з бази ZORRO (картки яких виведені під твоєю відповіддю):\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=2)}"
    )

    payload = {
        'systemInstruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'role': 'user', 'parts': [{'text': user_prompt}]}],
        'generationConfig': {'temperature': 0.25, 'maxOutputTokens': 600}
    }

    for model_name in CANDIDATE_MODELS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
            r = requests.post(url, json=payload, timeout=14)
            if r.status_code == 200:
                parts = r.json()['candidates'][0]['content']['parts']
                text = ''.join(t.get('text', '') for t in parts).strip()
                if text:
                    return text
        except Exception as e:
            LOG.warning(f"Модель {model_name} не відповіла: {e}")
            continue

    # Розумна резервна консультація на базі реальних характеристик (якщо API Gemini перевантажене)
    lines = ["Я підібрав для вас найкращі варіанти з нашого каталогу за вашими критеріями:"]
    for f in facts:
        specs = f['характеристики']
        spec_summary = ", ".join(f"{k.lower()}: {v}" for k, v in list(specs.items())[:3])
        lines.append(f"• {f['назва']} — надійна модель бренду {f['бренд']} ({spec_summary}).")
    lines.append("\nЯка з цих моделей вам найбільше імпонує за дизайном та характеристиками?")
    return "\n".join(lines)

def format_ai_text(raw_text: str) -> str:
    escaped = html.escape(raw_text)
    escaped = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped)
    return escaped.replace('\n', '<br>')

def product_html(p: dict) -> str:
    name = html.escape(p['name'])
    raw_url = (p.get('url') or '').strip()
    if not raw_url or raw_url.rstrip('/') == 'https://zorro.com.ua':
        raw_url = f"https://zorro.com.ua/katalog/search/?q={urllib.parse.quote_plus(p['name'])}"
    url = html.escape(raw_url, quote=True)
    img = html.escape(p.get('image') or 'https://zorro.com.ua/content/images/no-image.png', quote=True)
    price_val = int(p['price']) if p.get('price') else 0
    vendor = html.escape(p.get('vendor') or 'ZORRO')

    _, pos_attrs = build_positive_index(p)
    display_keys = ('Механізм', 'Водостійкість', 'Матеріал ремінця', 'Матеріал корпусу', 'Скло', 'Функції', 'Діаметр')
    specs_list = [f"{k}: {pos_attrs[k]}" for k in display_keys if k in pos_attrs]
    if len(specs_list) < 3:
        for k, v in pos_attrs.items():
            if k not in ('Гарантія', 'Фабрика зборки', 'Інструкція') and f"{k}: {v}" not in specs_list:
                specs_list.append(f"{k}: {v}")
            if len(specs_list) >= 3:
                break

    short_specs = html.escape(' • '.join(specs_list[:3])) if specs_list else html.escape((p.get('description') or '')[:95] + '...')

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
            <a href="{url}" target="_self" style="background:#00a046;
color:#ffffff; padding:5px 12px; border-radius:6px; font-size:12px; font-weight:600; text-decoration:none;">Купити</a>
          </div>
        </div>
      </div>
      <div style="font-size:11.5px; color:#555; margin-top:8px; line-height:1.35; border-top:1px dashed #e5e5e5; padding-top:6px;">{short_specs}</div>
      <div style="display:flex; gap:12px; margin-top:8px;">
        <span class="ai-card-action" data-action="similar" data-name="{html.escape(p['name'], quote=True)}" style="cursor:pointer;color:#00a046;font-size:11px;">🔍 Схожі товари</span>
        <span class="ai-card-action" data-action="desc" data-name="{html.escape(p['name'], quote=True)}" style="cursor:pointer;font-size:11px;">📄 Опис товару</span>
      </div>
    </div>
    """

def send_telegram_notification(phone: str, message: str = "") -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"📞 Заявка ZORRO: {phone}\n{message}"},
            timeout=8
        )
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except Exception:
        LOG.exception("Telegram callback failed")
        return False

@app.get("/")
def root():
    return {"status": "ok", "version": "v2-repaired"}

@app.get("/api/catalog-status")
def catalog_status():
    try:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), COUNT(*) FILTER (WHERE available), MAX(synced_at) FROM zorro_products_v2"
                )
                count, available, last_sync = cur.fetchone()
            return {
                "status": "ok",
                "products": count,
                "available": available,
                "last_sync": last_sync.isoformat() if last_sync else None
            }
        finally:
            conn.close()
    except Exception:
        LOG.exception("Catalog status error")
        return {"status": "loading_or_error", "message": "Перевірте Render Logs"}

@app.post("/api/chat")
def chat(data: ChatInput):
    from conversation_v4 import handle_chat
    try:
        return handle_chat(data, db, product_html, format_ai_text)
    except Exception:
        LOG.exception("v4 chat failed")
        return {"reply": "Виникла помилка консультації. Будь ласка, спробуйте ще раз.", "state": data.state}

@app.post("/api/callback")
def callback(data: CallbackInput):
    if not data.phone.strip():
        return {"status": "error", "reply": "Введіть номер телефону."}
    if send_telegram_notification(data.phone, data.message):
        return {"status": "success", "reply": "Дякуємо! Ваш номер передано менеджеру."}
    return {"status": "error", "reply": "Не вдалося відправити заявку. Зверніться до магазину напряму."}
