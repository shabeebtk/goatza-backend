from django.core.cache import cache

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