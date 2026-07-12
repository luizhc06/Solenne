from cogs.weather import _TTLCache, _strip_accents, _state_to_uf


def test_ttlcache_miss_on_unknown_key():
    cache = _TTLCache(ttl_seconds=60)
    value, hit = cache.get("missing")
    assert hit is False
    assert value is None


def test_ttlcache_hit_after_set():
    cache = _TTLCache(ttl_seconds=60)
    cache.set("curitiba", {"lat": -25.43, "lon": -49.27})
    value, hit = cache.get("curitiba")
    assert hit is True
    assert value == {"lat": -25.43, "lon": -49.27}


def test_ttlcache_expires_after_ttl():
    cache = _TTLCache(ttl_seconds=-1)
    cache.set("curitiba", "algo")
    value, hit = cache.get("curitiba")
    assert hit is False
    assert value is None


def test_ttlcache_caches_negative_result():
    # _geocode_city_sync guarda None pra cidade nao encontrada, pra nao bater
    # de novo na API com o mesmo erro de digitacao dentro do TTL.
    cache = _TTLCache(ttl_seconds=60)
    cache.set("cidadeinexistente", None)
    value, hit = cache.get("cidadeinexistente")
    assert hit is True
    assert value is None


def test_strip_accents():
    assert _strip_accents("São Paulo") == "Sao Paulo"
    assert _strip_accents("Ceará") == "Ceara"
    assert _strip_accents("Brasilia") == "Brasilia"


def test_state_to_uf_known_state():
    assert _state_to_uf("Parana") == "PR"
    assert _state_to_uf("Paraná") == "PR"
    assert _state_to_uf("sao paulo") == "SP"


def test_state_to_uf_unknown_state():
    assert _state_to_uf("Nao E Um Estado") is None
