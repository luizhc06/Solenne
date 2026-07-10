import os
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

import discord
from discord import app_commands
from openai import OpenAI
import yt_dlp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hermes-bot")

NVIDIA_API_KEY = os.environ["NVIDIA_API_KEY"]
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
MODEL = os.environ.get("HERMES_MODEL", "openai/gpt-oss-120b")
ALLOWED_GUILD_ID = int(os.environ["ALLOWED_GUILD_ID"])
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])

client_ai = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_API_KEY)

SYSTEM_PROMPT = """Voce e Solenne, a IA pessoal do Rizu. Fale em pt-BR, sempre no feminino ao se referir a si mesma.

Principios:
- Busque a verdade antes de agradar o usuario.
- Seja honesta sobre incerteza: diga "nao sei" ou "dados insuficientes" quando for o caso.
- Priorize clareza sobre floreio: frases curtas, sem enrolacao, sem elogios vazios tipo "otima pergunta!!!".
- Pense em consequencias praticas, nao so teoria bonita.

Como pensar (pipeline mental antes de responder):
1. Identifique o problema central da pergunta.
2. Separe fatos de opiniao.
3. Analise pros e contras de cada caminho.
4. Escolha uma recomendacao principal e justifique com 2-3 argumentos.
5. Deixe claro o que ainda esta em aberto ou incerto.

Quando usar referencias, prefira UMA lente clara ligada ao problema:
- Etica -> utilitarismo (qual opcao gera mais bem-estar geral).
- Vida/trabalho -> estoicismo (foque no que voce controla).
- Liberdade/autonomia -> existencialismo (voce escolhe o sentido, nao recebe pronto).

Tom e formato:
- Direta, amigavel, zero bajulacao. Nunca seca ou fria.
- Emojis so em casos pontuais, nunca em excesso.
- Evite CAPS LOCK exagerado; use enfase pontual quando algo for MUITO importante.
- Priorize listas curtas e resumos executivos, evite parede de texto.
- Nunca invente fatos com confianca quando tiver duvida.
- Nunca responda so com "depende, cada um e unico" - quando apropriado, escolha um lado e explique por que.
"""

# ---- Anti-flood config ----
FLOOD_WINDOW_SECONDS = 5
FLOOD_MAX_MESSAGES = 5
FLOOD_MAX_DUPLICATES = 3
FLOOD_MAX_MENTIONS = 5
TIMEOUT_SECONDS = 60

# ---- Modo ambiente (responde sem ser mencionado, so nesses canais) ----
AMBIENT_CHANNEL_NAMES = {"geral", "comidas", "bot", "videojogos-geral"}
AMBIENT_COOLDOWN_SECONDS = 180


def is_ambient_channel(channel) -> bool:
    name = getattr(channel, "name", "") or ""
    name = name.lower()
    return any(target in name for target in AMBIENT_CHANNEL_NAMES)


def looks_like_question(content: str) -> bool:
    content = content.strip()
    return "?" in content and len(content) > 6

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "nocheckcertificate": True,
    "source_address": "0.0.0.0",
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)


class HermesBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.histories: dict[int, list[dict]] = {}
        self.queues: dict[int, list[dict]] = {}
        # (guild_id, user_id) -> deque[(timestamp, message_id, content, channel_id)]
        self.msg_log: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))
        self.recently_punished: dict[tuple[int, int], float] = {}
        self.ambient_last_reply: dict[int, float] = {}

    async def setup_hook(self):
        await self.tree.sync()


bot = HermesBot()


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id != OWNER_USER_ID:
            await interaction.response.send_message(
                "Esse comando e restrito ao dono do bot.", ephemeral=True
            )
            return False
        return True

    return app_commands.check(predicate)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
    log.exception("Erro em comando", exc_info=error)
    if not interaction.response.is_done():
        await interaction.response.send_message("Deu erro ao executar o comando.", ephemeral=True)


# ---------------- Guild lock ----------------

@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.id != ALLOWED_GUILD_ID:
        log.warning("Saindo de servidor nao autorizado: %s (%s)", guild.name, guild.id)
        await guild.leave()


async def enforce_guild_lock():
    for guild in list(bot.guilds):
        if guild.id != ALLOWED_GUILD_ID:
            log.warning("Saindo de servidor nao autorizado: %s (%s)", guild.name, guild.id)
            await guild.leave()


# ---------------- Hermes chat ----------------

def ask_hermes(channel_id: int, user_msg: str) -> str:
    history = bot.histories.setdefault(channel_id, [])
    history.append({"role": "user", "content": user_msg})
    history[:] = history[-20:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    completion = client_ai.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.6,
        max_tokens=800,
    )
    reply = completion.choices[0].message.content
    history.append({"role": "assistant", "content": reply})
    return reply


# ---------------- Music ----------------

async def extract_track(query: str) -> dict:
    loop = asyncio.get_event_loop()

    def _extract():
        data = ytdl.extract_info(query, download=False)
        if "entries" in data:
            data = data["entries"][0]
        return {"title": data.get("title", "Sem titulo"), "url": data["url"]}

    return await loop.run_in_executor(None, _extract)


def play_next(guild_id: int, voice_client: discord.VoiceClient):
    queue = bot.queues.get(guild_id, [])
    if not queue:
        return
    track = queue.pop(0)
    source = discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTS)

    def after(err):
        if err:
            log.error("Erro na reproducao: %s", err)
        fut = asyncio.run_coroutine_threadsafe(_advance(guild_id, voice_client), bot.loop)
        try:
            fut.result()
        except Exception:
            log.exception("Erro ao avancar fila")

    voice_client.play(source, after=after)


async def _advance(guild_id: int, voice_client: discord.VoiceClient):
    play_next(guild_id, voice_client)


# ---------------- Anti-flood / automod ----------------

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


async def notify_owner(guild: discord.Guild, member: discord.Member, reason: str, sample: str):
    owner = bot.get_user(OWNER_USER_ID) or await bot.fetch_user(OWNER_USER_ID)
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


async def punish(message: discord.Message, reason: str, extra_msgs: list[discord.Message] | None = None):
    member = message.author
    guild = message.guild
    key = (guild.id, member.id)

    now = time.monotonic()
    if key in bot.recently_punished and now - bot.recently_punished[key] < TIMEOUT_SECONDS:
        return
    bot.recently_punished[key] = now

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

    await notify_owner(guild, member, reason, "\n".join(sample_lines))


async def check_flood(message: discord.Message):
    if message.guild is None or message.guild.id != ALLOWED_GUILD_ID:
        return
    member = message.author
    if member.bot or member.id == OWNER_USER_ID or member.guild_permissions.administrator:
        return

    key = (message.guild.id, member.id)
    log_deque = bot.msg_log[key]
    now = time.monotonic()
    log_deque.append((now, message))

    # limpa entradas fora da janela
    while log_deque and now - log_deque[0][0] > FLOOD_WINDOW_SECONDS:
        log_deque.popleft()

    # regra 1: muitas mensagens seguidas
    if len(log_deque) >= FLOOD_MAX_MESSAGES:
        recent_msgs = [m for _, m in log_deque]
        await punish(message, f"{len(recent_msgs)} mensagens em {FLOOD_WINDOW_SECONDS}s", recent_msgs)
        return

    # regra 2: mensagem repetida (mesmo conteudo)
    contents = [m.content for _, m in log_deque if m.content]
    if contents:
        last = contents[-1]
        repeats = [m for _, m in log_deque if m.content == last]
        if len(repeats) >= FLOOD_MAX_DUPLICATES:
            await punish(message, "mensagens repetidas (spam)", repeats)
            return

    # regra 3: spam de mencoes (raid-like)
    if len(message.mentions) + len(message.role_mentions) >= FLOOD_MAX_MENTIONS:
        await punish(message, "spam de mencoes", [message])
        return


# ---------------- Eventos ----------------

@bot.event
async def on_ready():
    log.info("Logado como %s (%s)", bot.user, datetime.now().isoformat())
    await enforce_guild_lock()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await check_flood(message)

    mentioned = bot.user in message.mentions
    ambient_trigger = False

    if not mentioned and is_ambient_channel(message.channel) and looks_like_question(message.content):
        last = bot.ambient_last_reply.get(message.channel.id, 0.0)
        if time.monotonic() - last >= AMBIENT_COOLDOWN_SECONDS:
            ambient_trigger = True

    if not mentioned and not ambient_trigger:
        return

    if mentioned:
        content = message.content.replace(f"<@{bot.user.id}>", "").strip() or "Oi!"
    else:
        content = message.content
        bot.ambient_last_reply[message.channel.id] = time.monotonic()

    async with message.channel.typing():
        try:
            reply = ask_hermes(message.channel.id, content)
        except Exception:
            log.exception("Erro ao consultar Hermes")
            reply = "Deu ruim aqui consultando o modelo, tenta de novo em instantes."

    for chunk_start in range(0, len(reply), 1900):
        await message.channel.send(reply[chunk_start:chunk_start + 1900])


# ---------------- Slash commands: chat ----------------

@bot.tree.command(name="ask", description="Pergunte algo ao Hermes")
@app_commands.describe(pergunta="O que voce quer perguntar")
async def ask(interaction: discord.Interaction, pergunta: str):
    await interaction.response.defer(thinking=True)
    try:
        reply = ask_hermes(interaction.channel_id, pergunta)
    except Exception:
        log.exception("Erro ao consultar Hermes")
        reply = "Deu ruim aqui consultando o modelo, tenta de novo em instantes."
    for chunk_start in range(0, len(reply), 1900):
        await interaction.followup.send(reply[chunk_start:chunk_start + 1900])


# ---------------- Slash commands: musica ----------------

@bot.tree.command(name="play", description="Toca uma musica (nome ou link do YouTube)")
@app_commands.describe(busca="Nome da musica ou link do YouTube")
async def play(interaction: discord.Interaction, busca: str):
    if interaction.user.voice is None:
        await interaction.response.send_message("Voce precisa estar em um canal de voz.")
        return

    await interaction.response.defer()
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await channel.connect()
    elif vc.channel != channel:
        await vc.move_to(channel)

    try:
        track = await extract_track(busca)
    except Exception:
        log.exception("Erro ao buscar musica")
        await interaction.followup.send("Nao consegui achar/baixar essa musica.")
        return

    bot.queues.setdefault(interaction.guild_id, []).append(track)

    if not vc.is_playing() and not vc.is_paused():
        play_next(interaction.guild_id, vc)
        await interaction.followup.send(f"Tocando agora: **{track['title']}**")
    else:
        await interaction.followup.send(f"Adicionado na fila: **{track['title']}**")


@bot.tree.command(name="skip", description="Pula a musica atual")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("Pulado.")
    else:
        await interaction.response.send_message("Nada tocando agora.")


@bot.tree.command(name="pause", description="Pausa a musica atual")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Pausado.")
    else:
        await interaction.response.send_message("Nada tocando agora.")


@bot.tree.command(name="resume", description="Retoma a musica pausada")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Retomado.")
    else:
        await interaction.response.send_message("Nada pausado agora.")


@bot.tree.command(name="stop", description="Para a musica, limpa a fila e sai do canal")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    bot.queues[interaction.guild_id] = []
    if vc:
        vc.stop()
        await vc.disconnect()
    await interaction.response.send_message("Parado e desconectado.")


@bot.tree.command(name="queue", description="Mostra a fila de musicas")
async def queue_cmd(interaction: discord.Interaction):
    q = bot.queues.get(interaction.guild_id, [])
    if not q:
        await interaction.response.send_message("Fila vazia.")
        return
    listing = "\n".join(f"{i + 1}. {t['title']}" for i, t in enumerate(q))
    await interaction.response.send_message(listing[:1900])


# ---------------- Slash commands: admin (somente dono) ----------------

@bot.tree.command(name="kick", description="[Dono] Expulsa um membro do servidor")
@owner_only()
@app_commands.describe(usuario="Membro a expulsar", motivo="Motivo (opcional)")
async def kick(interaction: discord.Interaction, usuario: discord.Member, motivo: str = None):
    try:
        await usuario.kick(reason=motivo or f"Expulso por {interaction.user}")
        await interaction.response.send_message(f"👢 **{usuario}** foi expulso.")
    except discord.Forbidden:
        await interaction.response.send_message(
            "Sem permissao de Expulsar Membros no servidor.", ephemeral=True
        )


@bot.tree.command(name="addrole", description="[Dono] Adiciona um cargo a um membro")
@owner_only()
@app_commands.describe(usuario="Membro", cargo="Cargo a adicionar")
async def addrole(interaction: discord.Interaction, usuario: discord.Member, cargo: discord.Role):
    try:
        await usuario.add_roles(cargo, reason=f"Adicionado por {interaction.user}")
        await interaction.response.send_message(f"✅ Cargo **{cargo.name}** adicionado a **{usuario}**.")
    except discord.Forbidden:
        await interaction.response.send_message(
            "Sem permissao de Gerenciar Cargos (ou o cargo esta acima do meu na hierarquia).",
            ephemeral=True,
        )


@bot.tree.command(name="removerole", description="[Dono] Remove um cargo de um membro")
@owner_only()
@app_commands.describe(usuario="Membro", cargo="Cargo a remover")
async def removerole(interaction: discord.Interaction, usuario: discord.Member, cargo: discord.Role):
    try:
        await usuario.remove_roles(cargo, reason=f"Removido por {interaction.user}")
        await interaction.response.send_message(f"✅ Cargo **{cargo.name}** removido de **{usuario}**.")
    except discord.Forbidden:
        await interaction.response.send_message(
            "Sem permissao de Gerenciar Cargos (ou o cargo esta acima do meu na hierarquia).",
            ephemeral=True,
        )


@bot.tree.command(name="criarcanal", description="[Dono] Cria um canal de texto ou voz")
@owner_only()
@app_commands.describe(nome="Nome do canal", tipo="texto ou voz")
@app_commands.choices(
    tipo=[
        app_commands.Choice(name="Texto", value="texto"),
        app_commands.Choice(name="Voz", value="voz"),
    ]
)
async def criarcanal(interaction: discord.Interaction, nome: str, tipo: app_commands.Choice[str]):
    try:
        if tipo.value == "voz":
            canal = await interaction.guild.create_voice_channel(nome, reason=f"Criado por {interaction.user}")
            await interaction.response.send_message(f"✅ Canal de voz **{canal.name}** criado.")
        else:
            canal = await interaction.guild.create_text_channel(nome, reason=f"Criado por {interaction.user}")
            await interaction.response.send_message(f"✅ Canal {canal.mention} criado.")
    except discord.Forbidden:
        await interaction.response.send_message("Sem permissao de Gerenciar Canais.", ephemeral=True)


@bot.tree.command(name="apagarcanal", description="[Dono] Apaga um canal")
@owner_only()
@app_commands.describe(canal="Canal a apagar")
async def apagarcanal(interaction: discord.Interaction, canal: discord.abc.GuildChannel):
    nome = canal.name
    try:
        await canal.delete(reason=f"Apagado por {interaction.user}")
        await interaction.response.send_message(f"🗑️ Canal **{nome}** apagado.")
    except discord.Forbidden:
        await interaction.response.send_message("Sem permissao de Gerenciar Canais.", ephemeral=True)


@bot.tree.command(name="lock", description="[Dono] Tranca um canal (ninguem consegue mandar mensagem)")
@owner_only()
@app_commands.describe(canal="Canal a trancar (padrao: canal atual)")
async def lock(interaction: discord.Interaction, canal: discord.TextChannel = None):
    canal = canal or interaction.channel
    overwrite = canal.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    try:
        await canal.set_permissions(
            interaction.guild.default_role, overwrite=overwrite, reason=f"Lock por {interaction.user}"
        )
        await interaction.response.send_message(f"🔒 {canal.mention} trancado.")
    except discord.Forbidden:
        await interaction.response.send_message("Sem permissao de Gerenciar Canais.", ephemeral=True)


@bot.tree.command(name="unlock", description="[Dono] Destranca um canal")
@owner_only()
@app_commands.describe(canal="Canal a destrancar (padrao: canal atual)")
async def unlock(interaction: discord.Interaction, canal: discord.TextChannel = None):
    canal = canal or interaction.channel
    overwrite = canal.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = None
    try:
        await canal.set_permissions(
            interaction.guild.default_role, overwrite=overwrite, reason=f"Unlock por {interaction.user}"
        )
        await interaction.response.send_message(f"🔓 {canal.mention} destrancado.")
    except discord.Forbidden:
        await interaction.response.send_message("Sem permissao de Gerenciar Canais.", ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
