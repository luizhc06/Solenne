import asyncio
import logging
from collections import defaultdict

from db import get_user_summary, save_user_summary
from ai_client import _complete, ai_gate, TruncatedAIResponse
from utils import strip_mentions, truncate_words

log = logging.getLogger("hermes-bot")

# O prompt pede "no maximo 5 linhas", mas o modelo nem sempre obedece: o perfil do
# dono chegou a 25 linhas e 1726 chars (achado na verificacao geral de 20/09/2026),
# porque nada no CODIGO garantia o teto - so o pedido em texto. Esse resumo entra
# inteiro no system prompt de toda conversa da pessoa com ela, entao um perfil
# inchado dilui as instrucoes de persona a cada mensagem. MAX_SUMMARY_CHARS e
# backstop pra quando o modelo obedece as 5 linhas mas escreve linhas gigantes.
MAX_SUMMARY_LINES = 5
MAX_SUMMARY_CHARS = 500

# update_profile() le o resumo atual, espera uma chamada de IA (que pode levar
# varios segundos) e so entao grava - sem lock, duas tasks para o MESMO user_id
# (ex: uma mensagem de chat e um clique no botao de feedback quase juntos, ver
# cogs/chat.py e views.py) podiam ler o mesmo `current` desatualizado e a que
# terminasse por ultimo sobrescrevia a outra silenciosamente (lost update,
# achado do conselho de agentes, 19/08/2026). Um lock por user_id serializa
# leitura+IA+escrita: a segunda task so comeca a ler depois que a primeira
# terminou de gravar, entao ela ve o resumo ja atualizado como `current`.
_profile_locks: "defaultdict[int, asyncio.Lock]" = defaultdict(asyncio.Lock)

PROFILE_UPDATE_PROMPT = """Voce mantem um resumo curto (no maximo 5 linhas) sobre cada pessoa
que conversa com voce: fatos uteis e reais, preferencias, interesses, contexto recorrente,
coisas que a pessoa pediu explicitamente pra voce lembrar. Nunca inclua bobagem generica
nem repita a conversa toda - so o que for realmente util lembrar depois.

Resumo atual sobre {name}:
{current_summary}

Nova mensagem de {name}: {message}

Se a mensagem trouxer algo novo e util para lembrar sobre essa pessoa, atualize o resumo
(maximo 5 linhas, frases curtas e diretas). Se nao trouxer nada relevante, responda
EXATAMENTE com o resumo atual, sem mudar nada. Responda somente com o resumo atualizado,
sem comentarios nem explicacoes."""


def _cap_summary(text: str) -> str:
    """Aplica o teto de 5 linhas / 500 chars no CODIGO, independente do modelo obedecer
    o pedido em texto do PROFILE_UPDATE_PROMPT ou nao."""
    linhas = [l.strip() for l in (text or "").splitlines() if l.strip()]
    cortado = "\n".join(linhas[:MAX_SUMMARY_LINES])
    return truncate_words(cortado, MAX_SUMMARY_CHARS)


async def update_profile(user_id: int, name: str, message: str):
    """Disparada via asyncio.create_task() de dentro de ask_hermes() (cogs/chat.py) - por
    ser uma task solta, NAO herdava o ai_gate.interactive() do chamador (achado do
    conselho de agentes, 18/08/2026): com 20 pessoas conversando ao mesmo tempo, isso
    virava ate 20 chamadas de IA concorrentes e descontroladas, competindo pela mesma
    cota da API que o chat oficial respeita via fila. background() e o modo certo aqui -
    e trabalho de fundo de verdade, sem ninguem esperando na tela por ele."""
    loop = asyncio.get_event_loop()
    # Tira mencoes cruas ANTES de virar prompt: sem isso, "<@111> <@222>" solto vai pro
    # modelo como se fosse fato sobre a pessoa e as vezes volta gravado no resumo (achado
    # na verificacao geral de 20/09/2026 - mesma familia do bug do ping de grupo).
    message = strip_mentions(message)
    if not message:
        return
    async with _profile_locks[user_id]:
        current = await loop.run_in_executor(None, get_user_summary, user_id)
        prompt = PROFILE_UPDATE_PROMPT.format(name=name, current_summary=current or "(vazio ainda)", message=message)
        try:
            async with ai_gate.background():
                # max_tokens explicito: o prompt pede "no maximo 5 linhas", que cabe
                # folgado em 400. Antes usava o padrao de 800 do _complete e, em
                # 19/09/2026, uma geracao passou ate desse teto - ou seja, o modelo
                # ignorou o limite de 5 linhas e saiu discorrendo. Estourar aqui e sinal
                # de resposta fora do formato, nao de orcamento apertado.
                new_summary = await _complete(
                    [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=400
                )
        except TruncatedAIResponse:
            # Nao e erro de verdade, e condicao esperada: o resumo veio cortado no meio,
            # e gravar isso sujaria pra sempre o que ela "sabe" sobre a pessoa - esse
            # texto entra no system prompt de toda conversa dela com ela. Mantem o
            # resumo anterior, que estava inteiro. WARNING em vez de exception() porque
            # nao ha stack pra investigar: ja se sabe exatamente o que aconteceu.
            log.warning("Resumo de %s veio truncado, mantendo o perfil anterior", name)
            return
        except Exception:
            log.exception("Erro ao atualizar perfil de %s", name)
            return
        new_summary = _cap_summary(new_summary)
        if new_summary and new_summary != current:
            await loop.run_in_executor(None, save_user_summary, user_id, name, new_summary)


# asyncio so segura uma referencia FRACA a uma task criada com create_task() - se
# ninguem mais referenciar o objeto, o event loop pode coleta-la no meio da execucao,
# sem erro nenhum (achado do conselho de agentes, 18/08/2026; risco teorico, nunca
# confirmado em producao, mas barato de fechar). Este set guarda referencia forte
# ate a task terminar; discard() no callback de conclusao evita vazamento.
_background_tasks: set[asyncio.Task] = set()


def schedule_profile_update(user_id: int, name: str, message: str) -> asyncio.Task:
    """Dispara update_profile() em segundo plano, sem bloquear quem chamou, mas
    segurando referencia forte pra task nao sumir no meio (ver _background_tasks)."""
    task = asyncio.create_task(update_profile(user_id, name, message))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
