import time
import random
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import ALLOWED_GUILD_ID
from db import add_xp, get_user_level, get_rank_position, get_user_levels_count, get_leaderboard
from leveling_utils import xp_for_level

log = logging.getLogger("hermes-bot")

# Evita farm com "k", "kkk", emoji solto - mensagem precisa ter pelo menos isso de
# conteudo (apos strip) pra contar XP.
MIN_XP_MESSAGE_LENGTH = 3

# Cooldown de ganho por usuario, em memoria (nao no banco) - ver LevelingCog.last_xp_at.
# Igual ao padrao de PUNISH_COOLDOWN_SECONDS/MENTION_WINDOW_SECONDS em moderation.py: o
# banco so precisa ser tocado quando XP de fato e concedido (no maximo 1x/minuto/pessoa),
# nao a cada mensagem so pra checar o cooldown. Trade-off aceito: um restart do bot zera
# esse dict e a proxima mensagem de cada um pode conceder XP um pouco antes da hora -
# inofensivo, mesmo espirito do comentario sobre pop_due_reminders em db.py.
XP_COOLDOWN_SECONDS = 60

# Faixa de XP por mensagem (padrao tipo MEE6: da variacao e dificulta prever quando vai
# subir de nivel). So um patamar inicial - ajustavel depois de ver o volume real de
# mensagens do servidor (mesmo espirito do AI_CONCURRENCY_LIMIT em config.py).
XP_MIN = 15
XP_MAX = 25

PROGRESS_BAR_WIDTH = 8


def progress_bar(current: int, total: int, width: int = PROGRESS_BAR_WIDTH) -> str:
    """Barra de progresso em blocos unicode (▰ preenchido / ▱ vazio). Pura, pra testar
    sem Discord."""
    if total <= 0:
        filled = width
    else:
        filled = max(0, min(width, round(width * current / total)))
    return "▰" * filled + "▱" * (width - filled)


class LevelingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user_id -> time.monotonic() do ultimo ganho de XP concedido.
        self.last_xp_at: dict[int, float] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or message.guild.id != ALLOWED_GUILD_ID:
            return
        if len(message.content.strip()) < MIN_XP_MESSAGE_LENGTH:
            return

        user_id = message.author.id
        now = time.monotonic()
        ultimo = self.last_xp_at.get(user_id, 0.0)
        if now - ultimo < XP_COOLDOWN_SECONDS:
            return
        self.last_xp_at[user_id] = now

        amount = random.randint(XP_MIN, XP_MAX)
        loop = asyncio.get_event_loop()
        try:
            subiu, novo_nivel = await loop.run_in_executor(
                None, add_xp, user_id, message.author.display_name, amount
            )
        except Exception:
            log.exception("Erro ao conceder XP para %s", user_id)
            return

        if subiu:
            try:
                await message.channel.send(
                    f"🎉 {message.author.mention} subiu pro nivel {novo_nivel}!"
                )
            except discord.HTTPException:
                log.exception("Erro ao anunciar level-up de %s", user_id)

    @app_commands.command(name="rank", description="Mostra seu nivel e XP (ou de outra pessoa)")
    @app_commands.describe(usuario="De quem ver o rank (padrao: voce mesmo)")
    async def rank(self, interaction: discord.Interaction, usuario: discord.Member = None):
        alvo = usuario or interaction.user
        loop = asyncio.get_event_loop()
        dados = await loop.run_in_executor(None, get_user_level, alvo.id)
        if dados is None:
            proprio = alvo.id == interaction.user.id
            texto = (
                "Voce ainda nao tem XP registrado - manda uma mensagem no chat pra comecar."
                if proprio
                else f"{alvo.display_name} ainda nao tem XP registrado."
            )
            await interaction.response.send_message(texto, ephemeral=proprio)
            return

        posicao = await loop.run_in_executor(None, get_rank_position, alvo.id)
        total = await loop.run_in_executor(None, get_user_levels_count)

        nivel = dados["level"]
        xp_total = dados["xp"]
        piso_nivel = xp_for_level(nivel)
        teto_nivel = xp_for_level(nivel + 1)
        xp_no_nivel = xp_total - piso_nivel
        xp_necessario = teto_nivel - piso_nivel

        embed = discord.Embed(
            title=f"📊 Rank de {alvo.display_name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Nivel", value=str(nivel), inline=True)
        embed.add_field(name="Posicao", value=f"#{posicao} de {total}", inline=True)
        embed.add_field(name="XP total", value=str(xp_total), inline=True)
        embed.add_field(
            name=f"Progresso pro nivel {nivel + 1}",
            value=f"{progress_bar(xp_no_nivel, xp_necessario)}  `{xp_no_nivel}/{xp_necessario}`",
            inline=False,
        )
        embed.set_thumbnail(url=alvo.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Mostra o top 10 do servidor por XP")
    async def leaderboard(self, interaction: discord.Interaction):
        loop = asyncio.get_event_loop()
        top = await loop.run_in_executor(None, get_leaderboard, 10)
        if not top:
            await interaction.response.send_message(
                "Ninguem tem XP registrado ainda.", ephemeral=True
            )
            return

        medalhas = ["🥇", "🥈", "🥉"]
        linhas = []
        for i, u in enumerate(top):
            posicao = medalhas[i] if i < len(medalhas) else f"`#{i + 1}`"
            nome = u["display_name"] or f"Usuario {u['user_id']}"
            linhas.append(f"{posicao} **{nome}** — nivel {u['level']} ({u['xp']} XP)")

        embed = discord.Embed(
            title="🏆 Ranking do servidor",
            description="\n".join(linhas),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelingCog(bot))
