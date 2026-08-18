import re
import time
import logging
from datetime import timedelta, datetime, timezone
from collections import Counter, defaultdict, deque

import discord
from discord.ext import commands

from config import ALLOWED_GUILD_ID, OWNER_USER_ID

log = logging.getLogger("hermes-bot")

FLOOD_WINDOW_SECONDS = 5
FLOOD_MAX_MESSAGES = 5
FLOOD_MAX_DUPLICATES = 3
TIMEOUT_SECONDS = 60
# Achado do conselho de 18/08/2026: ao contrario da regra de mencao (que ja avisa antes
# de punir), a regra 1 (muitas mensagens seguidas) apagava e aplicava timeout na hora -
# facil de bater organicamente numa reacao empolgada em sequencia ("kkkk", "mds", "top").
# Agora segue o mesmo padrao: primeira vez que bate o limiar, so avisa; se persistir
# dentro do cooldown, ai pune.
FLOOD_WARN_COOLDOWN_SECONDS = FLOOD_WINDOW_SECONDS * 4

# ---------------------------------------------------------------------------------
# Mencoes
#
# A regra antiga era "5 ou mais mencoes numa mensagem = spam", e apagava na hora, com
# timeout e DM pro dono com botao de banir. Marcar 5 amigos DIFERENTES numa mensagem e
# conversa normal, e foi exatamente isso que aconteceu em producao (ago/2026): mensagem
# legitima apagada, sem aviso nenhum.
#
# O que incomoda de verdade nao e mencionar MUITA gente, e mencionar a MESMA pessoa
# repetidamente. Entao a contagem agora e por alvo, com aviso antes da punicao.
# ---------------------------------------------------------------------------------

# Janela pra considerar que as mencoes ao mesmo alvo sao a mesma "sessao" de insistencia.
MENTION_WINDOW_SECONDS = 60
# Na 2a mensagem seguida marcando a mesma pessoa ela avisa; na 3a, castigo.
MENTION_WARN_AT = 2
MENTION_PUNISH_AT = 3
MENTION_TIMEOUT_SECONDS = 10 * 60
# Repetir o MESMO @ varias vezes dentro de uma unica mensagem (@rizu @rizu @rizu @rizu).
MENTION_SAME_TARGET_IN_ONE_MSG = 4
# Muita gente DIFERENTE de uma vez so continua sendo tratado como raid, mas num patamar
# em que nao da pra confundir com "marquei a galera": 5 amigos e conversa, 10 e ataque.
MASS_MENTION_DISTINCT = 10
# Nao punir de novo em seguida pelo mesmo motivo (separado da duracao do castigo).
PUNISH_COOLDOWN_SECONDS = 60

# So conta mencao escrita no TEXTO. message.mentions inclui o autor da mensagem
# respondida quando o reply pinga - contar isso faria conversa normal de reply virar
# flood de mencao, que e o falso positivo mais facil de cometer aqui.
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")


def mention_counts(content: str) -> Counter:
    """Quantas vezes cada alvo foi marcado NO TEXTO da mensagem.

    Devolve Counter porque a repeticao importa: o payload do Discord deduplica
    message.mentions, entao "@rizu @rizu @rizu" chegaria como um alvo so por ali.
    """
    alvos = USER_MENTION_RE.findall(content or "")
    alvos += [f"role:{r}" for r in ROLE_MENTION_RE.findall(content or "")]
    return Counter(alvos)


def mention_verdict(counts: Counter, historico: dict[str, int]) -> tuple[str | None, str]:
    """Decide o que fazer com as mencoes de uma mensagem. Pura, pra poder ser testada.

    `historico` e quantas mensagens recentes (dentro da janela) ja marcaram cada alvo,
    incluindo esta. Devolve (acao, motivo), com acao em None / "avisar" / "punir".
    """
    if not counts:
        return None, ""

    repetido = max(counts.values())
    if repetido >= MENTION_SAME_TARGET_IN_ONE_MSG:
        return "punir", f"marcou a mesma pessoa {repetido}x na mesma mensagem"

    if len(counts) >= MASS_MENTION_DISTINCT:
        return "punir", f"marcou {len(counts)} pessoas de uma vez"

    pico = max(historico.values(), default=0)
    if pico >= MENTION_PUNISH_AT:
        return "punir", f"marcou a mesma pessoa em {pico} mensagens seguidas"
    if pico == MENTION_WARN_AT:
        return "avisar", "insistindo na mencao"
    return None, ""


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
        # (guild, autor, alvo) -> deque[timestamp das mensagens que marcaram esse alvo]
        self.mention_log: dict[tuple[int, int, str], deque] = defaultdict(lambda: deque(maxlen=10))
        # Quem ja foi avisado nesta janela, pra nao repetir o aviso a cada mensagem.
        self.mention_warned: dict[tuple[int, int], float] = {}
        # Mesma ideia, pra regra 1 (muitas mensagens seguidas) do check_flood.
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

        # regra 3: insistencia em mencionar a mesma pessoa (ver check_mentions)
        await self.check_mentions(message)

    async def check_mentions(self, message: discord.Message):
        """Avisa antes de punir, e so pune insistencia de verdade.

        A regra anterior apagava na hora qualquer mensagem com 5+ mencoes, sem aviso -
        e "marquei 5 amigos" caiu nela em producao. Aqui a punicao exige repetir a MESMA
        pessoa, e a pessoa recebe um aviso antes de qualquer coisa ser apagada.
        """
        counts = mention_counts(message.content)
        if not counts:
            return

        agora = time.monotonic()
        historico: dict[str, int] = {}
        for alvo in counts:
            # Marcar a si mesmo nao e incomodo pra ninguem.
            if alvo == str(message.author.id):
                continue
            registro = self.mention_log[(message.guild.id, message.author.id, alvo)]
            registro.append(agora)
            while registro and agora - registro[0] > MENTION_WINDOW_SECONDS:
                registro.popleft()
            historico[alvo] = len(registro)

        acao, motivo = mention_verdict(counts, historico)
        if acao is None:
            return

        if acao == "avisar":
            chave = (message.guild.id, message.author.id)
            ultimo_aviso = self.mention_warned.get(chave, 0.0)
            if agora - ultimo_aviso < MENTION_WINDOW_SECONDS:
                return
            self.mention_warned[chave] = agora
            minutos = MENTION_TIMEOUT_SECONDS // 60
            # Nada e apagado aqui de proposito: o aviso e pra dar chance de parar.
            await message.reply(
                f"Opa, {message.author.display_name}, segura a mao na mencao — se continuar "
                f"marcando a mesma pessoa eu vou te dar {minutos} minutos de castigo.",
                mention_author=False,
            )
            return

        # Punicao: limpa o historico pra nao repunir pela mesma sequencia assim que o
        # cooldown acabar.
        for alvo in counts:
            self.mention_log.pop((message.guild.id, message.author.id, alvo), None)
        await self.punish(message, motivo, [message], timeout_seconds=MENTION_TIMEOUT_SECONDS)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        await self.check_flood(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
