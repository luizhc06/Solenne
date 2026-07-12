import time
import logging
from datetime import timedelta, datetime, timezone
from collections import defaultdict, deque

import discord
from discord.ext import commands

from config import ALLOWED_GUILD_ID, OWNER_USER_ID

log = logging.getLogger("hermes-bot")

FLOOD_WINDOW_SECONDS = 5
FLOOD_MAX_MESSAGES = 5
FLOOD_MAX_DUPLICATES = 3
FLOOD_MAX_MENTIONS = 5
TIMEOUT_SECONDS = 60


class ModerationView(discord.ui.View):
    def __init__(self, guild: discord.Guild, member: discord.Member, reason: str):
        super().__init__(timeout=None)
        self.guild = guild
        self.member_id = member.id
        self.member_display = str(member)
        self.reason = reason

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
            content=f"↩️ Ignorado. **{self.member_display}** so ficou com o timeout de {TIMEOUT_SECONDS}s.",
            view=self,
        )


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, user_id) -> deque[(timestamp, message)]
        self.msg_log: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))
        self.recently_punished: dict[tuple[int, int], float] = {}

    async def notify_owner(self, guild: discord.Guild, member: discord.Member, reason: str, sample: str):
        owner = self.bot.get_user(OWNER_USER_ID) or await self.bot.fetch_user(OWNER_USER_ID)
        if owner is None:
            log.error("Nao encontrei o usuario dono (OWNER_USER_ID) para notificar.")
            return
        embed = discord.Embed(
            title="🚨 Flood detectado",
            description=f"**Usuario:** {member.mention} (`{member}` / `{member.id}`)\n**Motivo:** {reason}",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        if sample:
            embed.add_field(name="Amostra", value=sample[:1000], inline=False)
        embed.set_footer(text=f"Servidor: {guild.name}")
        view = ModerationView(guild, member, reason)
        try:
            await owner.send(embed=embed, view=view)
        except discord.Forbidden:
            log.error("Nao consegui mandar DM pro dono (DMs fechadas?).")

    async def punish(self, message: discord.Message, reason: str, extra_msgs: list[discord.Message] | None = None):
        member = message.author
        guild = message.guild
        key = (guild.id, member.id)

        now = time.monotonic()
        if key in self.recently_punished and now - self.recently_punished[key] < TIMEOUT_SECONDS:
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
            await member.timeout(timedelta(seconds=TIMEOUT_SECONDS), reason=f"Automod: {reason}")
        except discord.Forbidden:
            log.error("Sem permissao de Moderate Members para dar timeout.")
        except discord.HTTPException:
            log.exception("Falha ao aplicar timeout")

        await self.notify_owner(guild, member, reason, "\n".join(sample_lines))

    async def check_flood(self, message: discord.Message):
        if message.guild is None or message.guild.id != ALLOWED_GUILD_ID:
            return
        member = message.author
        if member.bot or member.id == OWNER_USER_ID or member.guild_permissions.administrator:
            return

        key = (message.guild.id, member.id)
        log_deque = self.msg_log[key]
        now = time.monotonic()
        log_deque.append((now, message))

        # limpa entradas fora da janela
        while log_deque and now - log_deque[0][0] > FLOOD_WINDOW_SECONDS:
            log_deque.popleft()

        # regra 1: muitas mensagens seguidas
        if len(log_deque) >= FLOOD_MAX_MESSAGES:
            recent_msgs = [m for _, m in log_deque]
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

        # regra 3: spam de mencoes (raid-like)
        if len(message.mentions) + len(message.role_mentions) >= FLOOD_MAX_MENTIONS:
            await self.punish(message, "spam de mencoes", [message])
            return

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        await self.check_flood(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
