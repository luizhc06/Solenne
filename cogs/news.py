import re
import asyncio
import random
import logging
from datetime import datetime, timedelta, timezone, time as dtime

import feedparser
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import ALLOWED_GUILD_ID, NEWS_TIMEZONE
from db import filter_unposted_links, mark_news_posted
from ai_client import ai_lock, _complete
from utils import thinking_embed, NEWS_THINKING_ETA_SECONDS
from views import FeedbackView
from notify import notify_owner_text

log = logging.getLogger("hermes-bot")

NEWS_CHANNEL_NAME = "noticias"
NEWS_POST_TIME = dtime(hour=12, minute=0, tzinfo=NEWS_TIMEZONE)
NEWS_LOOKBACK_HOURS = 30
NEWS_ITEMS_PER_CATEGORY = 4

NEWS_CATEGORIES = {
    "geek": {
        "label": "🎌 Geek & Anime",
        "color": discord.Color.blue(),
        "feeds": [
            ("Anime News Network", "https://www.animenewsnetwork.com/newsfeed/rss.xml"),
            ("MyAnimeList", "https://myanimelist.net/rss/news.xml"),
        ],
    },
    "tecnologia": {
        "label": "💻 Tecnologia & Hardware",
        "color": discord.Color.dark_blue(),
        "feeds": [
            ("Tom's Hardware", "https://www.tomshardware.com/feeds/all"),
            ("Wccftech", "https://wccftech.com/feed/"),
        ],
    },
    "ciencia": {
        "label": "🔬 Ciencia",
        "color": discord.Color.green(),
        "feeds": [
            ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"),
            ("Nature News", "https://www.nature.com/nature.rss"),
        ],
    },
    "ia": {
        "label": "🤖 Inteligencia Artificial",
        "color": discord.Color.purple(),
        "feeds": [
            ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
            ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
        ],
    },
    "brasil": {
        "label": "🇧🇷 Brasil",
        "color": discord.Color.gold(),
        "feeds": [
            ("G1 Brasil", "https://g1.globo.com/dynamo/brasil/rss2.xml"),
            ("G1 Politica", "https://g1.globo.com/dynamo/politica/rss2.xml"),
        ],
    },
    "mundo": {
        "label": "🌍 Mundo, Guerras & Governos",
        "color": discord.Color.red(),
        "feeds": [
            ("BBC World", "http://feeds.bbci.co.uk/news/world/rss.xml"),
            ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ],
    },
}

TAG_RE = re.compile(r"<[^<]+?>")


def _fetch_feed_entries(name: str, url: str, cutoff: datetime) -> list[dict]:
    entries = []
    try:
        parsed = feedparser.parse(url)
    except Exception:
        log.exception("Erro ao buscar feed %s", name)
        return entries

    for entry in parsed.entries[:10]:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            published_dt = datetime(*published[:6], tzinfo=timezone.utc)
            if published_dt < cutoff:
                continue
        summary = TAG_RE.sub("", entry.get("summary", ""))[:300]
        link = entry.get("link", "")
        if not link:
            continue

        # Busca imagem em media:content, media:thumbnail ou enclosures (nem todo feed tem).
        img = None
        mc = entry.get("media_content")
        if isinstance(mc, list) and mc:
            img = mc[0].get("url")
        if not img:
            mt = entry.get("media_thumbnail")
            if isinstance(mt, list) and mt:
                img = mt[0].get("url")
        if not img:
            for enc in entry.get("enclosures", []):
                if enc.get("type", "").startswith("image/"):
                    img = enc.get("href")
                    break

        entries.append(
            {
                "title": entry.get("title", "Sem titulo"),
                "link": link,
                "summary": summary,
                "source": name,
                "image": img,
            }
        )
    return entries


def _collect_category_items(category: dict) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    items = []
    for name, url in category["feeds"]:
        items.extend(_fetch_feed_entries(name, url, cutoff))

    # Descarta noticias que ja foram mostradas recentemente (mesmo link), pra nao
    # repetir quando /noticias manual e o post automatico caem no mesmo dia.
    unposted_links = filter_unposted_links([it["link"] for it in items])
    seen = set()
    deduped = []
    for it in items:
        if it["link"] in unposted_links and it["link"] not in seen:
            seen.add(it["link"])
            deduped.append(it)

    return deduped[: NEWS_ITEMS_PER_CATEGORY * 2]


NEWS_SUMMARY_PROMPT = """Voce recebeu uma lista de noticias reais (titulo + resumo original, podem estar
em ingles) de uma categoria. Escolha as {n} mais relevantes e importantes. Para cada uma, traduza o
titulo para portugues (mantendo o sentido, sem inventar) e escreva um resumo curto em portugues (1-2
frases, direto, sem floreio, sem opiniao). Nao invente nada que nao esteja no texto original - se um
resumo original for vago, mantenha vago. Se o titulo/resumo original ja estiver em portugues, so
mantenha ou ajuste levemente. Responda EXATAMENTE nesse formato, uma linha por noticia escolhida, na
ordem de importancia, usando " ||| " como separador:

INDICE ||| titulo traduzido para portugues ||| resumo em portugues

Onde INDICE e o numero do item na lista abaixo (comecando em 0). Nao inclua mais nada alem dessas linhas,
sem numeracao extra, sem comentarios.

Noticias:
{items_text}"""


async def _summarize_category(items: list[dict]) -> list[dict]:
    if not items:
        return []
    items_text = "\n".join(
        f"{i}. [{it['source']}] {it['title']} - {it['summary']}" for i, it in enumerate(items)
    )
    prompt = NEWS_SUMMARY_PROMPT.format(n=min(NEWS_ITEMS_PER_CATEGORY, len(items)), items_text=items_text)

    async def _try_once() -> list[dict]:
        # max_tokens generoso: 4 itens com titulo+resumo traduzidos podem passar
        # facil de 800 tokens e ficar cortados no meio (resumo quebrado tipo "O").
        raw = await _complete([{"role": "user", "content": prompt}], temperature=0.4, max_tokens=1800)
        picked = []
        for line in raw.strip().splitlines():
            line = line.strip()
            parts = line.split("|||")
            if len(parts) != 3:
                continue
            idx_str, titulo_pt, resumo_pt = (p.strip() for p in parts)
            if not idx_str.isdigit():
                continue
            # Resumo suspeito demais curto costuma ser resposta cortada no meio -
            # melhor descartar esse item do que mostrar algo quebrado tipo "O".
            if len(resumo_pt) < 15:
                continue
            idx = int(idx_str)
            if 0 <= idx < len(items):
                item = dict(items[idx])
                item["title_pt"] = titulo_pt or item["title"]
                item["summary_pt"] = resumo_pt or item["summary"]
                picked.append(item)
        return picked

    for attempt in range(2):
        try:
            picked = await _try_once()
        except Exception:
            log.exception("Erro ao resumir noticias (tentativa %s)", attempt + 1)
            picked = []
        if picked:
            return picked[:NEWS_ITEMS_PER_CATEGORY]

    log.warning("Resumo de noticias falhou 2x, mostrando itens sem traducao")
    return items[:NEWS_ITEMS_PER_CATEGORY]


def build_item_embed(category: dict, item: dict) -> discord.Embed:
    titulo = item.get("title_pt") or item["title"]
    resumo = item.get("summary_pt") or item["summary"] or "(sem resumo disponivel)"
    embed = discord.Embed(
        title=titulo[:250],
        description=resumo[:400],
        url=item["link"],
        color=category["color"],
    )
    if item.get("image"):
        embed.set_thumbnail(url=item["image"])
    embed.set_footer(text=f"Fonte: {item['source']}")
    return embed


NEWS_INTRO_PROMPT = """Escreva UMA linha curta de abertura, com a sua personalidade (direta, sem
bajulacao, pode ter humor leve), pra introduzir o resumo diario de noticias que voce vai postar agora.
Nao inclua data nem as palavras "resumo" ou "noticias" no texto - so a frase de abertura em si. Varie o
estilo, evite soar generica ou repetitiva. Responda somente com essa linha, sem aspas."""

NEWS_INTRO_FALLBACKS = [
    "Vamo que vamo, direto ao ponto.",
    "Separei o que importou de verdade hoje.",
    "Nada de enrolacao, so o essencial.",
    "Bora ver no que o mundo se meteu hoje.",
]


async def build_news_intro() -> str:
    async with ai_lock:
        try:
            intro = await _complete([{"role": "user", "content": NEWS_INTRO_PROMPT}], temperature=0.9, max_tokens=60)
        except Exception:
            log.exception("Erro ao gerar introducao das noticias")
            return random.choice(NEWS_INTRO_FALLBACKS)
    intro = intro.strip().strip('"')
    return intro or random.choice(NEWS_INTRO_FALLBACKS)


async def build_news_digest() -> list[tuple[dict, list[discord.Embed]]]:
    loop = asyncio.get_event_loop()
    sections = []
    for category in NEWS_CATEGORIES.values():
        raw_items = await loop.run_in_executor(None, _collect_category_items, category)
        # So a chamada de IA fica dentro do lock global - o post automatico e um
        # /noticias manual rodando ao mesmo tempo nao devem martelar a API da NVIDIA
        # em paralelo (isso agrava 504s la e ja causou digest incompleto).
        async with ai_lock:
            curated = await _summarize_category(raw_items)
        embeds = [build_item_embed(category, item) for item in curated]
        if embeds:
            sections.append((category, embeds))
            await loop.run_in_executor(None, mark_news_posted, [it["link"] for it in curated])
    return sections


def find_news_channel(guild: discord.Guild) -> discord.TextChannel | None:
    for channel in guild.text_channels:
        if NEWS_CHANNEL_NAME in channel.name.lower():
            return channel
    return None


async def post_news_digest(channel: discord.TextChannel):
    placeholder = await channel.send(
        embed=thinking_embed(
            "📰 Buscando e resumindo as noticias do dia...", eta_seconds=NEWS_THINKING_ETA_SECONDS
        )
    )
    sections = await build_news_digest()
    if not sections:
        await placeholder.edit(
            content="Nao encontrei noticias relevantes nas ultimas horas, tento de novo mais tarde.",
            embed=None,
        )
        return
    today = datetime.now(NEWS_TIMEZONE).strftime("%d/%m/%Y")
    intro = await build_news_intro()
    
    header_embed = discord.Embed(description=f"**{intro}**", color=discord.Color.purple())
    header_embed.set_author(name=f"Resumo de Notícias — {today}", icon_url=channel.guild.me.display_avatar.url)
    
    await placeholder.edit(
        content=None, embed=header_embed
    )
    for category, embeds in sections:
        await channel.send(f"# {category['label']}")
        try:
            await channel.send(embeds=embeds, view=FeedbackView(category["label"]))
        except discord.HTTPException:
            log.exception("Erro ao enviar embeds da categoria %s", category["label"])
            await channel.send("(deu erro ao mostrar essa categoria, pulando pra proxima)")


class NewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.daily_news_task.start()

    def cog_unload(self):
        self.daily_news_task.cancel()

    @tasks.loop(time=NEWS_POST_TIME)
    async def daily_news_task(self):
        guild = self.bot.get_guild(ALLOWED_GUILD_ID)
        if guild is None:
            return
        channel = find_news_channel(guild)
        if channel is None:
            log.warning("Canal de noticias nao encontrado (procurando por '%s' no nome).", NEWS_CHANNEL_NAME)
            return
        try:
            await post_news_digest(channel)
        except Exception:
            log.exception("Erro ao postar resumo diario de noticias")
            await notify_owner_text(self.bot, "⚠️ O resumo diario de noticias falhou. Confere os logs.")

    @daily_news_task.before_loop
    async def before_daily_news_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="noticias", description="Manda um resumo de noticias agora")
    async def noticias(self, interaction: discord.Interaction):
        # A geracao pode demorar varios minutos (traducao de 5 categorias, as vezes
        # com retry por instabilidade da API da NVIDIA) - o token do webhook da
        # interacao expira em 15min, entao a partir daqui tudo vai direto pro canal
        # (channel.send/message.edit nao expiram) em vez de interaction.followup.
        await interaction.response.send_message(
            "📰 Preparando o resumo de noticias, ja chega no canal...", ephemeral=True
        )
        try:
            await post_news_digest(interaction.channel)
        except Exception:
            log.exception("Erro ao gerar resumo de noticias sob demanda")
            await interaction.channel.send("Deu erro ao gerar o resumo de noticias, tenta de novo.")


async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))
