import asyncio

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from ai_client import (
    EmptyAIResponse,
    PriorityGate,
    THINK_BUDGET,
    THINK_LOW,
    THINK_OFF,
    clean_reply,
    friendly_ai_error,
    is_transient_ai_error,
    parse_json_payload,
    thinking_kwargs,
)


def test_thinking_off_desliga_o_raciocinio():
    assert thinking_kwargs(THINK_OFF) == {"chat_template_kwargs": {"enable_thinking": False}}


def test_thinking_budget_liga_o_raciocinio_com_teto():
    """O centro da correcao: ate ago/2026 TODA chamada mandava enable_thinking=False,
    o que transforma um modelo de raciocinio num modelo de 12B que responde de primeira."""
    kwargs = thinking_kwargs(THINK_BUDGET, budget=512)["chat_template_kwargs"]
    assert kwargs["enable_thinking"] is True
    assert kwargs["reasoning_budget"] == 512


def test_thinking_low_usa_low_effort():
    kwargs = thinking_kwargs(THINK_LOW)["chat_template_kwargs"]
    assert kwargs == {"enable_thinking": True, "low_effort": True}


def test_clean_reply_tira_tag_de_raciocinio_e_prefixo_de_nome():
    """Se o backend nao separar o trace em reasoning_content, ele vem como <think> no
    texto; e o modelo as vezes assina a fala com o proprio nome. Nada disso vai pro chat."""
    assert clean_reply("<think>hmm deixa eu ver</think>\nSolenne: oi, tudo certo") == "oi, tudo certo"
    assert clean_reply("resposta normal") == "resposta normal"


def test_parse_json_payload_aceita_cerca_de_codigo():
    assert parse_json_payload('```json\n{"noticias": []}\n```') == {"noticias": []}


def test_parse_json_payload_resgata_json_com_texto_em_volta():
    assert parse_json_payload('Claro! {"noticias": [1]} espero ter ajudado') == {"noticias": [1]}


def test_parse_json_payload_falha_sem_json():
    with pytest.raises(ValueError):
        parse_json_payload("nao tem json nenhum aqui")


def test_resposta_vazia_nao_e_retentada():
    """Repetir a mesma chamada da o mesmo resultado (o raciocinio estourou o
    max_tokens): quem trata e o chamador, refazendo com outros parametros."""
    assert is_transient_ai_error(EmptyAIResponse("vazio")) is False


def test_resposta_vazia_tem_mensagem_propria_no_discord():
    msg = friendly_ai_error(EmptyAIResponse("vazio"))
    assert "Deu ruim" not in msg
    assert msg.strip()


def test_priority_gate_deixa_o_chat_passar_na_frente_do_digest():
    """Enquanto o digest de noticias roda, uma mencao ficava presa atras dele na fila
    FIFO e a pessoa via so o "Pensando..." parado por minutos."""
    async def cenario():
        gate = PriorityGate()
        ordem = []

        async def fundo():
            async with gate.background():
                ordem.append("digest")

        async def chat():
            async with gate.interactive():
                ordem.append("chat")

        async with gate.background():  # primeira categoria do digest, ja rodando
            tarefa_fundo = asyncio.create_task(fundo())
            await asyncio.sleep(0)  # o digest entra na fila primeiro
            tarefa_chat = asyncio.create_task(chat())
            await asyncio.sleep(0)
        await asyncio.gather(tarefa_fundo, tarefa_chat)
        return ordem

    assert asyncio.run(cenario()) == ["chat", "digest"]


def _status_error(code: int):
    request = httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
    response = httpx.Response(code, request=request)
    cls = {401: AuthenticationError, 429: RateLimitError}.get(code, APIStatusError)
    return cls("erro", response=response, body=None)


def _timeout():
    return APITimeoutError(httpx.Request("POST", "https://x"))


def _connection():
    return APIConnectionError(request=httpx.Request("POST", "https://x"))


@pytest.mark.parametrize("exc", [_timeout(), _connection(), _status_error(429), _status_error(500), _status_error(504)])
def test_transient_errors_are_retried(exc):
    assert is_transient_ai_error(exc) is True


@pytest.mark.parametrize("exc", [_status_error(401), _status_error(400), _status_error(403), ValueError("x")])
def test_definitive_errors_are_not_retried(exc):
    """Chave invalida ou pedido malformado nao melhora tentando de novo -
    retentar so faz a pessoa esperar mais pelo mesmo erro."""
    assert is_transient_ai_error(exc) is False


def test_404_e_tratado_como_soluco_e_retentado():
    """Visto em producao (06/08/2026): duas tentativas seguidas da mesma categoria de
    noticias tomaram 404 enquanto as outras cinco do MESMO digest voltaram 200, e a
    reproducao minutos depois passou 6/6 com prompt identico. 404 aqui e roteamento do
    NIM engasgando, nao modelo inexistente - e sem retry derrubava a categoria inteira."""
    assert is_transient_ai_error(_status_error(404)) is True


def test_friendly_message_distinguishes_auth_from_transient():
    """O ponto da mudanca: antes toda causa virava a mesma frase generica."""
    auth = friendly_ai_error(_status_error(401))
    server = friendly_ai_error(_status_error(504))
    assert auth != server
    assert "chave" in auth.lower()
    assert "504" in server


def test_friendly_message_for_rate_limit_and_timeout():
    assert "limite" in friendly_ai_error(_status_error(429)).lower()
    assert "demorou" in friendly_ai_error(_timeout()).lower()


def test_friendly_message_never_leaks_the_exception_text():
    """A mensagem vai pro canal do Discord - nao pode vazar traceback nem chave."""
    exc = AuthenticationError(
        "Incorrect API key provided: nvapi-SEGREDO123",
        response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
        body=None,
    )
    assert "SEGREDO123" not in friendly_ai_error(exc)


def test_unknown_error_falls_back_to_generic_message():
    assert "Deu ruim" in friendly_ai_error(RuntimeError("algo inesperado"))
