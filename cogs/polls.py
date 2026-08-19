import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import OWNER_USER_ID
from db import (
    create_poll,
    set_poll_message_id,
    cast_vote,
    get_poll,
    get_poll_results,
    get_open_polls,
    get_expired_polls,
    close_poll,
)

log = logging.getLogger("hermes-bot")

POLL_CHECK_SECONDS = 30  # mesmo intervalo do REMINDER_CHECK_SECONDS (cogs/reminders.py)
MIN_OPTIONS = 2
MAX_OPTIONS = 5
POLL_MAX_DURATION_MINUTES = 60 * 24 * 30  # 30 dias
OPTION_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

# Anti-clique-nervoso: cooldown pequeno em memoria por (poll_id, user_id), mesmo padrao
# de recently_punished/mention_warned em cogs/moderation.py - nao precisa tocar o banco
# pra isso.
VOTE_CLICK_COOLDOWN_SECONDS = 3

# Debounce de edicao da mensagem de resultado: se muita gente votar ao mesmo tempo,
# editar a cada voto pode estourar o rate limit de edicao de mensagem do Discord (~5
# req/5s por canal). Em vez de editar a cada clique, no maximo 1 edicao a cada ~2s por
# enquete (mesmo espirito do comentario sobre AI_CONCURRENCY_LIMIT em config.py).
RESULT_EDIT_DEBOUNCE_SECONDS = 2


def collect_options(opcao1, opcao2, opcao3, opcao4, opcao5) -> list[str]:
    """Junta as opcoes preenchidas (ignora vazias/so espaco), na ordem dada, ate o
    limite de MAX_OPTIONS - o que sobra de opcao5..opcao3 se a pessoa deixar vazio."""
    brutas = [opcao1, opcao2, opcao3, opcao4, opcao5]
    return [o.strip()[:100] for o in brutas if o and o.strip()][:MAX_OPTIONS]


def can_close_poll(user_id: int, creator_id: int, owner_id: int = OWNER_USER_ID) -> bool:
    """So quem criou a enquete ou o dono do bot pode encerra-la manualmente."""
    return user_id == creator_id or user_id == owner_id


def progress_bar(count: int, total: int, width: int = 10) -> str:
    filled = round(width * count / total) if total > 0 else 0
    return "█" * filled + "░" * (width - filled)


def build_results_embed(poll: dict, results: dict[int, int], closed: bool = False) -> discord.Embed:
    """Embed de resultado - so contagem/barra por opcao, nunca lista de quem votou em
    que (resultado anonimo por padrao, ver anonimo=True no /enquete)."""
    options = poll["options"]
    total = sum(results.get(i, 0) for i in range(len(options)))

    embed = discord.Embed(
        title="🗳️ Enquete encerrada" if closed else "🗳️ Enquete",
        description=f"**{poll['question']}**",
        color=discord.Color.dark_grey() if closed else discord.Color.blurple(),
    )
    for i, option in enumerate(options):
        count = results.get(i, 0)
        pct = (count / total * 100) if total else 0
        emoji = OPTION_EMOJIS[i] if i < len(OPTION_EMOJIS) else "•"
        embed.add_field(
            name=f"{emoji} {option}",
            value=f"{progress_bar(count, total)} `{count}` voto(s) ({pct:.0f}%)",
            inline=False,
        )

    footer = f"{total} voto(s) no total"
    if not closed:
        footer += (
            " • encerra automaticamente no horario marcado"
            if poll.get("closes_at")
            else " • sem prazo, use /encerrarenquete pra fechar"
        )
    embed.set_footer(text=footer)
    return embed


class PollButton(discord.ui.Button):
    def __init__(self, poll_id: int, option_index: int, label: str):
        super().__init__(
            label=label[:80],
            style=discord.ButtonStyle.primary,
            # custom_id fixo (nao gerado a toa): a view precisa sobreviver a restart
            # do bot, e discord.py so volta a rotear clique pra ela se o custom_id
            # bater com o que foi reregistrado via bot.add_view (ver PollsCog.cog_load).
            custom_id=f"poll:{poll_id}:{option_index}",
            emoji=OPTION_EMOJIS[option_index] if option_index < len(OPTION_EMOJIS) else None,
        )
        self.option_index = option_index

    async def callback(self, interaction: discord.Interaction):
        view: PollView = self.view
        await view.cog.handle_vote(interaction, view.poll_id, self.option_index)


class PollView(discord.ui.View):
    """timeout=None (igual ModerationView) - enquete sem prazo fica valendo ate alguem
    encerrar, entao a view nao pode expirar sozinha."""

    def __init__(self, poll_id: int, options: list[str], cog: "PollsCog"):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.cog = cog
        for idx, option in enumerate(options[:MAX_OPTIONS]):
            self.add_item(PollButton(poll_id, idx, option))


class PollsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (poll_id, user_id) -> ultimo clique (time.monotonic())
        self.last_vote_click: dict[tuple[int, int], float] = {}
        # poll_ids com uma edicao de resultado ja agendada (debounce - ver
        # schedule_result_update).
        self._pending_result_update: set[int] = set()

    async def cog_load(self):
        # Reregistra a view de cada enquete ainda aberta pra sobreviver a um restart
        # do bot - sem isso, os botoes ficam "mudos" apos reiniciar (mesmo espirito do
        # before_loop + wait_until_ready ja usado em reminders.py/core.py, so que aqui
        # o que precisa "esperar o bot subir" e o registro da view, nao um loop).
        await self._register_open_poll_views()
        self.check_polls_task.start()

    def cog_unload(self):
        self.check_polls_task.cancel()

    async def _register_open_poll_views(self):
        loop = asyncio.get_event_loop()
        try:
            open_polls = await loop.run_in_executor(None, get_open_polls)
        except Exception:
            log.exception("Erro ao buscar enquetes abertas pra reregistrar as views")
            return
        for poll in open_polls:
            view = PollView(poll["id"], poll["options"], self)
            if poll["message_id"] is not None:
                self.bot.add_view(view, message_id=poll["message_id"])
            else:
                self.bot.add_view(view)

    # -----------------------------------------------------------------------------
    # Voto
    # -----------------------------------------------------------------------------

    async def handle_vote(self, interaction: discord.Interaction, poll_id: int, option_index: int):
        loop = asyncio.get_event_loop()
        poll = await loop.run_in_executor(None, get_poll, poll_id)
        if poll is None or poll["closed"]:
            await interaction.response.send_message("Essa enquete ja foi encerrada.", ephemeral=True)
            return

        key = (poll_id, interaction.user.id)
        now = time.monotonic()
        last_click = self.last_vote_click.get(key, 0.0)
        if now - last_click < VOTE_CLICK_COOLDOWN_SECONDS:
            await interaction.response.send_message(
                "Calma, espera um pouquinho antes de trocar de novo.", ephemeral=True
            )
            return
        self.last_vote_click[key] = now

        is_new = await loop.run_in_executor(None, cast_vote, poll_id, interaction.user.id, option_index)
        opcao = poll["options"][option_index]
        texto = f"Voto em **{opcao}** registrado!" if is_new else f"Voto atualizado pra **{opcao}**!"
        await interaction.response.send_message(texto, ephemeral=True)

        await self.schedule_result_update(poll_id)

    async def schedule_result_update(self, poll_id: int):
        if poll_id in self._pending_result_update:
            return
        self._pending_result_update.add(poll_id)
        asyncio.create_task(self._debounced_result_update(poll_id))

    async def _debounced_result_update(self, poll_id: int):
        try:
            await asyncio.sleep(RESULT_EDIT_DEBOUNCE_SECONDS)
        finally:
            # Libera ANTES de editar: se um voto novo chegar durante a propria edicao,
            # ele agenda outra rodada em vez de ficar esperando esta terminar.
            self._pending_result_update.discard(poll_id)
        await self.refresh_results_message(poll_id)

    async def refresh_results_message(self, poll_id: int):
        loop = asyncio.get_event_loop()
        poll = await loop.run_in_executor(None, get_poll, poll_id)
        if poll is None or poll["message_id"] is None or poll["closed"]:
            return
        channel = self.bot.get_channel(poll["channel_id"])
        if channel is None:
            return
        results = await loop.run_in_executor(None, get_poll_results, poll_id)
        embed = build_results_embed(poll, results, closed=False)
        try:
            message = channel.get_partial_message(poll["message_id"])
            await message.edit(embed=embed)
        except discord.HTTPException:
            log.exception("Erro ao atualizar resultado da enquete %s", poll_id)

    # -----------------------------------------------------------------------------
    # Encerramento
    # -----------------------------------------------------------------------------

    async def _close_and_announce(self, poll: dict):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, close_poll, poll["id"])
        results = await loop.run_in_executor(None, get_poll_results, poll["id"])
        embed = build_results_embed(poll, results, closed=True)

        if poll["message_id"] is None:
            return
        channel = self.bot.get_channel(poll["channel_id"])
        if channel is None:
            return
        try:
            message = await channel.fetch_message(poll["message_id"])
        except discord.HTTPException:
            log.warning("Nao achei a mensagem da enquete %s pra encerrar.", poll["id"])
            return

        view = PollView(poll["id"], poll["options"], self)
        for child in view.children:
            child.disabled = True
        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Erro ao editar mensagem de resultado final da enquete %s", poll["id"])

    @tasks.loop(seconds=POLL_CHECK_SECONDS)
    async def check_polls_task(self):
        loop = asyncio.get_event_loop()
        try:
            expired = await loop.run_in_executor(None, get_expired_polls)
        except Exception:
            log.exception("Erro ao buscar enquetes vencidas")
            return
        for poll in expired:
            await self._close_and_announce(poll)

    @check_polls_task.before_loop
    async def before_check_polls_task(self):
        await self.bot.wait_until_ready()

    # -----------------------------------------------------------------------------
    # Comandos
    # -----------------------------------------------------------------------------

    @app_commands.command(name="enquete", description="Cria uma enquete com botoes (ate 5 opcoes)")
    @app_commands.describe(
        pergunta="A pergunta da enquete",
        opcao1="Primeira opcao",
        opcao2="Segunda opcao",
        opcao3="Terceira opcao (opcional)",
        opcao4="Quarta opcao (opcional)",
        opcao5="Quinta opcao (opcional)",
        duracao_minutos="Encerra sozinha depois de X minutos (deixe vazio pra so fechar via /encerrarenquete)",
        anonimo="Reservado pra uma versao futura que mostra quem votou em que - por enquanto o resultado e sempre so a contagem",
    )
    async def enquete(
        self,
        interaction: discord.Interaction,
        pergunta: str,
        opcao1: str,
        opcao2: str,
        opcao3: str = None,
        opcao4: str = None,
        opcao5: str = None,
        duracao_minutos: int = None,
        anonimo: bool = True,
    ):
        options = collect_options(opcao1, opcao2, opcao3, opcao4, opcao5)
        if len(options) < MIN_OPTIONS:
            await interaction.response.send_message(
                f"Preciso de pelo menos {MIN_OPTIONS} opcoes diferentes.", ephemeral=True
            )
            return
        if len(set(options)) != len(options):
            await interaction.response.send_message(
                "As opcoes precisam ser diferentes entre si.", ephemeral=True
            )
            return
        if duracao_minutos is not None and not (1 <= duracao_minutos <= POLL_MAX_DURATION_MINUTES):
            await interaction.response.send_message(
                f"Duracao precisa ser entre 1 e {POLL_MAX_DURATION_MINUTES} minutos.", ephemeral=True
            )
            return

        closes_at = (
            datetime.now(timezone.utc) + timedelta(minutes=duracao_minutos) if duracao_minutos else None
        )

        loop = asyncio.get_event_loop()
        poll_id = await loop.run_in_executor(
            None,
            create_poll,
            interaction.channel_id,
            interaction.user.id,
            pergunta[:250],
            options,
            anonimo,
            closes_at,
        )
        poll = await loop.run_in_executor(None, get_poll, poll_id)
        embed = build_results_embed(poll, {}, closed=False)
        view = PollView(poll_id, options, self)

        await interaction.response.send_message(embed=embed, view=view)
        sent = await interaction.original_response()
        await loop.run_in_executor(None, set_poll_message_id, poll_id, sent.id)

    @app_commands.command(name="encerrarenquete", description="Encerra uma enquete manualmente e mostra o resultado final")
    @app_commands.describe(id="O numero da enquete (mostrado no rodape ao criar)")
    async def encerrarenquete(self, interaction: discord.Interaction, id: int):
        loop = asyncio.get_event_loop()
        poll = await loop.run_in_executor(None, get_poll, id)
        if poll is None:
            await interaction.response.send_message(f"Nao achei a enquete `#{id}`.", ephemeral=True)
            return
        if poll["closed"]:
            await interaction.response.send_message(f"A enquete `#{id}` ja estava encerrada.", ephemeral=True)
            return
        if not can_close_poll(interaction.user.id, poll["creator_id"]):
            await interaction.response.send_message(
                "So quem criou essa enquete (ou o dono do bot) pode encerra-la.", ephemeral=True
            )
            return

        await self._close_and_announce(poll)
        await interaction.response.send_message(f"✅ Enquete `#{id}` encerrada.", ephemeral=True)

    @app_commands.command(name="enquetes", description="Lista as enquetes abertas no momento")
    async def enquetes(self, interaction: discord.Interaction):
        loop = asyncio.get_event_loop()
        open_polls = await loop.run_in_executor(None, get_open_polls)
        if not open_polls:
            await interaction.response.send_message("Nenhuma enquete aberta no momento.", ephemeral=True)
            return
        linhas = "\n".join(f"`#{p['id']}` — **{p['question'][:100]}**" for p in open_polls)
        embed = discord.Embed(
            title="🗳️ Enquetes abertas", description=linhas[:4000], color=discord.Color.blurple()
        )
        embed.set_footer(text="Pra encerrar: /encerrarenquete <id>")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PollsCog(bot))
