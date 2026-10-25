"""Zorro v7 consultation orchestrator.

Gemini function-calls a *plan* with multiple possible actions. Python validates
references, filters, stock, constraints and returns only verified product IDs.
The widget/API response is backwards-compatible with Zorro v6.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from buyer_profile import clean_profile, merge_profile, retrieval_context
from evidence_v7 import eligible, full_facts, rank_candidates, choose_ids
from sales_actions import (
    explicit_checkout_intent, trusted_product_url, resolve_checkout_product,
    verified_product_links,
)

LOG = logging.getLogger('zorro.consultation')
from quality_telemetry import add as measure
ALLOWED = {'search', 'compare', 'recommend', 'describe', 'store_info', 'clarify', 'checkout'}


def planning_tools():
    return [{'functionDeclarations': [{
        'name': 'plan_consultation',
        'description': 'Plan one or more actions for a Ukrainian watch store. No SQL. State only new or changed customer requirements.',
        'parameters': {'type': 'OBJECT', 'properties': {
            'actions': {'type': 'ARRAY', 'items': {'type': 'STRING', 'enum': sorted(ALLOWED)},
                        'description': 'All actions necessary, e.g. compare+recommend or compare+search. Keep original order of buyer request.'},
            'mode': {'type': 'STRING', 'enum': ['new', 'refine']},
            'filters_delta': {'type': 'OBJECT', 'properties': {
                'category': {'type': 'STRING'}, 'max_price': {'type': 'INTEGER'}, 'min_price': {'type': 'INTEGER'},
                'brand': {'type': 'STRING'}, 'sku': {'type': 'STRING'}, 'strap': {'type': 'STRING'},
                'exclude_leather': {'type': 'BOOLEAN'}, 'calling': {'type': 'BOOLEAN'},
                'mechanism': {'type': 'STRING'}, 'sim': {'type': 'BOOLEAN'},
                'keywords': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
                'remove_filters': {'type': 'ARRAY', 'items': {'type': 'STRING'}}
            }},
            'search_query': {'type': 'STRING', 'description': 'Semantic purpose and style from buyer, retaining previous goal when refining'},
            'relative_price': {'type': 'STRING', 'enum': ['none','cheaper','dearer'],
                               'description': 'Use for a relative price request compared with shown watches, with no explicit new amount.'},
            'new_alternatives': {'type': 'BOOLEAN', 'description': 'True if buyer wants previously unshown other models'},
            'profile_update': {'type': 'OBJECT', 'properties': {
                'goal': {'type': 'STRING'}, 'reset': {'type': 'BOOLEAN'},
                'reject_ids': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
                'changes': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
                    'feature': {'type': 'STRING'},
                    'status': {'type': 'STRING', 'enum': ['must','prefer','avoid','indifferent','remove']}
                }, 'required': ['feature','status']}}
            }},
            'positions': {'type': 'ARRAY', 'items': {'type': 'INTEGER'}},
            'ids': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
            'clarification': {'type': 'STRING', 'description': 'Ask exactly one question only when a key reference or requirement is missing.'}
        }, 'required': ['actions', 'mode']}
    }]}]


PLANNER = (
    'You are the planning brain of Zorro, a Ukrainian wristwatch sales consultant. '
    'Call plan_consultation once, with ALL necessary actions. Python executes actions, not you. '
    'Classify intent from meaning, not isolated keywords. Actions can combine compare, recommend and search. '
    'Compare means describe VERIFIED differences between multiple previously shown models; recommend means pick a winner '
    'with a reason. If buyer says "which of those is better and why", choose BOTH compare and recommend. '
    'Never arbitrarily pick one model when buyer asks about two or three. '
    'Use ids / positions only for products that were previously shown. '
    'If 3 were shown and user asks "these two" without identifying which two, ask clarification, do NOT guess. '
    'Do not ask for details user already supplied. When buyer changes a requirement, include filters_delta and profile_update. '
    'For "compass is not important" set indifferent, NOT avoid. For "without compass" set avoid. '
    'For explicit required feature, use must. For an optional wish, prefer. '
    'Represent an occasion such as hiking or gift in goal, not as a falsely verified feature. '
    'New unrelated shopping intent => mode new, reset old context. Otherwise refine. '
    'Use store_info ONLY for delivery/returns/payment/policy questions, not hallucinated store policies. '
    'When the customer wants to place an order or buy a model, use checkout, NOT store_info. '
    'A checkout action guides the customer to the official Zorro product page; it never places an order. '
    'If multiple previously shown models exist, specify the requested ID/position only when unambiguous. '
    'If buyer explicitly requests other models in addition to comparing, actions compare+search. '
    'Respect price/budget, gender/category, brand, negative requirements. '
    'For objective verifiable mandatory feature use must, for subjective goals like sturdy or beautiful use prefer or goal. '
    'For structured fields category, brand, price, strap, calling, SIM and mechanism, use filters_delta ONLY; do not duplicate the same mandatory condition in profile_update.changes. '
    'When buyer asks for cheaper/dearer without saying a new amount, set relative_price accordingly. '
    'If they ask for different watches than last suggestions, set new_alternatives true. '
    'Do not put earlier constraints into filters_delta unless changed this turn. '
    'No invented product IDs, stock claims or characteristics.'
)


def _model_call(message: str, history: list[dict], state: dict, shown: list[dict], current_title: str | None):
    import main
    context = {'latest_message': message, 'dialogue': history[-8:],
               'current_filters': state['filters'], 'buyer_profile': state['buyer_profile'],
               'previous_shown': [full_facts(p) for p in shown[:5]],
               'last_search_intent': state['search_intent'], 'opened_product': current_title}
    response = main.gemini([{'role': 'user', 'parts': [{'text': json.dumps(context, ensure_ascii=False)}]}],
                            PLANNER, planning_tools())
    for part in main.model_parts(response):
        call = part.get('functionCall')
        if isinstance(call, dict) and call.get('name') == 'plan_consultation':
            return call.get('args') or {}
    raise ValueError('Missing plan_consultation function call')


def _fallback_plan(text: str, shown: list[dict]) -> dict:
    """Safe reduced service if Gemini tool call fails; not a keyword-based intelligence system."""
    if shown:
        return {'actions': ['clarify'], 'mode': 'refine',
                'clarification': 'Зараз не вдалося точно зрозуміти запит. Можете коротко уточнити, що саме хочете порівняти або змінити?'}
    return {'actions': ['clarify'], 'mode': 'new',
            'clarification': 'Підкажіть, для кого обираємо годинник і що для вас найважливіше?'}


def _validated_plan(plan: Any) -> dict:
    plan = plan if isinstance(plan, dict) else {}
    acts = plan.get('actions', [])
    actions = list(dict.fromkeys(x for x in acts if isinstance(x,str) and x in ALLOWED))[:4] if isinstance(acts,list) else []
    return {'actions': actions or ['clarify'],
            'mode': 'new' if plan.get('mode') == 'new' else 'refine',
            'filters_delta': plan.get('filters_delta') if isinstance(plan.get('filters_delta'), dict) else {},
            'search_query': str(plan.get('search_query') or '')[:240],
            'relative_price': plan.get('relative_price') if plan.get('relative_price') in ('cheaper','dearer') else 'none',
            'new_alternatives': plan.get('new_alternatives') is True,
            'profile_update': plan.get('profile_update') if isinstance(plan.get('profile_update'), dict) else {},
            'positions': plan.get('positions') if isinstance(plan.get('positions'), list) else [],
            'ids': plan.get('ids') if isinstance(plan.get('ids'), list) else [],
            'clarification': str(plan.get('clarification') or '')[:240]}


def resolve_references(plan: dict, shown: list[dict], message: str) -> tuple[list[dict], bool]:
    """Only resolve from actual products served to browser in preceding turn."""
    by_id = {str(p['site_id']): p for p in shown}
    selected = []
    for i in plan['positions'][:6]:
        if type(i) is int and 1 <= i <= len(shown):
            p = shown[i-1]
            if p not in selected:
                selected.append(p)
    for value in plan['ids'][:6]:
        sid = str(value)
        if sid in by_id and by_id[sid] not in selected:
            selected.append(by_id[sid])
    if not selected:
        # Names supplied by buyer can be resolved from actual displayed rows.
        mentioned = [p for p in shown if len(str(p.get('name') or '')) > 4
                     and str(p['name']).casefold() in message.casefold()]
        selected = mentioned or shown[:]
    count_two = bool(re.search(r'\b(двох|дві|два|обидв[аі])\b', message.casefold()))
    explicit_names = sum(bool(str(p.get('name') or '').casefold() in message.casefold()) for p in shown)
    explicit_numbers = bool(re.search(r'\b(перш\w*|друг\w*|трет\w*|1\s*(?:і|та|&)\s*2|1\s*(?:і|та|&)\s*3|2\s*(?:і|та|&)\s*3)\b', message.casefold()))
    # Gemini must not guess which two if three were offered and buyer gave no reference.
    ambiguous = (len(shown) > 2 and count_two and explicit_names < 2 and not explicit_numbers)
    return selected[:3], ambiguous


def _rerank(query: str, candidates: list[dict], state: dict, message: str) -> list[dict]:
    import main
    # MODEL IS NEVER ASKED TO SEARCH ALL PRODUCTS. Only a short trusted pool.
    if not candidates:
        return []
    request = {'query': message, 'purpose': state['buyer_profile'].get('goal',''),
               'preferences': state['buyer_profile']['preferences'],
               'current_filters': state['filters'],
               'candidates': [full_facts(p) for p in candidates[:24]]}
    system = (
        'You are ranking previously retrieved watch products for Zorro. '
        'Choose 1-3 real IDs from the supplied candidate list, best meeting buyer priorities. '
        'All candidates have already passed strict price and feature validation in Python. '
        'Treat incomplete information as unknown. Do not infer impact resistance, comfort, quality or waterproofing '
        'from casing material, appearance or ATM. Select variety only when relevant. '
        'Return ONLY JSON {"selected_ids":["id1","id2","id3"]}. '
        'Never invent identifiers or use knowledge of products outside supplied candidate list.'
    )
    try:
        response = main.gemini([{'role': 'user', 'parts': [{'text': json.dumps(request, ensure_ascii=False)}]}],
                               system, json_mode=True)
        raw = ''.join(x.get('text','') for x in main.model_parts(response))
        selected_ids = choose_ids(json.loads(raw).get('selected_ids'), candidates, 3)
        index = {str(p['site_id']):p for p in candidates}
        selected = [index[k] for k in selected_ids]
        return selected or candidates[:3]
    except Exception as ex:
        LOG.warning('Rerank fallback: %s', type(ex).__name__)
        measure('rerank_fallbacks')
        return candidates[:3]


def _safe_fallback(action: str, products: list[dict], message: str, filters: dict, profile: dict) -> str:
    import main
    if action == 'compare':
        return main.compare_text(products, message, {**filters,'buyer_profile':profile})
    if action == 'recommend':
        return main.personal_recommendation(products,message,{**filters,'buyer_profile':profile})
    if action == 'describe' and products:
        return main.describe_text(products[0],message,{**filters,'buyer_profile':profile})
    return 'Підібрав доступні моделі під ваші вимоги. Їхні ціни й характеристики показані в картках нижче.'


def _respond(action: str, message: str, products: list[dict], filters: dict, profile: dict, history: list[dict]):
    import main
    purpose = {'search':'search', 'compare':'compare', 'recommend':'recommend', 'describe':'describe',
               'compare_recommend':'compare_and_recommend'}[action]
    return main.sales_reply(purpose, message, products, filters, history,
            lambda: _safe_fallback('compare' if action == 'compare_recommend' else action,
                                   products,message,filters,profile), profile)


def run(data) -> dict:
    import main
    measure('chat_requests')
    current = data.state if isinstance(data.state, dict) else {}
    state = {'filters': current.get('filters') if isinstance(current.get('filters'),dict) else {},
             'product_ids': [str(x)[:64] for x in current.get('product_ids', [])[:5]] if isinstance(current.get('product_ids'),list) else [],
             'search_intent': str(current.get('search_intent') or '')[:350],
             'buyer_profile': clean_profile(current.get('buyer_profile')),
             'selected_product_id': str(current.get('selected_product_id') or '')[:64]}
    message = data.message.strip()[:2000]
    history = [{'role': m.role, 'content': re.sub(r'<[^>]*>', ' ', m.content)[:450]}
               for m in data.history[-10:] if m.role in ('user','assistant')]
    shown = main.get_ids(state['product_ids'])
    try:
        plan = _validated_plan(_model_call(message, history, state, shown, data.current_product))
    except Exception as ex:
        LOG.warning('Planner unavailable: %s',type(ex).__name__)
        measure('planner_fallbacks')
        plan = _validated_plan(_fallback_plan(message, shown))
    # A direct purchase command must not be misclassified as a store-policy question.
    if explicit_checkout_intent(message):
        plan['actions'] = ['checkout']
        plan['mode'] = 'refine'
    actions = plan['actions']
    if plan['mode'] == 'new' and not ('search' in actions or not shown):
        # A mistaken 'new' action may not discard the reference set under comparison.
        plan['mode'] = 'refine'
    if plan['mode'] == 'new':
        state['filters'] = {}
        state['search_intent'] = ''
        shown = []
        state['product_ids'] = []
        state['selected_product_id'] = ''
    delta = dict(plan['filters_delta'])
    delta['mode'] = plan['mode']
    filters = main.merge_filter_state(state['filters'],delta,message)
    # Relative price refers to the verified rows the buyer saw, not a guessed amount.
    if shown and plan['relative_price'] != 'none' and not any(k in delta for k in ('min_price','max_price')):
        prices = [int(float(p['price'])) for p in shown if float(p.get('price') or 0) > 0]
        if prices:
            if plan['relative_price'] == 'cheaper':
                ceiling = min(prices) - 1
                if ceiling > 0:
                    filters['max_price'] = min(ceiling, int(filters.get('max_price') or ceiling))
            elif plan['relative_price'] == 'dearer':
                floor = max(prices) + 1
                filters['min_price'] = max(floor, int(filters.get('min_price') or floor))
    state['filters'] = filters
    state['buyer_profile'] = merge_profile(state['buyer_profile'],plan['profile_update'],
                new_topic=(plan['mode']=='new'),
                previously_shown=[str(p['site_id']) for p in shown])

    if 'checkout' in actions:
        # Product selection is made only from previously verified catalog rows.
        chosen = resolve_checkout_product(shown, plan, message, history)
        if not chosen and state['selected_product_id']:
            chosen = next((p for p in shown if str(p['site_id']) ==
                           state['selected_product_id']), None)
        if not chosen and data.current_product:
            opened = main.find_current_product(data.current_product)
            if opened and (not shown or any(str(p['site_id']) ==
                                            str(opened['site_id']) for p in shown)):
                chosen = opened
        if not chosen:
            if shown:
                options = '; '.join('%d — %s' % (i + 1, p['name'])
                                    for i, p in enumerate(shown[:3]))
                reply = 'Допоможу з оформленням. Яку саме модель обрали: ' + options + '?'
                return {'reply': reply, 'products': [], 'state': state,
                        'product_links': verified_product_links(reply, shown)}
            return {'reply': 'Із задоволенням допоможу оформити замовлення. '
                             'Яку модель годинника ви хочете купити?',
                    'products': [], 'state': state, 'product_links': []}
        url = trusted_product_url(chosen.get('url'))
        if not url:
            return {'reply': 'Для цієї моделі поки не вдалося перевірити посилання '
                             'на сторінку покупки. Можете звернутися до менеджера '
                             'через кнопку телефону в чаті.',
                    'products': [], 'state': state, 'product_links': []}
        state['selected_product_id'] = str(chosen['site_id'])
        reply = ('Для оформлення замовлення на «%s» перейдіть на сторінку товару '
                 'та додайте його до кошика. Я не оформлюю покупку замість вас, '
                 'але можу допомогти з вибором або запропонувати зв’язок із менеджером.'
                 ) % chosen['name']
        return {'reply': reply, 'products': [], 'state': state,
                'product_links': verified_product_links(reply, [chosen]),
                'checkout': {'product_id': str(chosen['site_id']), 'url': url}}

    if 'store_info' in actions:
        return {'reply':'Умови доставки, оплати, гарантії та повернення потрібно звіряти з актуальними сторінками Zorro. Не називатиму непідтверджених умов.',
                'products':[], 'state':state}
    if 'clarify' in actions and not any(a in actions for a in ('search','compare','describe','recommend')):
        measure('clarifications')
        return {'reply':plan['clarification'] or 'Що для вас найважливіше під час вибору годинника?',
                'products':[], 'state':state}

    response_parts = []
    shown_new = []
    previous_actions = [x for x in actions if x in ('compare','recommend','describe')]
    if previous_actions:
        referenced, ambiguity = resolve_references(plan,shown,message)
        if ambiguity:
            measure('clarifications')
            listed = '; '.join('%d — %s' % (i+1,p['name']) for i,p in enumerate(shown))
            return {'reply':'Ви згадали дві моделі, але раніше ми розглядали три. Які саме дві порівняти? '+listed,
                    'products':[],'state':state,
                    'product_links':verified_product_links('Ви згадали дві моделі, але раніше ми розглядали три. Які саме дві порівняти? '+listed,shown)}
        if not referenced and 'describe' in previous_actions and data.current_product:
            p = main.find_current_product(data.current_product)
            referenced = [p] if p else []
        if not referenced:
            return {'reply':'Уточніть, будь ласка, які саме моделі з показаних порівняти або оцінити.',
                    'products':[],'state':state}
        if 'compare' in previous_actions:
            measure('comparisons')
        if 'recommend' in previous_actions:
            measure('recommendations')
        if 'compare' in previous_actions and 'recommend' in previous_actions:
            if len(referenced) < 2:
                return {'reply':'Щоб порівняти моделі, назвіть хоча б дві.', 'products':[],'state':state}
            response_parts.append(_respond('compare_recommend',message,referenced,filters,state['buyer_profile'],history))
        elif 'compare' in previous_actions:
            if len(referenced)<2:
                return {'reply':'Які саме дві моделі хочете порівняти?', 'products':[],'state':state}
            response_parts.append(_respond('compare',message,referenced,filters,state['buyer_profile'],history))
        elif 'recommend' in previous_actions:
            response_parts.append(_respond('recommend',message,referenced,filters,state['buyer_profile'],history))
        elif 'describe' in previous_actions:
            response_parts.append(_respond('describe',message,referenced[:1],filters,state['buyer_profile'],history))

    if 'search' in actions:
        measure('searches')
        proposed_query = plan['search_query'].strip()
        prior = state['search_intent']
        query = (proposed_query or ((prior + ' ' + message).strip() if plan['mode']=='refine' and prior else message))[:400]
        context = retrieval_context(state['buyer_profile'])
        keywords = filters.get('keywords') or []
        query = (query + ' ' + context + ' ' + ' '.join(str(k) for k in keywords[:4]))[:470]
        pool = main.shortlist(filters, query, 80)
        exclusions = set(state['buyer_profile']['rejected_ids'])
        if plan['new_alternatives'] or any(a in actions for a in ('compare','recommend')):
            exclusions.update(str(p['site_id']) for p in shown)
        pool = [p for p in pool if str(p['site_id']) not in exclusions]
        pool = rank_candidates(pool, state['buyer_profile'], query, 24)
        if not pool:
            measure('no_results')
            response_parts.append('Не знайшов товарів, які одночасно мають підтверджені потрібні характеристики й відповідають бюджету. Яку з вимог можемо змінити?')
            state['search_intent'] = (proposed_query or query)[:350]
        else:
            shown_new = _rerank(query,pool,state,message)
            response_parts.append(_respond('search', message, shown_new, filters,state['buyer_profile'],history))
            state['product_ids'] = [str(p['site_id']) for p in shown_new]
            state['search_intent'] = (proposed_query or query)[:350]

    reply = '\n\n'.join(p for p in response_parts if p)
    if not reply:
        reply = plan['clarification'] or 'Уточніть, будь ласка, що саме хочете знайти або порівняти.'
    reply = reply[:1400]
    return {'reply': reply, 'products': [main.render_product(p) for p in shown_new],
            'state': state,
            'product_links': verified_product_links(reply, shown_new + shown)}
