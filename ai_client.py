import asyncio
import logging

from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from config import NVIDIA_API_KEY, MODEL, REFINEMENT_ROUNDS

log = logging.getLogger("hermes-bot")

client_ai = AsyncOpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_API_KEY)

# Uma resposta de IA de cada vez em todo o bot - evita respostas se atropelando
# quando varias pessoas usam comandos ao mesmo tempo.
ai_lock = asyncio.Lock()

# rascunho + REFINEMENT_ROUNDS refinos + humanizacao = REFINEMENT_ROUNDS + 2 chamadas
# sequenciais por resposta. Estava em 3 (5 chamadas), o que segurava o ai_lock global
# por muito tempo, fazia todo mundo esperar na fila e multiplicava a chance de pegar
# um 504 da NVIDIA no meio. Ajustavel por env (REFINEMENT_ROUNDS) se quiser trocar
# velocidade por polimento.

CRITIQUE_PROMPT = (
    "Releia sua resposta anterior com espirito critico, como se fosse outra pessoa "
    "revisando. Aponte pra si mesma: falhas de logica, coisas incertas apresentadas "
    "com confianca demais, floreio ou enrolacao desnecessaria, partes genericas demais. "
    "Depois reescreva uma versao melhor: mais precisa, mais direta, cortando o que "
    "sobrou. Responda somente com a nova versao da resposta, sem comentar o processo "
    "nem citar a critica."
)

HUMANIZE_PROMPT = (
    "Reescreva essa resposta final para soar como uma pessoa de verdade conversando "
    "no Discord, nao como um assistente robotico: cadencia natural, sem parecer "
    "checklist nem relatorio, mas sem perder a precisao, o tom direto e as opinioes "
    "que voce ja formou. Pode manter listas curtas se ajudar a clareza. Responda "
    "somente com o texto final, pronto para enviar."
)


# A API da NVIDIA da 504/timeout com alguma frequencia sob carga. Uma falha dessas
# derrubava a resposta inteira e a pessoa via so "deu ruim". Retry curto com backoff
# resolve a maioria; erro definitivo (chave invalida, modelo inexistente) nao e
# retentado, porque tentar de novo so faz esperar mais pelo mesmo erro.
AI_MAX_ATTEMPTS = 3
AI_RETRY_BASE_DELAY_SECONDS = 2


def is_transient_ai_error(exc: Exception) -> bool:
    """Se vale a pena tentar de novo. Funcao pura pra poder ser testada."""
    if isinstance(exc, AuthenticationError):
        return False
    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500 or exc.status_code == 429
    return False


def friendly_ai_error(exc: Exception) -> str:
    """Mensagem pro Discord que diz o que houve, sem vazar traceback nem chave.

    Antes era sempre "Deu ruim aqui consultando o modelo", pra qualquer causa -
    chave invalida, 504 e timeout ficavam indistinguiveis pra quem estava no canal
    e pro dono lendo depois.
    """
    if isinstance(exc, AuthenticationError):
        return "Minha chave da API foi recusada. Isso e coisa pro Rizu resolver, nao adianta tentar de novo."
    if isinstance(exc, RateLimitError):
        return "Bati no limite de requisicoes da API agora. Me da uns minutos e pergunta de novo."
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return "A API demorou demais pra responder e desisti. Tenta de novo em instantes."
    if isinstance(exc, APIStatusError):
        if exc.status_code >= 500:
            return f"A API da NVIDIA respondeu {exc.status_code} (problema do lado deles). Tenta de novo em instantes."
        return f"A API recusou o pedido ({exc.status_code}). Se persistir, e configuracao minha."
    return "Deu ruim aqui consultando o modelo, tenta de novo em instantes."


async def _complete(messages: list[dict], temperature: float, max_tokens: int = 800) -> str:
    last_error: Exception | None = None
    for attempt in range(AI_MAX_ATTEMPTS):
        try:
            completion = await client_ai.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            # A API as vezes retorna content=None (sem levantar erro) em vez de string vazia.
            return completion.choices[0].message.content or ""
        except Exception as exc:
            if not is_transient_ai_error(exc):
                raise
            last_error = exc
            if attempt < AI_MAX_ATTEMPTS - 1:
                delay = AI_RETRY_BASE_DELAY_SECONDS * (2**attempt)
                log.warning(
                    "Erro transitorio da API (%s), tentativa %s/%s, nova tentativa em %ss",
                    type(exc).__name__, attempt + 1, AI_MAX_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)

    raise last_error


async def _think_and_answer(base_messages: list[dict]) -> str:
    draft = await _complete(base_messages, temperature=0.6)

    for _ in range(REFINEMENT_ROUNDS):
        refine_messages = base_messages + [
            {"role": "assistant", "content": draft},
            {"role": "user", "content": CRITIQUE_PROMPT},
        ]
        draft = await _complete(refine_messages, temperature=0.5)

    final_messages = base_messages + [
        {"role": "assistant", "content": draft},
        {"role": "user", "content": HUMANIZE_PROMPT},
    ]
    return await _complete(final_messages, temperature=0.75)
