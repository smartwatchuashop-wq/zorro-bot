"""Conservative attribute evidence and deterministic constraint checks.

Unknown never means absent. No language model is allowed to bypass hard filters.
Only facts from actual PostgreSQL product rows are considered evidence.
"""
from __future__ import annotations

import re
from typing import Any
from buyer_profile import clean_profile

NEG = re.compile(r'(^|\W)(ні|немає|відсутн\w*|не підтрим\w*|без|no|false|ніт|none|0)(\W|$)', re.I)
UNKNOWN = {'', '-', '—', 'н/д', 'невідомо', 'unknown', 'null'}


def normalize(raw: Any) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', str(raw or '')).casefold()).strip()


def tokens(raw: Any) -> list[str]:
    return [t for t in re.findall(r'[\wа-яіїєґ]+', normalize(raw)) if len(t) >= 3]


def matches(label: str, feature: str) -> bool:
    ft = tokens(feature)
    if not ft:
        return False
    lt = tokens(label)
    # avoid 'компас' matching 'компактний': keep the first 6 chars
    # for long words; short terms match whole words.
    for word in ft:
        length = min(len(word), 7)
        if not any((v == word if length < 5 else v[:length] == word[:length]) for v in lt):
            return False
    return True


def status(product: dict, feature: str) -> tuple[str, str, str]:
    """Return (present|absent|unknown, attribute field, verbatim value)."""
    attrs = product.get('attributes') or {}
    if not isinstance(attrs, dict):
        return ('unknown', '', '')
    for key, raw in attrs.items():
        value = normalize(raw)
        if value in UNKNOWN:
            continue
        name_matches = matches(str(key), feature)
        value_matches = matches(str(raw), feature)
        if name_matches:
            return ('absent' if NEG.search(value) else 'present', str(key), str(raw)[:170])
        if value_matches:
            # Note: 'без компаса' explicitly proves absent; other listings prove present.
            return ('absent' if re.search(r'\bбез\b[^,;.]{0,25}' + re.escape(tokens(feature)[0][:6]), value) else 'present', str(key), str(raw)[:170])
    return ('unknown', '', '')


def eligible(product: dict, profile: dict) -> tuple[bool, list[str]]:
    """Conservative: reject unknown when an explicit must/avoid feature is strict.

    'indifferent' is ignored, and 'prefer' influences ranking, not eligibility.
    """
    errors = []
    for wish in clean_profile(profile)['preferences']:
        desired = wish['status']
        if desired not in ('must', 'avoid'):
            continue
        observed, _, _ = status(product, wish['feature'])
        if desired == 'must' and observed != 'present':
            errors.append('not verified present: ' + wish['feature'])
        elif desired == 'avoid' and observed != 'absent':
            errors.append('not verified absent: ' + wish['feature'])
    return not errors, errors


def rank_candidates(products: list[dict], profile: dict, query: str, max_count: int = 24) -> list[dict]:
    """Keep original hybrid order as primary signal, add modest preference rewards.

    Do not use imaginary sales or margin statistics.
    """
    wishes = clean_profile(profile)['preferences']
    scored = []
    for position, product in enumerate(products):
        ok, _ = eligible(product, profile)
        if not ok:
            continue
        score = 10 / (10 + position)
        for pref in wishes:
            if pref['status'] == 'prefer':
                observed, _, _ = status(product, pref['feature'])
                if observed == 'present':
                    score += .16
        scored.append((score, position, product))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [p for _, _, p in scored[:max_count]]


def full_facts(product: dict) -> dict:
    attrs = product.get('attributes') or {}
    if not isinstance(attrs, dict):
        attrs = {}
    return {'id': str(product.get('site_id') or ''),
            'name': str(product.get('name') or '')[:150],
            'brand': str(product.get('vendor') or '')[:100],
            'price_uah': float(product.get('price') or 0),
            'attributes': {str(k)[:100]: str(v)[:190] for k, v in list(attrs.items())[:40]
                           if v not in (None, '')}}


def choose_ids(proposed: Any, candidates: list[dict], max_items: int = 3) -> list[str]:
    allowed = {str(p.get('site_id')) for p in candidates}
    result = []
    for value in proposed if isinstance(proposed, list) else []:
        sid = str(value)
        if sid in allowed and sid not in result:
            result.append(sid)
        if len(result) >= max_items:
            break
    return result
