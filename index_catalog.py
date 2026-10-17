"""Explicit one-off embedding indexer. Never run automatically on a web request/deploy.

Usage: python index_catalog.py --limit 20      # small test batch
       python index_catalog.py                  # full initial/changed-only pass
       python index_catalog.py --force           # re-embed everything (costly)
Requires schema.sql and DATABASE_URL with INSERT permission + GEMINI_API_KEY.
"""
import argparse
import hashlib
import json
import logging
import os
import time

import psycopg2
from psycopg2.extras import RealDictCursor
import requests

from hybrid_search import (EMBED_MODEL, EMBED_DIM, CATEGORIES, document_text,
                           doc_embedding_text, vector_literal, GEMINI_KEY, DB_URL)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('zorro.indexer')


def embed_batch(documents):
    if not GEMINI_KEY:
        raise RuntimeError('Missing GEMINI_API_KEY')
    requests_payload = [
        {'model': 'models/' + EMBED_MODEL,
         'content': {'parts': [{'text': doc_embedding_text(p)}]},
         'outputDimensionality': EMBED_DIM}
        for p in documents
    ]
    for attempt in range(5):
        try:
            res = requests.post(
                'https://generativelanguage.googleapis.com/v1beta/models/' + EMBED_MODEL + ':batchEmbedContents',
                headers={'x-goog-api-key': GEMINI_KEY}, json={'requests': requests_payload}, timeout=90)
            if res.status_code in (429, 500, 502, 503) and attempt < 4:
                time.sleep(min(30, 2 ** attempt * 3))
                continue
            res.raise_for_status()
            embeddings = res.json().get('embeddings', [])
            if len(embeddings) != len(documents):
                raise RuntimeError('Unexpected Google embedding batch response length')
            vectors = [e.get('values', []) for e in embeddings]
            for v in vectors:
                vector_literal(v)  # Validate length and numeric integrity.
            return vectors
        except requests.RequestException:
            if attempt >= 4:
                raise
            time.sleep(min(30, 2 ** attempt * 3))
    raise RuntimeError('Embedding batch failed')


def run(limit=0, batch_size=8, force=False, pause=.6):
    if not DB_URL:
        raise RuntimeError('Missing DATABASE_URL')
    if batch_size < 1 or batch_size > 32:
        raise ValueError('batch_size must be 1..32')
    conn = psycopg2.connect(DB_URL, connect_timeout=15)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT p.site_id,p.sku,p.name,p.vendor,p.category,p.description,p.attributes '
                        'FROM zorro_products_v2 p WHERE p.category=ANY(%s) '
                        'ORDER BY p.site_id', (list(CATEGORIES),))
            products = [dict(p) for p in cur.fetchall()]
            cur.execute('SELECT site_id,content_hash,embedding_model FROM zorro_product_search')
            old = {str(x['site_id']): x for x in cur.fetchall()}
        conn.commit()
        changed = []
        for p in products:
            document = document_text(p)
            checksum = hashlib.sha256((EMBED_MODEL + '\n' + str(p['name']) + '\n' + document).encode()).hexdigest()
            previous = old.get(str(p['site_id']))
            if not force and previous and previous['content_hash'] == checksum and previous['embedding_model'] == EMBED_MODEL:
                continue
            changed.append((p, document, checksum))
        if limit:
            changed = changed[:limit]
        LOG.info('Catalog=%s, existing=%s, embedding changes in this run=%s', len(products),len(old),len(changed))
        written = 0
        for offset in range(0, len(changed), batch_size):
            group = changed[offset:offset+batch_size]
            vectors = embed_batch([x[0] for x in group])
            with conn.cursor() as cur:
                for (p, doc, checksum), vector in zip(group, vectors):
                    cur.execute(
                        'INSERT INTO zorro_product_search(site_id,document,content_hash,embedding_model,embedding) '
                        'VALUES (%s,%s,%s,%s,%s::vector) '
                        'ON CONFLICT(site_id) DO UPDATE SET document=EXCLUDED.document, '
                        'content_hash=EXCLUDED.content_hash,embedding_model=EXCLUDED.embedding_model, '
                        'embedding=EXCLUDED.embedding,updated_at=now()',
                        (str(p['site_id']), doc, checksum, EMBED_MODEL, vector_literal(vector)))
            conn.commit()  # Resume works after temporary quota/network failure.
            written += len(group)
            LOG.info('Indexed %s/%s', written, len(changed))
            if pause:
                time.sleep(pause)
        LOG.info('FINISHED: %s new/updated embeddings; no catalog products modified', written)
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--pause', type=float, default=.6)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    run(args.limit, args.batch_size, args.force, args.pause)
