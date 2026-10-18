"""Non-persistent anonymous counters. No customer text, contacts or tokens.
Reset after process restart; use structured logs/real observability for long-term analysis.
"""
from collections import Counter
from threading import Lock

_lock = Lock()
_counts = Counter()


def add(name: str):
    if name not in {'chat_requests','planner_fallbacks','rerank_fallbacks',
                    'no_results','clarifications','searches','comparisons','recommendations'}:
        return
    with _lock:
        _counts[name] += 1


def snapshot():
    with _lock:
        return {'status':'ok', 'metrics':dict(_counts), 'note':
                'Per-process anonymous counters only; reset on redeploy. No chat content stored.'}
