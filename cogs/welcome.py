import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from config import ALLOWED_GUILD_ID, WELCOME_CHANNEL_ID
from db import mark_welcomed, was_recently_welcomed

log = logging.getLogger("hermes-bot")

# Janela de dedupe do gateway reentregando on_member_join apos reconexao (ver
# db.was_recently_welcomed) - nao tem a ver com detectar rejoin de verdade.
WELCOME_DEDUPE_WINDOW_SECONDS = 60

# Conta com menos tempo de existencia que isso ganha um aviso visual na embed (sinal
# classico de raid/bot pra moderacao notar) - sem nenhuma acao automatica, so
# enriquecimento de informacao. Decisao de banir/expulsar continua manual via /kick.
NEW_ACCOUNT_THRESHOLD = timedelta(hours=24)

WELCOME_MESSAGE_TEMPLATE = (
    "Seja bem-vindo(a) ao **{servidor}**, {mention}! Fica a vontade pra dar um oi por aqui."
)


def _format_account_age(delta: timedelta) -> str:
    """Formata a idade da conta de forma legivel ('12min', '3h', '2 dias'). Pura, pra
    poder ser testada sem depender de datetime.now()."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{max(total_seconds, 0)}s"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days} dia{'s' if days != 1 else ''}"


def resolve_welcome_channel(
    guild: discord.Guild, welcome_channel_id: int | None
) -> discord.abc.Messageable | None:
    """Resolve o canal de destino: WELCOME_CHANNEL_ID -> system_channel -> None.

    Cai pro fallback tanto se o canal configurado nao existir mais quanto se a Solenne
    nao tiver permissao de mandar mensagem nele. Pura o suficiente pra testar com um
    guild falso (so precisa de get_channel/system_channel/me)."""
    channel = None
    if welcome_channel_id:
        channel = guild.get_channel(welcome_channel_id)
        if channel is not None:
            me = guild.me
            if me is not None and not channel.permissions_for(me).send_messages:
                channel = None
    if channel is None:
        channel = guild.system_channel
    return channel


def build_welcome_embed(member: discord.Member, now: datetime | None = None) -> discord.Embed:
    """Monta a embed de boas-vindas. Recebe `now` pra ser testavel sem relogio real."""
    now = now or datetime.now(timezone.utc)
    account_age = now - member.created_at

    embed = discord.Embed(
        title=f"Bem-vindo(a), {member.display_name}!",
        description=WELCOME_MESSAGE_TEMPLATE.format(
            mention=member.mention, servidor=member.guild.name
        ),
        color=discord.Color.blurple(),
        timestamp=now,
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if account_age < NEW_ACCOUNT_THRESHOLD:
        embed.add_field(
            name="⚠️ Conta nova",
            value=f"Criada ha {_format_account_age(account_age)}",
            inline=False,
        )

    embed.set_footer(
        text=(
            f"Membro #{member.guild.member_count} • "
            f"conta criada em {member.created_at.strftime('%d/%m/%Y')}"
        )
    )
    return embed


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != ALLOWED_GUILD_ID:
            return
        if member.bot:
            return

        loop = asyncio.get_event_loop()
        try:
            already_welcomed = await loop.run_in_executor(
                None, was_recently_welcomed, member.guild.id, member.id, WELCOME_DEDUPE_WINDOW_SECONDS
            )
        except Exception:
            log.exception("Erro ao checar dedupe de boas-vindas para %s", member.id)
            already_welcomed = False
        if already_welcomed:
            return

        channel = resolve_welcome_channel(member.guild, WELCOME_CHANNEL_ID)
        if channel is None:
            log.warning(
                "Sem canal de boas-vindas (WELCOME_CHANNEL_ID) nem system_channel em %s (%s) - ignorando.",
                member.guild.name,
                member.guild.id,
            )
            return

        embed = build_welcome_embed(member)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.error("Sem permissao para mandar mensagem de boas-vindas no canal %s.", channel.id)
            return
        except discord.HTTPException:
            log.exception("Erro ao mandar mensagem de boas-vindas para %s", member.id)
            return

        try:
            await loop.run_in_executor(None, mark_welcomed, member.guild.id, member.id)
        except Exception:
            log.exception("Erro ao marcar boas-vindas de %s no banco", member.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
