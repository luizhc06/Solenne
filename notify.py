import logging

import discord

from config import OWNER_USER_ID

log = logging.getLogger("hermes-bot")


async def notify_owner_text(bot: discord.Client, text: str):
    owner = bot.get_user(OWNER_USER_ID) or await bot.fetch_user(OWNER_USER_ID)
    if owner is None:
        log.error("Nao encontrei o usuario dono (OWNER_USER_ID) para notificar.")
        return
    try:
        await owner.send(text)
    except discord.Forbidden:
        log.error("Nao consegui mandar DM pro dono (DMs fechadas?).")
