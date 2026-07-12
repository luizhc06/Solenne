from cogs.weather import _strip_accents, _state_to_uf


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
