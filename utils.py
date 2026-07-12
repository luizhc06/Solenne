import re

import discord

AMBIENT_CHANNEL_NAMES = {"geral", "comidas", "bot", "videojogos-geral"}
AMBIENT_COOLDOWN_SECONDS = 180

THINKING_GIF_URL = "https://media.giphy.com/media/3oEjI6SIIHBdRxXI40/giphy.gif"
THINKING_ETA_SECONDS = 20
NEWS_THINKING_ETA_SECONDS = 40

URL_PATTERN = re.compile(r"https?://\S+")


def is_ambient_channel(channel) -> bool:
    name = getattr(channel, "name", "") or ""
    name = name.lower()
    return any(target in name for target in AMBIENT_CHANNEL_NAMES)


def looks_like_question(content: str) -> bool:
    content = content.strip()
    if content.startswith("/"):
        return False
    without_urls = URL_PATTERN.sub("", content)
    return "?" in without_urls and len(content) > 6


def thinking_embed(text: str | None = None, eta_seconds: int = THINKING_ETA_SECONDS) -> discord.Embed:
    text = text or f"🧠 Pensando... (resposta em ~{eta_seconds}s)"
    # GIF no author (pequeno, topo) em vez de thumbnail (grande, centralizado) - fica
    # discreto, tipo um indicador de "digitando..." em vez de dominar a mensagem.
    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_author(name=text, icon_url=THINKING_GIF_URL)
    return embed
