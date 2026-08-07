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


# Como as pessoas chamam a Solenne no meio da frase, sem marcar com @. Aceita
# variacao e diminutivo ("solene", "soleninha"), mas exige limite de palavra a
# esquerda pra nao casar no meio de outra palavra. Deliberadamente NAO aceita "sol"
# sozinho: e palavra comum demais em portugues pra virar gatilho.
NAME_TRIGGER_RE = re.compile(r"(?<![\wÀ-ÿ])solen\w*", re.IGNORECASE)

# Perguntas de verdade que nao terminam com "?" - o gatilho antigo exigia o "?"
# literal, entao "solenne me explica isso" e "alguem sabe se vai chover" passavam
# batido e a Solenne parecia estar ignorando as pessoas.
QUESTION_WORDS_RE = re.compile(
    r"(?<![\wÀ-ÿ])(qual|quais|quem|quando|onde|como|por\s*que|porque|pq|quanto|quantos|quantas|"
    r"o\s*que|oq|sera|alguem\s+sabe|algum\s+de\s+voces|explica|explique|me\s+explica|"
    r"me\s+diz|sabe\s+se|da\s+pra|vale\s+a\s+pena|recomenda|ajuda\s+ai)(?![\wÀ-ÿ])",
    re.IGNORECASE,
)


def mentions_solenne(content: str) -> bool:
    """Se a mensagem chama a Solenne pelo nome (sem @), tipo "solenne, o que voce acha"."""
    return bool(NAME_TRIGGER_RE.search(URL_PATTERN.sub("", content or "")))


def looks_like_question(content: str) -> bool:
    content = (content or "").strip()
    if content.startswith("/"):
        return False
    without_urls = URL_PATTERN.sub("", content)
    if len(content) <= 6:
        return False
    return "?" in without_urls or bool(QUESTION_WORDS_RE.search(without_urls))


def truncate_words(text: str, limit: int) -> str:
    """Corta no espaco anterior ao limite, com reticencias - nunca no meio da palavra."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    corte = text[:limit - 1]
    espaco = corte.rfind(" ")
    if espaco > limit * 0.6:
        corte = corte[:espaco]
    return corte.rstrip(" ,;:.-") + "…"


def truncate_sentences(text: str, limit: int) -> str:
    """Corta no fim da ultima frase que couber, em vez de no meio de uma.

    Titulo de card pode terminar com reticencias sem incomodar; uma fala da Solenne
    cortada em "Tudo ao…" fica so quebrada. Se nem a primeira frase couber, cai pro
    corte por palavra.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text

    fim = max(text.rfind(sinal, 0, limit + 1) for sinal in (". ", "! ", "? ", ".", "!", "?"))
    if fim > 0:
        return text[:fim + 1].strip()
    return truncate_words(text, limit)


def split_discord_message(text: str, limit: int = 1900) -> list[str]:
    """Fatia uma resposta longa respeitando quebras de linha e espacos.

    O corte antigo era em fatias fixas de 1900 caracteres, o que partia palavra,
    link e bloco de codigo no meio. Tambem garante pelo menos um pedaco nao vazio:
    mandar string vazia pro Discord levanta HTTPException e a resposta some.
    """
    text = (text or "").strip()
    if not text:
        return []
    pedacos = []
    while len(text) > limit:
        corte = text.rfind("\n", 0, limit)
        if corte < limit * 0.5:
            corte = text.rfind(" ", 0, limit)
        if corte < limit * 0.5:
            corte = limit
        pedacos.append(text[:corte].strip())
        text = text[corte:].strip()
    if text:
        pedacos.append(text)
    return [p for p in pedacos if p]


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
