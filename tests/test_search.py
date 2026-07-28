from cogs.search import wants_web_search, _extract_real_url


def test_wants_web_search_matches_pesquisa_variants():
    assert wants_web_search("pesquisa sobre gatos") is True
    assert wants_web_search("pode pesquisar isso pra mim?") is True
    assert wants_web_search("PESQUISE agora") is True


def test_wants_web_search_ignores_busca_and_procura():
    """"buscar"/"procurar" sao palavras do dia a dia - disparar busca web nelas fazia
    conversa normal virar pesquisa sem querer. O gatilho e restrito a "pesquis-" de
    proposito (regressao introduzida na refatoracao em Cogs, ver commit 5432f0f)."""
    assert wants_web_search("busca ai quanto custa") is False
    assert wants_web_search("to procurando um jogo bom") is False
    assert wants_web_search("fui buscar meu irmao na escola") is False


def test_wants_web_search_ignores_unrelated_text():
    assert wants_web_search("oi, tudo bem?") is False
    assert wants_web_search("qual o clima hoje") is False


def test_extract_real_url_from_ddg_redirect():
    ddg_href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert _extract_real_url(ddg_href) == "https://example.com/page"


def test_extract_real_url_passthrough_for_direct_url():
    assert _extract_real_url("https://example.com/direct") == "https://example.com/direct"
