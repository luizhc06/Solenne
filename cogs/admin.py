import asyncio
import random
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import OWNER_USER_ID
from notify import notify_owner_text
from ai_client import ai_gate, _complete
from utils import parse_emoji_ref

log = logging.getLogger("hermes-bot")

# Limites reais da API do Discord - checar ANTES de tentar upar poupa uma ida e volta
# que ia falhar de qualquer jeito (mesmo raciocinio de MAX_INPUT_BYTES em
# cogs/videotools.py). O resto (nome invalido, servidor no limite de slots) fica por
# conta do discord.HTTPException mesmo - ver comentario em _criar_emoji/_criar_figurinha.
EMOJI_MAX_BYTES = 256 * 1024
STICKER_MAX_BYTES = 512 * 1024


def _validar_imagem_emoji(imagem: discord.Attachment) -> str | None:
    """Devolve uma mensagem de erro amigavel, ou None se o anexo parece valido."""
    if imagem.content_type and not imagem.content_type.startswith("image/"):
        return f"Isso ai parece ser `{imagem.content_type}`, nao imagem. Anexa PNG, JPG, GIF ou WEBP."
    if imagem.size > EMOJI_MAX_BYTES:
        return f"Essa imagem tem mais de {EMOJI_MAX_BYTES // 1024}KB, o Discord nao aceita pra emoji. Usa uma menor."
    return None


def _validar_imagem_figurinha(imagem: discord.Attachment) -> str | None:
    """Mesma ideia de _validar_imagem_emoji, mas com o teto de figurinha (maior) e
    restrito a PNG/APNG - o Discord nao aceita GIF/JPG/WEBP pra figurinha customizada,
    so imagem estatica ou animada em PNG (alem de Lottie em JSON, que este comando nao
    cobre)."""
    if imagem.content_type and imagem.content_type not in ("image/png", "image/apng"):
        return f"Isso ai parece ser `{imagem.content_type}`. O Discord so aceita PNG ou APNG pra figurinha."
    if imagem.size > STICKER_MAX_BYTES:
        return f"Essa imagem tem mais de {STICKER_MAX_BYTES // 1024}KB, o Discord nao aceita pra figurinha. Usa uma menor."
    return None


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id != OWNER_USER_ID:
            await interaction.response.send_message(
                "Esse comando e restrito ao dono do bot.", ephemeral=True
            )
            local = interaction.command.qualified_name if interaction.command else "?"
            servidor = interaction.guild.name if interaction.guild else "DM"
            asyncio.create_task(
                notify_owner_text(
                    interaction.client,
                    f"⚠️ **Tentativa de uso de comando restrito**\n"
                    f"**Usuario:** {interaction.user} (`{interaction.user.id}`)\n"
                    f"**Comando:** /{local}\n"
                    f"**Local:** {servidor}",
                )
            )
            return False
        return True

    return app_commands.check(predicate)


PERTURBAR_LINHAS = [
    "psiu {mention}, psiu",
    "{mention} vc viu que eu to aqui ne",
    "ei {mention} SO QUERIA FALAR OI",
    "{mention} presta atencao em mim",
    "{mention} responde ai vai",
    "to entediada {mention}, fala comigo",
    "{mention} eu sei que vc ta vendo essa mensagem",
    "{mention}?? {mention}?? {mention}??",
    "nao esquece de mim {mention} 🥺",
    "{mention} adivinha quem e",
    "oi {mention} de novo",
    "{mention} 👀",
    "{mention} vc dormiu?",
    "{mention} manda um oi ai",
    "so passando pra perturbar {mention} mesmo",
]

PERTURBAR_COMEBACK_PROMPT = """Voce e Solenne e esta de brincadeira perturbando {nome} no chat do
Discord (tipo encher o saco de leve, sem ofender). {nome} acabou de responder: "{resposta}"

Escreva UMA linha curta, engracada e na hora, continuando a brincadeira de forma leve a partir
dessa resposta - sem ser ofensiva, sem sair do personagem. Responda somente com essa linha."""


async def _perturbar_comeback(resposta: str, nome: str) -> str:
    prompt = PERTURBAR_COMEBACK_PROMPT.format(nome=nome, resposta=resposta[:300])
    return await _complete([{"role": "user", "content": prompt}], max_tokens=100)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="kick", description="[Dono] Expulsa um membro do servidor")
    @owner_only()
    @app_commands.describe(usuario="Membro a expulsar", motivo="Motivo (opcional)")
    async def kick(self, interaction: discord.Interaction, usuario: discord.Member, motivo: str = None):
        try:
            await usuario.kick(reason=motivo or f"Expulso por {interaction.user}")
            await interaction.response.send_message(f"👢 **{usuario}** foi expulso.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Sem permissao de Expulsar Membros no servidor.", ephemeral=True
            )

    @app_commands.command(name="addrole", description="[Dono] Adiciona um cargo a um membro")
    @owner_only()
    @app_commands.describe(usuario="Membro", cargo="Cargo a adicionar")
    async def addrole(self, interaction: discord.Interaction, usuario: discord.Member, cargo: discord.Role):
        try:
            await usuario.add_roles(cargo, reason=f"Adicionado por {interaction.user}")
            await interaction.response.send_message(f"✅ Cargo **{cargo.name}** adicionado a **{usuario}**.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Sem permissao de Gerenciar Cargos (ou o cargo esta acima do meu na hierarquia).",
                ephemeral=True,
            )

    @app_commands.command(name="removerole", description="[Dono] Remove um cargo de um membro")
    @owner_only()
    @app_commands.describe(usuario="Membro", cargo="Cargo a remover")
    async def removerole(self, interaction: discord.Interaction, usuario: discord.Member, cargo: discord.Role):
        try:
            await usuario.remove_roles(cargo, reason=f"Removido por {interaction.user}")
            await interaction.response.send_message(f"✅ Cargo **{cargo.name}** removido de **{usuario}**.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Sem permissao de Gerenciar Cargos (ou o cargo esta acima do meu na hierarquia).",
                ephemeral=True,
            )

    @app_commands.command(name="criarcanal", description="[Dono] Cria um canal de texto ou voz")
    @owner_only()
    @app_commands.describe(nome="Nome do canal", tipo="texto ou voz")
    @app_commands.choices(
        tipo=[
            app_commands.Choice(name="Texto", value="texto"),
            app_commands.Choice(name="Voz", value="voz"),
        ]
    )
    async def criarcanal(self, interaction: discord.Interaction, nome: str, tipo: app_commands.Choice[str]):
        try:
            if tipo.value == "voz":
                canal = await interaction.guild.create_voice_channel(nome, reason=f"Criado por {interaction.user}")
                await interaction.response.send_message(f"✅ Canal de voz **{canal.name}** criado.")
            else:
                canal = await interaction.guild.create_text_channel(nome, reason=f"Criado por {interaction.user}")
                await interaction.response.send_message(f"✅ Canal {canal.mention} criado.")
        except discord.Forbidden:
            await interaction.response.send_message("Sem permissao de Gerenciar Canais.", ephemeral=True)

    @app_commands.command(name="apagarcanal", description="[Dono] Apaga um canal")
    @owner_only()
    @app_commands.describe(canal="Canal a apagar")
    async def apagarcanal(self, interaction: discord.Interaction, canal: discord.abc.GuildChannel):
        nome = canal.name
        try:
            await canal.delete(reason=f"Apagado por {interaction.user}")
            await interaction.response.send_message(f"🗑️ Canal **{nome}** apagado.")
        except discord.Forbidden:
            await interaction.response.send_message("Sem permissao de Gerenciar Canais.", ephemeral=True)

    @app_commands.command(name="lock", description="[Dono] Tranca um canal (ninguem consegue mandar mensagem)")
    @owner_only()
    @app_commands.describe(canal="Canal a trancar (padrao: canal atual)")
    async def lock(self, interaction: discord.Interaction, canal: discord.TextChannel = None):
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

    @app_commands.command(name="unlock", description="[Dono] Destranca um canal")
    @owner_only()
    @app_commands.describe(canal="Canal a destrancar (padrao: canal atual)")
    async def unlock(self, interaction: discord.Interaction, canal: discord.TextChannel = None):
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

    @app_commands.command(name="clear", description="[Dono] Apaga as ultimas N mensagens do canal")
    @owner_only()
    @app_commands.describe(quantidade="Quantas mensagens apagar (1-100)")
    async def clear(self, interaction: discord.Interaction, quantidade: app_commands.Range[int, 1, 100]):
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await interaction.channel.purge(limit=quantidade)
        except discord.Forbidden:
            await interaction.followup.send(
                "Sem permissao de Gerenciar Mensagens nesse canal.", ephemeral=True
            )
            return
        except discord.HTTPException:
            log.exception("Erro ao limpar mensagens")
            await interaction.followup.send("Deu erro ao apagar as mensagens.", ephemeral=True)
            return
        await interaction.followup.send(f"🧹 Apaguei {len(deleted)} mensagem(ns).", ephemeral=True)

    @app_commands.command(
        name="perturbar",
        description="[Dono] A Solenne perturba um usuario no canal, de brincadeira, por um tempinho",
    )
    @owner_only()
    @app_commands.describe(
        usuario="Quem vai ser perturbado",
        vezes="Quantas mensagens (1-10, padrao 3)",
        intervalo="Segundos entre cada mensagem (10-120, padrao 20)",
    )
    async def perturbar(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        vezes: app_commands.Range[int, 1, 10] = 3,
        intervalo: app_commands.Range[int, 10, 120] = 20,
    ):
        if usuario.bot:
            await interaction.response.send_message("Nao da pra perturbar outro bot.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Combinado, vou perturbar {usuario.mention} {vezes}x aqui no canal 😈", ephemeral=True
        )
        channel = interaction.channel
        linhas = random.sample(PERTURBAR_LINHAS, min(vezes, len(PERTURBAR_LINHAS)))

        def is_reply_from_target(m: discord.Message) -> bool:
            return m.channel.id == channel.id and m.author.id == usuario.id

        for linha in linhas:
            await channel.send(linha.format(mention=usuario.mention))
            try:
                reply_msg = await self.bot.wait_for("message", timeout=intervalo, check=is_reply_from_target)
            except asyncio.TimeoutError:
                continue
            try:
                async with ai_gate.interactive():
                    comeback = await _perturbar_comeback(reply_msg.content, usuario.display_name)
                if not comeback or not comeback.strip():
                    # A IA as vezes recusa/esvazia a resposta (ex: reply com palavrao
                    # pesado) - cai numa fala pronta em vez de nao mandar nada.
                    comeback = random.choice(PERTURBAR_LINHAS).format(mention=usuario.mention)
                await channel.send(comeback.strip())
            except Exception:
                log.exception("Erro ao gerar resposta do /perturbar")

    # -----------------------------------------------------------------------------
    # Emojis e figurinhas
    # -----------------------------------------------------------------------------

    async def _autocomplete_emoji(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Sugere os emojis customizados do servidor conforme a pessoa digita, pra nao
        precisar acertar nome e maiusculas/minusculas de cabeca em /removeemoji."""
        current = (current or "").lower()
        emojis = interaction.guild.emojis if interaction.guild else []
        filtrados = [e for e in emojis if current in e.name.lower()]
        return [
            app_commands.Choice(name=f"{e.name} ({'animado' if e.animated else 'estatico'})", value=e.name)
            for e in filtrados[:25]
        ]

    async def _autocomplete_figurinha(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").lower()
        figurinhas = interaction.guild.stickers if interaction.guild else []
        filtrados = [s for s in figurinhas if current in s.name.lower()]
        return [app_commands.Choice(name=s.name, value=s.name) for s in filtrados[:25]]

    @app_commands.command(name="addemoji", description="[Dono] Adiciona um emoji customizado ao servidor")
    @owner_only()
    @app_commands.describe(
        nome="Nome do emoji (sem espacos, 2-32 caracteres)",
        imagem="Imagem do emoji (PNG, JPG, GIF ou WEBP, ate 256KB)",
    )
    async def addemoji(self, interaction: discord.Interaction, nome: str, imagem: discord.Attachment):
        erro = _validar_imagem_emoji(imagem)
        if erro:
            await interaction.response.send_message(erro, ephemeral=True)
            return

        await interaction.response.defer()
        try:
            dados = await imagem.read()
            emoji = await interaction.guild.create_custom_emoji(
                name=nome, image=dados, reason=f"Adicionado por {interaction.user}"
            )
            await interaction.followup.send(f"✅ Emoji {emoji} (`{emoji.name}`) adicionado.")
        except discord.Forbidden:
            await interaction.followup.send("Sem permissao de Gerenciar Emojis e Figurinhas.", ephemeral=True)
        except discord.HTTPException as exc:
            # Surfaceia exc.text de proposito (diferente do padrao generico do resto do
            # arquivo): e a mensagem de validacao do proprio Discord (nome invalido,
            # limite de emojis do servidor atingido etc) e diz exatamente o que corrigir,
            # em vez de um "tenta de novo" que esconde a causa.
            log.exception("Erro ao criar emoji %s", nome)
            await interaction.followup.send(
                f"Deu erro ao criar o emoji: {exc.text or 'erro desconhecido'}", ephemeral=True
            )

    @app_commands.command(name="removeemoji", description="[Dono] Remove um emoji customizado do servidor")
    @owner_only()
    @app_commands.describe(emoji="Cole o emoji ou escolha o nome na lista")
    @app_commands.autocomplete(emoji=_autocomplete_emoji)
    async def removeemoji(self, interaction: discord.Interaction, emoji: str):
        nome, emoji_id = parse_emoji_ref(emoji)
        alvo = (
            interaction.guild.get_emoji(emoji_id)
            if emoji_id is not None
            else discord.utils.get(interaction.guild.emojis, name=nome)
        )
        if alvo is None:
            await interaction.response.send_message(
                "Nao encontrei nenhum emoji com esse nome/id neste servidor.", ephemeral=True
            )
            return

        nome_removido = alvo.name
        try:
            await alvo.delete(reason=f"Removido por {interaction.user}")
            await interaction.response.send_message(f"🗑️ Emoji **{nome_removido}** removido.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Sem permissao de Gerenciar Emojis e Figurinhas.", ephemeral=True
            )

    @app_commands.command(name="addfigurinha", description="[Dono] Adiciona uma figurinha ao servidor")
    @owner_only()
    @app_commands.describe(
        nome="Nome da figurinha (2-30 caracteres)",
        descricao="Descricao curta da figurinha",
        emoji_relacionado="Um emoji padrao relacionado (ex: 😀) - usado na busca de figurinhas do Discord",
        imagem="Imagem PNG ou APNG, ate 512KB, ideal 320x320px",
    )
    async def addfigurinha(
        self,
        interaction: discord.Interaction,
        nome: str,
        descricao: str,
        emoji_relacionado: str,
        imagem: discord.Attachment,
    ):
        erro = _validar_imagem_figurinha(imagem)
        if erro:
            await interaction.response.send_message(erro, ephemeral=True)
            return

        await interaction.response.defer()
        try:
            arquivo = await imagem.to_file()
            figurinha = await interaction.guild.create_sticker(
                name=nome,
                description=descricao,
                emoji=emoji_relacionado,
                file=arquivo,
                reason=f"Adicionado por {interaction.user}",
            )
            await interaction.followup.send(f"✅ Figurinha **{figurinha.name}** adicionada.")
        except discord.Forbidden:
            await interaction.followup.send("Sem permissao de Gerenciar Emojis e Figurinhas.", ephemeral=True)
        except discord.HTTPException as exc:
            log.exception("Erro ao criar figurinha %s", nome)
            await interaction.followup.send(
                f"Deu erro ao criar a figurinha: {exc.text or 'erro desconhecido'}", ephemeral=True
            )

    @app_commands.command(name="removefigurinha", description="[Dono] Remove uma figurinha do servidor")
    @owner_only()
    @app_commands.describe(figurinha="Nome da figurinha a remover")
    @app_commands.autocomplete(figurinha=_autocomplete_figurinha)
    async def removefigurinha(self, interaction: discord.Interaction, figurinha: str):
        alvo = discord.utils.get(interaction.guild.stickers, name=(figurinha or "").strip())
        if alvo is None:
            await interaction.response.send_message(
                "Nao encontrei nenhuma figurinha com esse nome neste servidor.", ephemeral=True
            )
            return

        nome_removido = alvo.name
        try:
            await alvo.delete(reason=f"Removido por {interaction.user}")
            await interaction.response.send_message(f"🗑️ Figurinha **{nome_removido}** removida.")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Sem permissao de Gerenciar Emojis e Figurinhas.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
