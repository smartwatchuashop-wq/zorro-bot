"""Real pgvector + FTS + trigram hybrid retrieval for Zorro PostgreSQL.

Read-only at request time. Indexing and schema setup are separate explicit steps.
All branches query the eligible catalog, not a pre-selected first-80 pool.
"""
import logging
import math
import os
import re
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor
import requests

LOG = logging.getLogger('zorro.hybrid')
EMBED_MODEL = os.getenv('EMBEDDING_MODEL', 'gemini-embedding-2')
EMBED_DIM = 768
DB_URL = os.getenv('DATABASE_URL', '')
GEMINI_KEY = os.getenv('GEMINI_API_KEY', '')
CATEGORIES = ('Чоловічі годинники', 'Жіночі годинники', 'Дитячі годинники',
              'Смарт годинники', 'Спортивні годинники', 'Унісекс годинники')


def document_text(product: dict) -> str:
    """Same source/format for indexing and hash update checks."""
    attrs = product.get('attributes') or {}
    if not isinstance(attrs, dict):
        attrs = {}
    info = ['Назва: ' + str(product.get('name') or ''),
            'Бренд: ' + str(product.get('vendor') or ''),
            'Категорія: ' + str(product.get('category') or ''),
            'Артикул: ' + str(product.get('sku') or '')]
    for k, v in sorted(attrs.items()):
        if v not in (None, ''):
            info.append(str(k) + ': ' + str(v))
    description = re.sub(r'<[^>]+>', ' ', str(product.get('description') or ''))
    info.append('Опис: ' + re.sub(r'\s+', ' ', description)[:900])
    return (' | '.join(info))[:3500]


def doc_embedding_text(product: dict) -> str:
    return 'title: ' + str(product.get('name') or '')[:140] + ' | text: ' + document_text(product)


def embed_text(value: str, is_query: bool = True) -> list[float]:
    if not GEMINI_KEY:
        raise RuntimeError('GEMINI_API_KEY required')
    prompt = ('task: search result | query: ' + value) if is_query else value
    response = requests.post(
        'https://generativelanguage.googleapis.com/v1beta/models/' + EMBED_MODEL + ':embedContent',
        headers={'x-goog-api-key': GEMINI_KEY},
        json={'model': 'models/' + EMBED_MODEL,
              'content': {'parts': [{'text': prompt[:4500]}]},
              'outputDimensionality': EMBED_DIM}, timeout=25)
    response.raise_for_status()
    obj = response.json()
    embedding = obj.get('embedding') or (obj.get('embeddings') or [{}])[0]
    vector = embedding.get('values', [])
    if len(vector) != EMBED_DIM or not all(isinstance(v, (float,int)) and math.isfinite(v) for v in vector):
        raise RuntimeError('Bad embedding response or dimension mismatch')
    return [float(v) for v in vector]


def vector_literal(vector: list[float]) -> str:
    if len(vector) != EMBED_DIM or not all(isinstance(v, (float,int)) and math.isfinite(v) for v in vector):
        raise ValueError('Invalid vector')
    # pgvector accepts a single properly validated literal as a bound parameter.
    return '[' + ','.join(format(float(n), '.9g') for n in vector) + ']'


def connect_ro():
    if not DB_URL:
        raise RuntimeError('DATABASE_URL missing')
    conn = psycopg2.connect(DB_URL, connect_timeout=8)
    conn.set_session(readonly=True, autocommit=True)
    return conn


def index_status() -> dict:
    conn = connect_ro()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='zorro_product_search')")
            exists = bool(cur.fetchone()[0])
            if not exists:
                return {'ready': False, 'indexed': 0, 'eligible': 0, 'model': EMBED_MODEL}
            cur.execute("SELECT COUNT(*) FROM zorro_products_v2 WHERE available=TRUE AND price>0 AND category=ANY(%s)", (list(CATEGORIES),))
            eligible = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM zorro_product_search s JOIN zorro_products_v2 p USING(site_id) WHERE p.available=TRUE AND p.price>0 AND p.category=ANY(%s) AND s.embedding_model=%s", (list(CATEGORIES), EMBED_MODEL))
            indexed = int(cur.fetchone()[0])
            return {'ready': indexed > 0, 'indexed': indexed, 'eligible': eligible, 'model': EMBED_MODEL,
                    'complete': indexed >= eligible and eligible > 0}
    finally:
        conn.close()


def _sql_constraints(filters: dict):
    conditions = ['p.available=TRUE', 'p.price>0', "p.url IS NOT NULL", "p.url<>''", 'p.category=ANY(%s)']
    values: list[Any] = [list(CATEGORIES)]
    if filters.get('category'):
        conditions.append('p.category=%s'); values.append(filters['category'])
    if filters.get('max_price') is not None:
        conditions.append('p.price<=%s'); values.append(filters['max_price'])
    if filters.get('min_price') is not None:
        conditions.append('p.price>=%s'); values.append(filters['min_price'])
    if filters.get('brand'):
        conditions.append('LOWER(p.vendor)=LOWER(%s)'); values.append(filters['brand'])
    if filters.get('sku'):
        conditions.append('LOWER(p.sku)=LOWER(%s)'); values.append(filters['sku'])
    return ' AND '.join(conditions), values


def ranked_ids(query: str, filters: dict, semantic: bool = True) -> dict[str, list[str]]:
    """Three independent ranked lists. The embedding searches all indexed stock.

    Category/price SQL predicates apply before scoring; other mandatory conditions
    (calling/strap/SIM) are validated later from the original catalog.
    """
    query = re.sub(r'\s+', ' ', query).strip()[:350]
    if not query:
        return {'semantic': [], 'lexical': [], 'trigram': []}
    where, args = _sql_constraints(filters)
    out = {'semantic': [], 'lexical': [], 'trigram': []}
    conn = connect_ro()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.zorro_product_search')")
            if cur.fetchone()[0] is None:
                raise RuntimeError('Run schema.sql first: zorro_product_search missing')
            # Lexical full-text search. Simple config works with mixed Cyrillic/Latin model identifiers.
            cur.execute(
                'SELECT s.site_id FROM zorro_product_search s JOIN zorro_products_v2 p USING(site_id) '
                'WHERE ' + where + ' AND s.embedding_model=%s '
                "AND to_tsvector('simple',s.document) @@ plainto_tsquery('simple',%s) "
                "ORDER BY ts_rank_cd(to_tsvector('simple',s.document), plainto_tsquery('simple',%s)) DESC, s.site_id LIMIT 80",
                args + [EMBED_MODEL, query, query])
            out['lexical'] = [r[0] for r in cur.fetchall()]
            # Name + SKU spelling errors. Trigram similarity is useful for short searches.
            cur.execute(
                'SELECT s.site_id FROM zorro_product_search s JOIN zorro_products_v2 p USING(site_id) '
                'WHERE ' + where + ' AND s.embedding_model=%s '
                "AND (LOWER(p.name) %% LOWER(%s) OR LOWER(COALESCE(p.vendor,'')) %% LOWER(%s) OR LOWER(COALESCE(p.sku,'')) %% LOWER(%s)) "
                "ORDER BY GREATEST(similarity(LOWER(p.name),LOWER(%s)),similarity(LOWER(COALESCE(p.vendor,'')),LOWER(%s)),similarity(LOWER(COALESCE(p.sku,'')),LOWER(%s))) DESC LIMIT 60",
                args + [EMBED_MODEL, query, query, query, query, query, query])
            out['trigram'] = [r[0] for r in cur.fetchall()]
            if semantic:
                vector = vector_literal(embed_text(query, True))
                cur.execute(
                    'SELECT s.site_id FROM zorro_product_search s JOIN zorro_products_v2 p USING(site_id) '
                    'WHERE ' + where + ' AND s.embedding_model=%s '
                    'ORDER BY s.embedding <=> %s::vector LIMIT 120',
                    args + [EMBED_MODEL, vector])
                out['semantic'] = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    return out


def reciprocal_rank_fusion(branches: dict[str, list[str]], limit: int = 90) -> list[str]:
    weights = {'semantic': 1.25, 'lexical': 1.15, 'trigram': .8, 'fallback': .4}
    scores: dict[str, float] = {}
    for key, ids in branches.items():
        seen = set()
        for position, identifier in enumerate(ids, 1):
            identifier = str(identifier)
            if identifier in seen:
                continue
            seen.add(identifier)
            scores[identifier] = scores.get(identifier, 0.) + weights.get(key, 1.) / (60 + position)
    return sorted(scores, key=lambda k: (-scores[k], k))[:limit]
