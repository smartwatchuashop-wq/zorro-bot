"""Validated, compact conversational preference memory for Zorro AI.

Gemini proposes preference changes, but this module validates schema, bounds
all user-supplied content, and never treats a missing catalog field as proof.
No network calls and no writes to the product catalog.
"""
from __future__ import annotations

import re
from typing import Any

ALLOWED = frozenset({'must', 'prefer', 'avoid', 'indifferent', 'remove'})
MAX_PREFERENCES = 12
MAX_TEXT = 72


def _clean(value: Any, size: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ''
    value = re.sub(r'<[^>]+>|[\x00-\x1f]', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()[:size]


def _key(value: Any) -> str:
    value = _clean(value).casefold()
    return re.sub(r'[^\wа-яіїєґ]+', ' ', value).strip()


def clean_profile(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    goal = _clean(raw.get('goal'), 170)
    choices = []
    seen = set()
    for entry in raw.get('preferences', []) if isinstance(raw.get('preferences'), list) else []:
        if not isinstance(entry, dict):
            continue
        term, status = _clean(entry.get('feature')), entry.get('status')
        if not term or status not in ALLOWED - {'remove'} or _key(term) in seen:
            continue
        choices.append({'feature': term, 'status': status})
        seen.add(_key(term))
        if len(choices) == MAX_PREFERENCES:
            break
    rejected = raw.get('rejected_ids', [])
    rejected = rejected if isinstance(rejected, list) else []
    rejected = list(dict.fromkeys(str(x)[:64] for x in rejected[:20]
                                  if isinstance(x, (str, int)) and re.fullmatch(r'[\w-]{1,64}', str(x))))
    return {'goal': goal, 'preferences': choices, 'rejected_ids': rejected}


def merge_profile(previous: Any, update: Any, new_topic: bool = False,
                  previously_shown: list[str] | None = None) -> dict:
    profile = clean_profile({} if new_topic else previous)
    if not isinstance(update, dict):
        return profile
    if update.get('reset') is True:
        profile = clean_profile({})
    goal = _clean(update.get('goal'), 170)
    if goal:
        profile['goal'] = goal
    existing = {_key(x['feature']): x for x in profile['preferences']}
    changes = update.get('changes', [])
    if isinstance(changes, list):
        for item in changes[:12]:
            if not isinstance(item, dict):
                continue
            term, status = _clean(item.get('feature')), item.get('status')
            if not term or status not in ALLOWED:
                continue
            key = _key(term)
            if status == 'remove':
                existing.pop(key, None)
            else:
                existing[key] = {'feature': term, 'status': status}
    profile['preferences'] = list(existing.values())[-MAX_PREFERENCES:]
    # Reject product IDs only if they were actually shown; Gemini does not
    # have permission to invent an arbitrary excluded product ID.
    allowed = set(str(x) for x in (previously_shown or []))
    suggested = update.get('reject_ids', [])
    if isinstance(suggested, list):
        for x in suggested[:10]:
            if str(x) in allowed and str(x) not in profile['rejected_ids']:
                profile['rejected_ids'].append(str(x))
    profile['rejected_ids'] = profile['rejected_ids'][-20:]
    return profile


def retrieval_context(profile: Any) -> str:
    """Soft search context, NOT a replacement for trusted SQL predicates."""
    profile = clean_profile(profile)
    bits = [profile['goal']] if profile['goal'] else []
    bits += [x['feature'] for x in profile['preferences'] if x['status'] in {'must', 'prefer'}]
    return ' '.join(bits)[:260]


def _feature_status(attributes: Any, phrase: str) -> str:
    """Positive/negative/unknown evidence for an arbitrary requested feature.

    Feature matching is deliberately conservative: one must see the feature
    named in the attribute name or in an explicit feature-bearing value.
    """
    if not isinstance(attributes, dict):
        return 'unknown'
    k = _key(phrase)
    roots = [token[:5] for token in k.split() if len(token) >= 3]
    if not roots:
        return 'unknown'
    for field, v in attributes.items():
        label, value = _key(field), _key(str(v))
        if not any(root in label or root in value for root in roots):
            continue
        if re.search(r'\b(ні|немає|відсутн|без|no|false|не підтрим|не має)\b', value):
            return 'negative'
        if value in ('', '-', 'невідомо', 'unknown'):
            continue
        return 'positive'
    return 'unknown'


def dislikes_product(product: dict, profile: Any) -> bool:
    """Only reject when the forbidden feature is positively evidenced.

    Unknown is not proof of absence; avoid claiming that it is.
    """
    profile = clean_profile(profile)
    attributes = product.get('attributes') or {}
    return any(_feature_status(attributes, p['feature']) == 'positive'
               for p in profile['preferences'] if p['status'] == 'avoid')


def verified_absence(product: dict, feature: str) -> bool:
    return _feature_status(product.get('attributes') or {}, feature) == 'negative'
