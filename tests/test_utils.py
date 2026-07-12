from utils import looks_like_question, TTLCache


def test_looks_like_question_true_for_real_question():
    assert looks_like_question("Qual e a capital do Brasil?") is True


def test_looks_like_question_false_for_slash_command():
    assert looks_like_question("/ask oi?") is False


def test_looks_like_question_false_without_question_mark():
    assert looks_like_question("oi tudo bem") is False


def test_looks_like_question_false_too_short():
    assert looks_like_question("oi?") is False


def test_looks_like_question_ignores_question_mark_inside_url():
    assert looks_like_question("confere https://youtu.be/watch?v=abc123") is False


def test_ttlcache_miss_on_unknown_key():
    cache = TTLCache(ttl_seconds=60)
    value, hit = cache.get("missing")
    assert hit is False
    assert value is None


def test_ttlcache_hit_after_set():
    cache = TTLCache(ttl_seconds=60)
    cache.set("curitiba", {"lat": -25.43, "lon": -49.27})
    value, hit = cache.get("curitiba")
    assert hit is True
    assert value == {"lat": -25.43, "lon": -49.27}


def test_ttlcache_expires_after_ttl():
    cache = TTLCache(ttl_seconds=-1)
    cache.set("curitiba", "algo")
    value, hit = cache.get("curitiba")
    assert hit is False
    assert value is None


def test_ttlcache_caches_negative_result():
    cache = TTLCache(ttl_seconds=60)
    cache.set("cidadeinexistente", None)
    value, hit = cache.get("cidadeinexistente")
    assert hit is True
    assert value is None
