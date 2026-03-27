from django.core.cache import cache

def cache_get(key):
    return cache.get(key)

def cache_set(key, value, timeout=300):
    cache.set(key, value, timeout)

def cache_delete(key):
    cache.delete(key)

def cache_delete_many(keys:list):
    cache.delete_many(keys)