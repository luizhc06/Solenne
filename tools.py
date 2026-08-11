"""Registro das ferramentas que a Solenne pode decidir usar sozinha.

Existe como modulo separado por causa de import circular: os cogs importam
`ai_client` pra falar com o modelo, entao `ai_client` nao pode importar os cogs de
volta pra descobrir o que eles sabem fazer. Aqui ninguem importa cog nenhum - os cogs
e que se registram na hora em que sao carregados, e o `ai_client` so le o registro.

Antes disso a Solenne nao DECIDIA nada: existia um regex procurando a palavra
"pesquisa" na mensagem (`wants_web_search`), e qualquer outra capacidade so era
acessivel por comando de barra. Ou seja, ela sabia consultar clima mas nunca consultava
sozinha quando alguem perguntava se ia chover.
"""
import json
import asyncio
import logging
import contextlib
import contextvars
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord

log = logging.getLogger("hermes-bot")

# Teto por ferramenta. Sem isso, uma busca pendurada segura o portao de IA e a pessoa
# fica olhando o "Pensando..." ate desistir - o mesmo sintoma que ja foi reclamado.
TOOL_TIMEOUT_SECONDS = 30


@dataclass
class ToolResult:
    """O que a ferramenta devolve.

    `content` e o que vai pro modelo (texto puro, geralmente JSON). `embed` e opcional
    e vai pro Discord junto da resposta - e assim que as fontes da pesquisa continuam
    aparecendo pra quem le, mesmo com a sintese sendo feita pelo proprio modelo.
    """

    content: str
    embed: discord.Embed | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[ToolResult]]


_REGISTRY: dict[str, Tool] = {}


@dataclass
class ToolContext:
    """Quem esta falando e onde, na requisicao em andamento.

    Existe pra que dados de identidade NUNCA venham do modelo. Se `criar_lembrete`
    recebesse o user_id como parametro, bastaria alguem pedir "cria um lembrete pro
    fulano" (ou o modelo se confundir) pra escrever no nome de outra pessoa. Assim a
    ferramenta so consegue agir por quem realmente mandou a mensagem.
    """

    author_id: int
    author_name: str
    channel_id: int


_CONTEXT: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar(
    "solenne_tool_context", default=None
)


@contextlib.contextmanager
def use_context(context: ToolContext):
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> ToolContext | None:
    return _CONTEXT.get()


def register(name: str, description: str, parameters: dict):
    """Decorator que registra a funcao como ferramenta disponivel pro modelo."""

    def decorator(handler):
        if name in _REGISTRY:
            log.warning("Ferramenta %s registrada duas vezes, sobrescrevendo", name)
        _REGISTRY[name] = Tool(name, description, parameters, handler)
        return handler

    return decorator


def tool_specs() -> list[dict]:
    """As ferramentas no formato que a API espera (schema de function calling)."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in _REGISTRY.values()
    ]


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


async def execute_tool(name: str, arguments_json: str) -> ToolResult:
    """Roda a ferramenta pedida, transformando qualquer falha em texto pro modelo.

    Nunca levanta: um erro aqui deve virar contexto que o modelo consegue usar ("a
    busca falhou, responde com o que voce sabe e avisa") em vez de derrubar a resposta
    inteira. O modelo lida bem com isso; a pessoa no canal nao lida com silencio.
    """
    tool = _REGISTRY.get(name)
    if tool is None:
        log.warning("Modelo pediu ferramenta inexistente: %s", name)
        return ToolResult(json.dumps({"erro": f"ferramenta '{name}' nao existe"}))

    try:
        argumentos = json.loads(arguments_json or "{}")
        if not isinstance(argumentos, dict):
            raise ValueError("argumentos nao sao um objeto")
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("Argumentos invalidos para %s: %r", name, arguments_json)
        return ToolResult(json.dumps({"erro": f"argumentos invalidos: {exc}"}))

    try:
        return await asyncio.wait_for(tool.handler(**argumentos), timeout=TOOL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log.warning("Ferramenta %s estourou %ss", name, TOOL_TIMEOUT_SECONDS)
        return ToolResult(json.dumps({"erro": "a ferramenta demorou demais e foi cancelada"}))
    except TypeError as exc:
        # Modelo inventou/esqueceu um parametro - avisa qual, pra ele poder corrigir.
        log.warning("Parametros errados para %s: %s", name, exc)
        return ToolResult(json.dumps({"erro": f"parametros errados: {exc}"}))
    except Exception as exc:
        log.exception("Erro ao executar a ferramenta %s", name)
        return ToolResult(json.dumps({"erro": f"{type(exc).__name__}: {str(exc)[:200]}"}))
