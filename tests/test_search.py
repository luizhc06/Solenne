from cogs.search import wants_web_search, _extract_real_url


def test_wants_web_search_matches_pesquisa_variants():
    assert wants_web_search("pesquisa sobre gatos") is True
    assert wants_web_search("pode pesquisar isso pra mim?") is True
    assert wants_web_search("PESQUISE agora") is True


def test_wants_web_search_matches_busca_and_procura_variants():
    assert wants_web_search("busca ai quanto custa") is True
    assert wants_web_search("procura sobre o assunto") is True


def test_wants_web_search_ignores_unrelated_text():
    assert wants_web_search("oi, tudo bem?") is False
    assert wants_web_search("qual o clima hoje") is False


def test_extract_real_url_from_ddg_redirect():
    ddg_href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert _extract_real_url(ddg_href) == "https://example.com/page"


def test_extract_real_url_passthrough_for_direct_url():
    assert _extract_real_url("https://example.com/direct") == "https://example.com/direct"
