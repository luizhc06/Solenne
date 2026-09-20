import re
import time
import logging

import discord

log = logging.getLogger("hermes-bot")

AMBIENT_CHANNEL_NAMES = {"geral", "comidas", "bot", "videojogos-geral"}
AMBIENT_COOLDOWN_SECONDS = 180

THINKING_GIF_URL = "https://media.giphy.com/media/RgzryV9nRCMHPVVXPV/100w.gif"

URL_PATTERN = re.compile(r"https?://\S+")

# Qualquer mencao do Discord: usuario (<@123>), usuario em forma antiga de apelido
# (<@!123>) e cargo (<@&123>). Tirar SO a mencao da Solenne deixava as outras como
# tokens crus de ID no texto que ia pro modelo - ver strip_mentions.
MENTION_RE = re.compile(r"<@[!&]?\d+>")


def strip_mentions(content: str) -> str:
    """Tira TODAS as mencoes, nao so a da Solenne.

    Em 19/09/2026 alguem chamou a galera pra jogar com uma mensagem que era so
    "@Hud @Rizu @KrekNeto @Solenne", sem texto nenhum. O codigo tirava apenas a mencao
    dela, entao sobravam os outros tres como "<@111> <@222> <@333>" - string nao vazia,
    que seguiu pro modelo COMO SE FOSSE A PERGUNTA. Pior: o system prompt informa o ID
    do dono, entao ela reconheceu o numero no meio dos tokens e respondeu "voce marcou
    o Rizu ai", parecendo estar falando em nome dele.
    """
    return MENTION_RE.sub(" ", content or "").strip()


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


async def safe_edit_original(interaction: discord.Interaction, **kwargs) -> None:
    """Edita a resposta original da interacao, com fallback se o token ja expirou.

    O token do webhook de uma interacao expira 15min depois do comando (API do
    Discord) - com a fila de IA sob carga (achado do conselho, 18/08/2026), uma
    resposta pode demorar o suficiente pra passar disso. Sem este helper, cada
    comando repetia o mesmo try/except (ou nem tinha) e o "Pensando..." ficava
    parado pra sempre, sem ninguem saber o motivo. Aceita os mesmos kwargs de
    Interaction.edit_original_response (content/embed/embeds/view).
    """
    try:
        await interaction.edit_original_response(**kwargs)
    except discord.HTTPException:
        log.warning("Token da interacao expirou antes da resposta - mandando no canal direto.")
        embeds = kwargs.get("embeds")
        if embeds is None:
            embed = kwargs.get("embed")
            embeds = [embed] if embed is not None else []
        send_kwargs: dict = {"embeds": embeds}
        if kwargs.get("view") is not None:
            send_kwargs["view"] = kwargs["view"]
        await interaction.channel.send(kwargs.get("content") or None, **send_kwargs)


async def safe_followup_send(interaction: discord.Interaction, content, **kwargs) -> None:
    """Mesma ideia de safe_edit_original, pra followup.send (ver docstring acima)."""
    try:
        await interaction.followup.send(content, **kwargs)
    except discord.HTTPException:
        log.warning("Token da interacao expirou antes do followup - mandando no canal direto.")
        await interaction.channel.send(content, **kwargs)


def thinking_embed(text: str | None = None) -> discord.Embed:
    """Placeholder de "estou trabalhando nisso" enquanto a resposta real nao chega.

    SEM estimativa de tempo, de proposito. Ate 13/09/2026 o padrao prometia "resposta em
    ~20s" - numero fixo que errava nos dois sentidos: as vezes ela responde bem antes, e
    com raciocinio ligado passa dos 45s. Palpite preciso e pior que nenhum, porque vira
    promessa quebrada; quem esta olhando so precisa saber que ela nao travou.

    O parametro eta_seconds saiu junto. Ele so era usado quando text era None, entao os
    cinco call sites que passavam os dois (news, search, linksummary e os dois de
    videotools) tinham o numero silenciosamente ignorado - nunca chegou a aparecer.

    O GIF fica no icon_url do author, um circulo de ~24px. Chegou a ir pro thumbnail
    (~80px) em 13/09/2026, mas o dono preferiu de volta no tamanho pequeno: o placeholder
    e um indicador de "nao travei", nao o assunto da mensagem. O Discord nao deixa
    escolher o tamanho do thumbnail - e ~80px fixo -, entao 24px so existe aqui.

    Por isso a URL aponta pra variante 100w e nao pra 200w: reduzido a 24px nao ha
    diferenca visivel, e sao 85KB em vez de 314KB.
    """
    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_author(name=text or "Pensando...", icon_url=THINKING_GIF_URL)
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
