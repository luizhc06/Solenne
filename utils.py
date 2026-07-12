import re
import time

import discord

AMBIENT_CHANNEL_NAMES = {"geral", "comidas", "bot", "videojogos-geral"}
AMBIENT_COOLDOWN_SECONDS = 180

THINKING_GIF_URL = "https://media.giphy.com/media/2WjpfxAI5MvC9Nl8U7/100w.gif"
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


class TTLCache:
    """Cache simples em memoria com expiracao - evita bater toda hora em APIs
    externas que nao mudam com frequencia (clima, perfil de interesses etc)."""

    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._store: dict = {}

    def get(self, key):
        entry = self._store.get(key)
        if entry is None:
            return None, False
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None, False
        return value, True

    def set(self, key, value):
        self._store[key] = (time.monotonic() + self.ttl, value)
