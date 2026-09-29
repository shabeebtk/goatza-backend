from django.core.cache import cache


def cache_is_degraded():
    """
    True when the cache backend is knowingly skipping Redis right now.

    THE ONE PLACE anything outside core.cache asks that question, so no caller
    has to know which backend is configured. LocMemCache has no such state and
    answers False — correct: there is nothing to be down.

    Ask this wherever a MISS and a FAILURE mean different things. Two places
    do: the /healthz component report, and the Places budget guard, where
    "nothing spent today" and "I cannot tell you what was spent" must not be
    treated the same way. See core/cache/resilient.py.
    """
    checker = getattr(cache, "is_degraded", None)
    return bool(checker()) if callable(checker) else False


def cache_get(key):
    return cache.get(key)

def cache_set(key, value, timeout=300):
    cache.set(key, value, timeout)

def cache_add(key, value, timeout=300):
    """
    Set only if the key is absent. Returns True when this caller is the one
    that set it.

    Unlike get-then-set, this is atomic in the backend, which is what makes it
    usable as a "count this once" latch under concurrent requests.
    """
    return cache.add(key, value, timeout)

def cache_delete(key):
    cache.delete(key)

def cache_delete_many(keys:list):
    cache.delete_many(keys)