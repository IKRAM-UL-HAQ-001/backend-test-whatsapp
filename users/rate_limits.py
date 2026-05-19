from django.core.cache import cache


def hit_rate_limit(scope, identifier, limit, window_seconds):
    cache_key = f"rate-limit:{scope}:{identifier}"
    current = cache.get(cache_key, 0)
    if current >= limit:
        return True
    if current == 0:
        cache.set(cache_key, 1, timeout=window_seconds)
    else:
        try:
            cache.incr(cache_key)
        except ValueError:
            cache.set(cache_key, current + 1, timeout=window_seconds)
    return False
