import json
import asyncio

import pytest

import tools


@pytest.fixture(autouse=True)
def registro_limpo():
    """Cada teste comeca com o registro vazio e devolve o original no fim - senao um
    teste veria as ferramentas de verdade que os cogs registram ao serem importados."""
    original = dict(tools._REGISTRY)
    tools._REGISTRY.clear()
    yield
    tools._REGISTRY.clear()
    tools._REGISTRY.update(original)


def _registrar(nome="somar", handler=None):
    @tools.register(
        name=nome,
        description="Soma dois numeros",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    )
    async def _padrao(a, b):
        return tools.ToolResult(json.dumps({"resultado": a + b}))

    return handler or _padrao


def test_tool_specs_sai_no_formato_da_api():
    _registrar()
    especificacoes = tools.tool_specs()
    assert len(especificacoes) == 1
    assert especificacoes[0]["type"] == "function"
    assert especificacoes[0]["function"]["name"] == "somar"
    assert "a" in especificacoes[0]["function"]["parameters"]["properties"]


def test_sem_ferramenta_registrada_a_lista_e_vazia():
    """answer_with_tools usa isso pra cair no caminho antigo em vez de mandar
    tools=[] pra API, que e pedido invalido."""
    assert tools.tool_specs() == []


def test_execute_tool_roda_e_devolve_o_resultado():
    _registrar()
    resultado = asyncio.run(tools.execute_tool("somar", '{"a": 2, "b": 3}'))
    assert json.loads(resultado.content) == {"resultado": 5}


def test_ferramenta_inexistente_vira_erro_pro_modelo():
    """Nao pode levantar: o modelo consegue se recuperar lendo o erro, mas quem esta no
    canal nao se recupera de silencio."""
    resultado = asyncio.run(tools.execute_tool("nao_existe", "{}"))
    assert "erro" in json.loads(resultado.content)


def test_argumentos_invalidos_viram_erro_pro_modelo():
    _registrar()
    resultado = asyncio.run(tools.execute_tool("somar", "isso nao e json"))
    assert "erro" in json.loads(resultado.content)


def test_parametro_faltando_vira_erro_pro_modelo():
    _registrar()
    resultado = asyncio.run(tools.execute_tool("somar", '{"a": 2}'))
    assert "erro" in json.loads(resultado.content)


def test_excecao_dentro_da_ferramenta_vira_erro_pro_modelo():
    @tools.register(name="quebra", description="x", parameters={"type": "object", "properties": {}})
    async def _quebra():
        raise RuntimeError("estourou tudo")

    resultado = asyncio.run(tools.execute_tool("quebra", "{}"))
    assert "estourou tudo" in json.loads(resultado.content)["erro"]


def test_ferramenta_pendurada_e_cancelada_por_timeout(monkeypatch):
    """Sem teto, uma busca travada segura o portao de IA e a pessoa fica olhando o
    "Pensando..." pra sempre - o mesmo sintoma que ja foi reclamado antes."""
    monkeypatch.setattr(tools, "TOOL_TIMEOUT_SECONDS", 0.05)

    @tools.register(name="lenta", description="x", parameters={"type": "object", "properties": {}})
    async def _lenta():
        await asyncio.sleep(5)
        return tools.ToolResult("{}")

    resultado = asyncio.run(tools.execute_tool("lenta", "{}"))
    assert "demorou demais" in json.loads(resultado.content)["erro"]


def test_contexto_comeca_vazio():
    assert tools.current_context() is None


def test_use_context_disponibiliza_e_limpa():
    """A identidade da requisicao vive em contextvar, nao em parametro de ferramenta:
    se o modelo pudesse escolher o user_id, bastaria pedir "cria um lembrete pro fulano"
    pra escrever em nome de outra pessoa."""
    ctx = tools.ToolContext(author_id=42, author_name="rizu", channel_id=7)
    with tools.use_context(ctx):
        atual = tools.current_context()
        assert atual.author_id == 42
        assert atual.channel_id == 7
    assert tools.current_context() is None


def test_contexto_chega_dentro_da_ferramenta():
    """Precisa atravessar o await do execute_tool - se nao atravessasse, toda ferramenta
    que depende de identidade falharia em producao e passaria nos testes unitarios."""
    visto = {}

    @tools.register(name="quem", description="x", parameters={"type": "object", "properties": {}})
    async def _quem():
        ctx = tools.current_context()
        visto["author_id"] = ctx.author_id if ctx else None
        return tools.ToolResult("{}")

    with tools.use_context(tools.ToolContext(author_id=99, author_name="rizu", channel_id=1)):
        asyncio.run(tools.execute_tool("quem", "{}"))
    assert visto["author_id"] == 99
