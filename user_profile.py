import asyncio
import logging

from db import get_user_summary, save_user_summary
from ai_client import _complete

log = logging.getLogger("hermes-bot")

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


async def update_profile(user_id: int, name: str, message: str):
    loop = asyncio.get_event_loop()
    current = await loop.run_in_executor(None, get_user_summary, user_id)
    prompt = PROFILE_UPDATE_PROMPT.format(name=name, current_summary=current or "(vazio ainda)", message=message)
    try:
        new_summary = await _complete([{"role": "user", "content": prompt}], temperature=0.3)
    except Exception:
        log.exception("Erro ao atualizar perfil de %s", name)
        return
    new_summary = new_summary.strip()
    if new_summary and new_summary != current:
        await loop.run_in_executor(None, save_user_summary, user_id, name, new_summary)
