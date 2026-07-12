import asyncio
import logging

from openai import AsyncOpenAI

from config import NVIDIA_API_KEY, MODEL

log = logging.getLogger("hermes-bot")

client_ai = AsyncOpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_API_KEY)

# Uma resposta de IA de cada vez em todo o bot - evita respostas se atropelando
# quando varias pessoas usam comandos ao mesmo tempo.
ai_lock = asyncio.Lock()

REFINEMENT_ROUNDS = 3  # rascunho + N refinos + humanizacao = 5 passadas no total

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


async def _complete(messages: list[dict], temperature: float, max_tokens: int = 800) -> str:
    completion = await client_ai.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    # A API as vezes retorna content=None (sem levantar erro) em vez de string vazia.
    return completion.choices[0].message.content or ""


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
