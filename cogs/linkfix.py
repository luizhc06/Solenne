import re
import logging

import discord
from discord.ext import commands

log = logging.getLogger("hermes-bot")

# Sem grupos de captura de proposito - .findall() ja retorna a URL inteira.
TWITTER_RE = re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/\S+")
INSTAGRAM_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels)/\S+")
TIKTOK_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/\S+")


def _convert_links(content: str) -> list[str]:
    converted = []
    for link in TWITTER_RE.findall(content):
        converted.append(link.replace("twitter.com", "fxtwitter.com").replace("x.com", "fxtwitter.com"))
    for link in INSTAGRAM_RE.findall(content):
        converted.append(link.replace("instagram.com", "ddinstagram.com"))
    for link in TIKTOK_RE.findall(content):
        converted.append(link.replace("tiktok.com", "vxtiktok.com"))
    return converted


class LinkFixCog(commands.Cog):
    """QoL: troca links de Twitter/Instagram/TikTok por versoes que embedam certo no Discord."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener(name="on_message")
    async def fix_broken_links(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.content:
            return

        converted_links = _convert_links(message.content)
        if not converted_links:
            return

        links_str = "\n".join(converted_links)
        await message.channel.send(
            content=f"🎥 **Embed corrigido para {message.author.mention}:**\n{links_str}",
            silent=True,  # nao gera notificacao nova pra quem ja viu a mensagem original
        )

        try:
            await message.edit(suppress=True)
        except discord.Forbidden:
            pass  # sem permissao de Gerenciar Mensagens - so manda o link corrigido mesmo


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkFixCog(bot))
