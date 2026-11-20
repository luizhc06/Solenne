import re
import time
import logging
from datetime import timedelta, datetime, timezone
from collections import defaultdict, deque

import discord
from discord.ext import commands

from config import ALLOWED_GUILD_ID, OWNER_USER_ID

log = logging.getLogger("hermes-bot")

FLOOD_WINDOW_SECONDS = 5
# Afrouxado em nov/2026 (era 5 e 3): conversa animada batia no limite sem ser spam.
FLOOD_MAX_MESSAGES = 8
FLOOD_MAX_DUPLICATES = 5
TIMEOUT_SECONDS = 60
# Achado do conselho de 18/08/2026: ao contrario da regra de mencao (que ja avisa antes
# de punir), a regra 1 (muitas mensagens seguidas) apagava e aplicava timeout na hora -
# facil de bater organicamente numa reacao empolgada em sequencia ("kkkk", "mds", "top").
# Agora segue o mesmo padrao: primeira vez que bate o limiar, so avisa; se persistir
# dentro do cooldown, ai pune.
FLOOD_WARN_COOLDOWN_SECONDS = FLOOD_WINDOW_SECONDS * 4

# Nao punir de novo em seguida pelo mesmo motivo (separado da duracao do castigo).
PUNISH_COOLDOWN_SECONDS = 60

# Mensagem que marca alguem (pessoa ou cargo) nunca entra no automod. A regra de mencao
# castigava amigos que so estavam se marcando (nov/2026), e "@fulano" repetido tambem
# caia na regra de mensagem repetida. Marcar gente e conversa, nao spam.
MENTION_RE = re.compile(r"<@[!&]?\d+>")


class ModerationView(discord.ui.View):
    def __init__(self, guild: discord.Guild, member: discord.Member, reason: str, timeout_seconds: int = TIMEOUT_SECONDS):
        super().__init__(timeout=None)
        self.guild = guild
        self.member_id = member.id
        self.member_display = str(member)
        self.reason = reason
        self.timeout_seconds = timeout_seconds

    @discord.ui.button(label="Banir", style=discord.ButtonStyle.danger, emoji="🔨")
    async def ban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_USER_ID:
            await interaction.response.send_message("Essa decisao nao e sua.", ephemeral=True)
            return
        member = self.guild.get_member(self.member_id)
        try:
            if member:
                await self.guild.ban(member, reason=f"Aprovado por {interaction.user} via automod: {self.reason}")
            else:
                await self.guild.ban(discord.Object(id=self.member_id), reason=f"Aprovado por {interaction.user}")
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"✅ **{self.member_display}** foi banido.", view=self
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Nao tenho permissao de Banir Membros no servidor ainda. Adiciona essa permissao ao meu cargo.",
                ephemeral=True,
            )

    @discord.ui.button(label="Ignorar", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def dismiss_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_USER_ID:
            await interaction.response.send_message("Essa decisao nao e sua.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"↩️ Ignorado. **{self.member_display}** so ficou com o timeout de {self.timeout_seconds}s.",
            view=self,
        )


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, user_id) -> deque[(timestamp, message)]
        self.msg_log: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))
        self.recently_punished: dict[tuple[int, int], float] = {}
        # Quem ja foi avisado pela regra 1 (muitas mensagens seguidas) do check_flood.
        self.flood_warned: dict[tuple[int, int], float] = {}

    async def notify_owner(
        self, guild: discord.Guild, member: discord.Member, reason: str, sample: str, timeout_seconds: int = TIMEOUT_SECONDS
    ):
        owner = self.bot.get_user(OWNER_USER_ID) or await self.bot.fetch_user(OWNER_USER_ID)
        if owner is None:
            log.error("Nao encontrei o usuario dono (OWNER_USER_ID) para notificar.")
            return
        embed = discord.Embed(
            title="🚨 Flood detectado",
            description=(
                f"**Usuario:** {member.mention} (`{member}` / `{member.id}`)\n"
                f"**Motivo:** {reason}\n**Timeout aplicado:** {timeout_seconds}s"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        if sample:
            embed.add_field(name="Amostra", value=sample[:1000], inline=False)
        embed.set_footer(text=f"Servidor: {guild.name}")
        view = ModerationView(guild, member, reason, timeout_seconds)
        try:
            await owner.send(embed=embed, view=view)
        except discord.Forbidden:
            log.error("Nao consegui mandar DM pro dono (DMs fechadas?).")

    async def punish(
        self,
        message: discord.Message,
        reason: str,
        extra_msgs: list[discord.Message] | None = None,
        timeout_seconds: int = TIMEOUT_SECONDS,
    ):
        member = message.author
        guild = message.guild
        key = (guild.id, member.id)

        now = time.monotonic()
        if key in self.recently_punished and now - self.recently_punished[key] < PUNISH_COOLDOWN_SECONDS:
            return
        self.recently_punished[key] = now

        to_delete = list(extra_msgs) if extra_msgs else [message]
        sample_lines = []
        for m in to_delete:
            sample_lines.append(m.content[:120])
            try:
                await m.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        try:
            await member.timeout(timedelta(seconds=timeout_seconds), reason=f"Automod: {reason}")
        except discord.Forbidden:
            log.error("Sem permissao de Moderate Members para dar timeout.")
        except discord.HTTPException:
            log.exception("Falha ao aplicar timeout")

        await self.notify_owner(
            guild, member, reason, "\n".join(sample_lines), timeout_seconds=timeout_seconds
        )

    async def check_flood(self, message: discord.Message):
        if message.guild is None or message.guild.id != ALLOWED_GUILD_ID:
            return
        member = message.author
        if member.bot or member.id == OWNER_USER_ID or member.guild_permissions.administrator:
            return
        if MENTION_RE.search(message.content or ""):
            return

        key = (message.guild.id, member.id)
        log_deque = self.msg_log[key]
        now = time.monotonic()
        log_deque.append((now, message))

        # limpa entradas fora da janela
        while log_deque and now - log_deque[0][0] > FLOOD_WINDOW_SECONDS:
            log_deque.popleft()

        # regra 1: muitas mensagens seguidas — avisa antes de punir (ver
        # FLOOD_WARN_COOLDOWN_SECONDS), mesmo padrao ja usado pra mencao.
        if len(log_deque) >= FLOOD_MAX_MESSAGES:
            recent_msgs = [m for _, m in log_deque]
            chave = (message.guild.id, member.id)
            ultimo_aviso = self.flood_warned.get(chave, 0.0)
            if now - ultimo_aviso >= FLOOD_WARN_COOLDOWN_SECONDS:
                self.flood_warned[chave] = now
                # Nada e apagado aqui de proposito: o aviso e pra dar chance de segurar o ritmo.
                await message.reply(
                    f"Opa, {member.display_name}, calma no ritmo — muita mensagem seguida "
                    f"em pouco tempo. Se continuar assim eu vou ter que te dar um tempo.",
                    mention_author=False,
                )
                return
            await self.punish(message, f"{len(recent_msgs)} mensagens em {FLOOD_WINDOW_SECONDS}s", recent_msgs)
            return

        # regra 2: mensagem repetida (mesmo conteudo)
        contents = [m.content for _, m in log_deque if m.content]
        if contents:
            last = contents[-1]
            repeats = [m for _, m in log_deque if m.content == last]
            if len(repeats) >= FLOOD_MAX_DUPLICATES:
                await self.punish(message, "mensagens repetidas (spam)", repeats)
                return


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        await self.check_flood(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
