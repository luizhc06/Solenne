import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from ai_client import is_transient_ai_error, friendly_ai_error


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


@pytest.mark.parametrize("exc", [_status_error(401), _status_error(400), _status_error(404), ValueError("x")])
def test_definitive_errors_are_not_retried(exc):
    """Chave invalida ou modelo inexistente nao melhora tentando de novo -
    retentar so faz a pessoa esperar mais pelo mesmo erro."""
    assert is_transient_ai_error(exc) is False


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
