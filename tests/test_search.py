from unittest.mock import MagicMock, patch

import httpx

from cogs.search import _extract_real_url, _web_search_sync, _web_search_tavily


def test_extract_real_url_from_ddg_redirect():
    ddg_href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert _extract_real_url(ddg_href) == "https://example.com/page"


def test_extract_real_url_passthrough_for_direct_url():
    assert _extract_real_url("https://example.com/direct") == "https://example.com/direct"


def test_tavily_sem_chave_configurada_devolve_none_sem_chamar_rede():
    """None (nao lista vazia) e o sinal pro chamador cair pro DDG - achado do conselho
    de agentes, 18/08/2026: distinguir "nao configurada"/"fora do ar" de "buscou e nao
    achou nada" e o que evita tratar Tavily indisponivel como busca sem resultado."""
    with patch("cogs.search.TAVILY_API_KEY", ""):
        with patch("httpx.post") as mock_post:
            assert _web_search_tavily("teste", 5) is None
            mock_post.assert_not_called()


def test_tavily_mapeia_title_url_content_para_o_formato_do_bot():
    resposta = MagicMock()
    resposta.raise_for_status = MagicMock()
    resposta.json.return_value = {
        "results": [
            {"title": "Titulo 1", "url": "https://a.com", "content": "trecho 1"},
            {"title": "", "url": "https://b.com", "content": "sem titulo, deve ser descartado"},
            {"title": "Titulo 3", "url": "", "content": "sem url, deve ser descartado"},
        ]
    }
    with patch("cogs.search.TAVILY_API_KEY", "chave-de-teste"):
        with patch("httpx.post", return_value=resposta) as mock_post:
            resultados = _web_search_tavily("teste", 5)

    mock_post.assert_called_once()
    assert resultados == [{"title": "Titulo 1", "url": "https://a.com", "snippet": "trecho 1"}]


def test_tavily_fora_do_ar_devolve_none_e_web_search_sync_cai_pro_ddg():
    """Falha de transporte (rede/HTTP) nao pode virar "nenhum resultado" - tem que cair
    pro fallback do DuckDuckGo em vez de responder vazio."""
    with patch("cogs.search.TAVILY_API_KEY", "chave-de-teste"):
        with patch("httpx.post", side_effect=httpx.ConnectError("recusado")):
            assert _web_search_tavily("teste", 5) is None

        with patch("httpx.post", side_effect=httpx.ConnectError("recusado")):
            with patch("cogs.search._web_search_ddg", return_value=[{"title": "x", "url": "y", "snippet": "z"}]) as mock_ddg:
                resultados = _web_search_sync("teste")

    mock_ddg.assert_called_once()
    assert resultados == [{"title": "x", "url": "y", "snippet": "z"}]


def test_web_search_sync_usa_tavily_quando_configurada_sem_tocar_no_ddg():
    resposta = MagicMock()
    resposta.raise_for_status = MagicMock()
    resposta.json.return_value = {"results": [{"title": "T", "url": "https://a.com", "content": "c"}]}
    with patch("cogs.search.TAVILY_API_KEY", "chave-de-teste"):
        with patch("httpx.post", return_value=resposta):
            with patch("cogs.search._web_search_ddg") as mock_ddg:
                resultados = _web_search_sync("teste")

    mock_ddg.assert_not_called()
    assert resultados == [{"title": "T", "url": "https://a.com", "snippet": "c"}]
