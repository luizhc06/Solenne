import re
import asyncio
import random
import logging
import unicodedata
from datetime import datetime, timedelta, timezone, time as dtime

import httpx
import feedparser
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import ALLOWED_GUILD_ID, NEWS_TIMEZONE, ANILIST_USERNAME
from db import filter_unposted_links, mark_news_posted
from ai_client import ai_gate, _complete, complete_json, THINK_OFF
from utils import thinking_embed, NEWS_THINKING_ETA_SECONDS, TTLCache, truncate_words
from views import FeedbackView
from notify import notify_owner_text

log = logging.getLogger("hermes-bot")

NEWS_CHANNEL_NAME = "noticias"
NEWS_POST_TIME = dtime(hour=12, minute=0, tzinfo=NEWS_TIMEZONE)
NEWS_LOOKBACK_HOURS = 30
# 3 por categoria x 6 categorias = 18 cards. Era 4 (24 cards): o digest virava uma
# parede de rolagem e as manchetes menos relevantes diluiam as que importavam.
NEWS_ITEMS_PER_CATEGORY = 3
# Quantos candidatos a IA recebe pra escolher. Precisa ser bem maior que
# NEWS_ITEMS_PER_CATEGORY, senao ela nao tem de onde escolher e o "mais relevante"
# vira so "os primeiros do feed".
NEWS_CANDIDATES_PER_CATEGORY = NEWS_ITEMS_PER_CATEGORY * 4

# Limites do que sai no card. Titulo curto e o pedido central: o modelo tende a
# traduzir a manchete inteira, com subtitulo e aposto, e o embed virava um paragrafo
# em negrito. O prompt pede o corte e isso aqui garante.
NEWS_TITLE_MAX_CHARS = 90
NEWS_SUMMARY_MAX_CHARS = 300

# feedparser.parse(url) baixa por conta propria, com socket sem timeout - um feed
# lento pendura a thread do executor e trava o digest inteiro. Baixamos com httpx
# (que tem timeout) e entregamos os bytes ja prontos pro feedparser.
FEED_TIMEOUT_SECONDS = 12
FEED_USER_AGENT = "Mozilla/5.0 (compatible; SolenneBot/1.0; +https://github.com/luizhc06/Solenne)"

# Personalizacao da categoria "geek" com base no perfil de anime do dono no AniList.
# O usuario vem do config (env ANILIST_USERNAME), compartilhado com cogs/anime.py.
ANILIST_API_URL = "https://graphql.anilist.co"
ANILIST_CACHE_TTL_SECONDS = 12 * 60 * 60  # perfil nao muda de hora em hora
ANILIST_MIN_SCORE_FOR_HIGHLIGHT = 7

ANILIST_QUERY = """
query ($name: String) {
  MediaListCollection(userName: $name, type: ANIME, status_in: [CURRENT, COMPLETED, PLANNING]) {
    lists {
      entries {
        score
        media {
          title { romaji }
          genres
        }
      }
    }
  }
}
"""

_anilist_cache = TTLCache(ANILIST_CACHE_TTL_SECONDS)


def _fetch_anilist_interest_sync() -> str:
    """Resumo curto (generos favoritos + series bem avaliadas) do perfil AniList do
    dono, usado so pra dar contexto de curadoria na categoria Geek & Anime."""
    cached, hit = _anilist_cache.get(ANILIST_USERNAME)
    if hit:
        return cached

    try:
        resp = httpx.post(
            ANILIST_API_URL,
            json={"query": ANILIST_QUERY, "variables": {"name": ANILIST_USERNAME}},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception("Erro ao buscar perfil do AniList")
        return ""

    entries = []
    for lst in data.get("data", {}).get("MediaListCollection", {}).get("lists") or []:
        entries.extend(lst.get("entries") or [])

    interest = _summarize_anilist_entries(entries)
    _anilist_cache.set(ANILIST_USERNAME, interest)
    return interest


def _summarize_anilist_entries(entries: list[dict]) -> str:
    """Extrai generos favoritos e series bem avaliadas de uma lista de entries do
    AniList (formato bruto da API). Separado do fetch pra poder testar sem rede."""
    if not entries:
        return ""

    genre_counts: dict[str, int] = {}
    for entry in entries:
        for genre in (entry.get("media") or {}).get("genres") or []:
            genre_counts[genre] = genre_counts.get(genre, 0) + 1
    top_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)[:5]

    scored = [e for e in entries if (e.get("score") or 0) >= ANILIST_MIN_SCORE_FOR_HIGHLIGHT]
    scored.sort(key=lambda e: e.get("score", 0), reverse=True)
    top_titles = [
        e["media"]["title"]["romaji"]
        for e in scored[:8]
        if (e.get("media") or {}).get("title", {}).get("romaji")
    ]

    parts = []
    if top_genres:
        parts.append("generos favoritos: " + ", ".join(top_genres))
    if top_titles:
        parts.append("series que gosta/acompanha: " + ", ".join(top_titles))
    return "; ".join(parts)


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
        # O feed venturebeat.com/category/ai congelou em maio/2026 (mesmo caso do antigo
        # G1 Brasil: responde 200, mas so com materia velha). Trocado por TechCrunch AI.
        "feeds": [
            ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
            ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ],
    },
    "brasil": {
        "label": "🇧🇷 Brasil",
        "color": discord.Color.gold(),
        # ATENCAO: o antigo feed "dynamo/brasil/rss2.xml" responde 200 mas esta
        # congelado desde maio/2023 - todo item caia fora do cutoff e a categoria
        # Brasil virava 100% politica. Se o Brasil sumir de novo, checar a data do
        # item mais recente do feed antes de suspeitar do resto do pipeline.
        #
        # G1 Politica saiu (ago/2026) pelo mesmo motivo pelo qual o feed congelado
        # incomodava: com o G1 geral ja cheio de politica, a segunda fonte dobrava a
        # aposta e o "Brasil" do dia virava so Brasilia. G1 Economia foi conferido
        # com o mesmo volume e frescor (10 itens nas ultimas 30h).
        "feeds": [
            ("G1", "https://g1.globo.com/rss/g1/"),
            ("G1 Economia", "https://g1.globo.com/rss/g1/economia/"),
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
        resp = httpx.get(
            url,
            timeout=FEED_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": FEED_USER_AGENT},
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception:
        log.exception("Erro ao buscar feed %s (%s)", name, url)
        return entries

    if not parsed.entries:
        log.warning("Feed %s (%s) respondeu sem nenhuma entrada.", name, url)
        return entries

    for entry in parsed.entries[:10]:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        # Sem data: mantem o item (alguns feeds nao datam), mas ele fica no fim da
        # ordenacao por recencia em vez de disputar as primeiras posicoes.
        published_dt = None
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
                "published": published_dt,
            }
        )
    return entries


def interleave_by_source(per_feed: list[list[dict]]) -> list[dict]:
    """Intercala as listas de cada fonte em rodizio (1 de cada, repetindo), com cada
    fonte ja ordenada da mais recente pra mais antiga.

    Antes isso era um `extend` sequencial seguido de um corte no fim: como cada feed
    devolve ate 10 itens e o corte era em 8, a SEGUNDA fonte de cada categoria era
    descartada inteira antes da IA sequer ver. Rodizio garante que toda fonte
    configurada aparece na disputa.
    """
    ordered = [
        sorted(items, key=lambda it: it.get("published") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for items in per_feed
    ]
    merged = []
    for i in range(max((len(items) for items in ordered), default=0)):
        for items in ordered:
            if i < len(items):
                merged.append(items[i])
    return merged


def _collect_category_items(category: dict) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    per_feed = [_fetch_feed_entries(name, url, cutoff) for name, url in category["feeds"]]
    items = interleave_by_source(per_feed)

    # Descarta noticias que ja foram mostradas recentemente (mesmo link), pra nao
    # repetir quando /noticias manual e o post automatico caem no mesmo dia.
    unposted_links = filter_unposted_links([it["link"] for it in items])
    seen = set()
    deduped = []
    for it in items:
        if it["link"] in unposted_links and it["link"] not in seen:
            seen.add(it["link"])
            deduped.append(it)

    # Depois do filtro por link, tira tambem a mesma materia publicada por fontes
    # diferentes (links diferentes, fato identico) - antes as duas iam pro digest.
    return dedup_same_story(deduped)[:NEWS_CANDIDATES_PER_CATEGORY]


NEWS_SUMMARY_PROMPT = """Voce e a curadoria de noticias da Solenne. Abaixo esta uma lista numerada de
noticias reais de uma categoria (titulo + resumo original, podem estar em ingles).

Escolha as {n} MAIS relevantes e importantes, em ordem de importancia. Regras:
- Se duas entradas forem sobre o MESMO fato, use so uma delas (a de melhor resumo) e descarte a outra.
- Descarte o que nao for noticia de verdade (publicidade, "melhores ofertas", lista de cupom, promocao).
- "i": o numero EXATO do item na lista abaixo (comecando em 0).
- "eco": copie as 5 PRIMEIRAS palavras do titulo original desse item, sem traduzir e sem mudar nada.
- "titulo": o fato principal em portugues do Brasil, no MAXIMO {titulo_max} caracteres. Corte subtitulo,
  aposto, nome de fonte e explicacao - isso vai no resumo, nao no titulo.
- "resumo": 1 frase em portugues do Brasil, no MAXIMO {resumo_max} caracteres, so com o que esta no
  texto original. Se o resumo original for vago, mantenha vago - nunca complete com conhecimento proprio.
- Sem opiniao, sem floreio, sem emoji. Tudo em portugues do Brasil, menos o campo "eco".

Responda SOMENTE com um objeto JSON valido, sem cerca de codigo e sem nenhum texto em volta:
{{"noticias": [{{"i": 0, "eco": "...", "titulo": "...", "resumo": "..."}}]}}

Noticias:
{items_text}"""

# Palavras curtas demais pra distinguir uma materia de outra - so poluem a comparacao.
_DEDUP_MIN_WORD_LEN = 4
# Prefixo usado no lugar da palavra inteira pra "helicopters" casar com "helicopter"
# e "sancoes" com "sancao", sem precisar de stemmer de verdade.
_DEDUP_STEM_LEN = 6
# Ligacao e conectivo que aparecem em qualquer manchete: sao longos o bastante pra
# passar do filtro de tamanho, mas nao dizem nada sobre QUAL e o assunto.
_DEDUP_STOPWORDS = {
    "contra", "sobre", "entre", "apos", "para", "pelos", "pelas", "novo", "nova",
    "novos", "novas", "diz", "dizem", "afirm", "segund", "durant", "ainda", "mais",
    "after", "with", "from", "over", "into", "amid", "says", "said", "than", "that",
    "this", "their", "have", "will", "amid", "under", "about",
}
# Fracao das palavras significativas da manchete MENOR que precisa aparecer na outra.
_DEDUP_OVERLAP_THRESHOLD = 0.6
# ...E quantas palavras distintivas precisam coincidir em numero absoluto. So a fracao
# nao basta: "UE aprova sancoes contra a Russia" e "EUA impoem sancoes contra a Russia"
# batem 2 de 3 (0.67, ACIMA do limiar) sendo materias diferentes, enquanto a mesma
# materia de helicoptero contada por duas fontes bate 3 de 5 (0.6). O que separa os dois
# casos nao e a proporcao, e a quantidade de detalhe concreto em comum: duas versoes do
# mesmo fato compartilham varios substantivos especificos, nao so tema + pais.
_DEDUP_MIN_SHARED_WORDS = 3


def _significant_words(title: str) -> set[str]:
    sem_acento = unicodedata.normalize("NFKD", title.lower())
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    palavras = re.findall(r"[a-z0-9]+", sem_acento)
    stems = {p[:_DEDUP_STEM_LEN] for p in palavras if len(p) >= _DEDUP_MIN_WORD_LEN}
    return stems - _DEDUP_STOPWORDS


def same_story(title_a: str, title_b: str) -> bool:
    """Se duas manchetes cobrem o mesmo fato, mesmo escritas por veiculos diferentes."""
    a, b = _significant_words(title_a), _significant_words(title_b)
    if not a or not b:
        return False
    comuns = len(a & b)
    if comuns < _DEDUP_MIN_SHARED_WORDS:
        return False
    return comuns / min(len(a), len(b)) >= _DEDUP_OVERLAP_THRESHOLD


def dedup_same_story(items: list[dict]) -> list[dict]:
    """Tira materias repetidas ANTES da IA ver a lista.

    A dedup do banco so pega link identico, entao a mesma noticia publicada pela BBC e
    pela Al Jazeera passava como duas candidatas - e as duas apareciam no digest. Tirar
    antes tambem devolve espaco na lista de candidatos pra assuntos de verdade diferentes.
    """
    mantidos: list[dict] = []
    for item in items:
        if any(same_story(item["title"], mantido["title"]) for mantido in mantidos):
            continue
        mantidos.append(item)
    return mantidos


def _eco_bate(eco: str, titulo_original: str) -> bool:
    """Confere se o item que a IA descreveu e mesmo o item do indice que ela devolveu.

    Sem essa ancora, um indice trocado faz o card mostrar o titulo de uma noticia com o
    LINK e a imagem de outra - o erro mais grave possivel aqui, porque parece certo.
    Reproduzido na sonda contra a API real antes do campo "eco" existir.
    """
    a, b = _significant_words(eco), _significant_words(titulo_original)
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.5


def resolve_picked_item(pick: dict, items: list[dict]) -> dict | None:
    """Casa uma escolha da IA com o item real da lista, validando pelo eco do titulo.

    Se o indice nao bater com o eco, tenta achar por eco qual item ela quis dizer, em
    vez de descartar - e o mesmo conteudo, so o numero que saiu errado.
    """
    idx = pick.get("i")
    if isinstance(idx, str) and idx.strip().lstrip("-").isdigit():
        idx = int(idx)
    if not isinstance(idx, int):
        idx = None

    eco = (pick.get("eco") or "").strip()
    if idx is not None and 0 <= idx < len(items):
        if not eco or _eco_bate(eco, items[idx]["title"]):
            return items[idx]

    if eco:
        for item in items:
            if _eco_bate(eco, item["title"]):
                log.warning("Indice %s nao bateu com o eco %r, casei pelo titulo", idx, eco[:60])
                return item

    log.warning("Escolha descartada: indice %s e eco %r nao casaram com nenhum item", idx, eco[:60])
    return None


def build_curated_items(payload: dict, items: list[dict]) -> list[dict]:
    """Converte o JSON da IA na lista de itens prontos pro embed. Pura, testavel sem rede."""
    escolhas = payload.get("noticias")
    if not isinstance(escolhas, list):
        raise ValueError("JSON sem a lista 'noticias'")

    curados: list[dict] = []
    ja_usados: set[str] = set()
    for pick in escolhas:
        if not isinstance(pick, dict):
            continue
        titulo_pt = (pick.get("titulo") or "").strip()
        resumo_pt = (pick.get("resumo") or "").strip()
        # Resumo curto demais costuma ser resposta cortada no meio - melhor descartar
        # esse item do que mostrar algo quebrado tipo "O".
        if len(resumo_pt) < 15:
            continue

        item_original = resolve_picked_item(pick, items)
        if item_original is None or item_original["link"] in ja_usados:
            continue

        ja_usados.add(item_original["link"])
        item = dict(item_original)
        item["title_pt"] = truncate_words(titulo_pt, NEWS_TITLE_MAX_CHARS) or item["title"]
        item["summary_pt"] = truncate_words(resumo_pt, NEWS_SUMMARY_MAX_CHARS) or item["summary"]
        curados.append(item)
    return curados


async def _summarize_category(items: list[dict], interest_hint: str = "") -> list[dict]:
    if not items:
        return []
    items_text = "\n".join(
        f"{i}. [{it['source']}] {it['title']} - {it['summary']}" for i, it in enumerate(items)
    )
    prompt = NEWS_SUMMARY_PROMPT.format(
        n=min(NEWS_ITEMS_PER_CATEGORY, len(items)),
        titulo_max=NEWS_TITLE_MAX_CHARS,
        resumo_max=NEWS_SUMMARY_MAX_CHARS,
        items_text=items_text,
    )
    if interest_hint:
        prompt += (
            f"\n\nContexto extra sobre quem vai ler: perfil de anime no AniList - {interest_hint}. "
            "Ao escolher as noticias mais relevantes, priorize as relacionadas a esses generos ou "
            "series quando fizer sentido - mas noticias muito importantes fora desse perfil ainda "
            "podem entrar, nao force a conexao se nao houver."
        )

    for attempt in range(2):
        try:
            # JSON estrito (response_format) no lugar do formato "INDICE ||| titulo |||
            # resumo": o separador de texto quebrava sozinho (o modelo copiava o " - "
            # da lista de entrada) e derrubava a categoria inteira pro fallback sem
            # traducao. Medido contra a API de producao, o JSON saiu valido em 9/9.
            payload = await complete_json(prompt, max_tokens=1800)
            curated = build_curated_items(payload, items)
        except Exception:
            log.exception("Erro ao resumir noticias (tentativa %s)", attempt + 1)
            curated = []
        if curated:
            return curated[:NEWS_ITEMS_PER_CATEGORY]

    log.warning("Resumo de noticias falhou 2x, mostrando itens sem traducao")
    return items[:NEWS_ITEMS_PER_CATEGORY]


def build_item_embed(category: dict, item: dict) -> discord.Embed:
    titulo = item.get("title_pt") or item["title"]
    resumo = item.get("summary_pt") or item["summary"] or "(sem resumo disponivel)"
    embed = discord.Embed(
        # Corta na palavra em vez de no caractere: o corte seco em 250/400 deixava
        # titulo terminando no meio de uma palavra quando o fallback sem traducao entrava.
        title=truncate_words(titulo, NEWS_TITLE_MAX_CHARS),
        description=truncate_words(resumo, NEWS_SUMMARY_MAX_CHARS),
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


async def build_news_intro(interactive: bool = False) -> str:
    slot = ai_gate.interactive() if interactive else ai_gate.background()
    async with slot:
        try:
            intro = await _complete(
                [{"role": "user", "content": NEWS_INTRO_PROMPT}], max_tokens=120, thinking=THINK_OFF
            )
        except Exception:
            log.exception("Erro ao gerar introducao das noticias")
            return random.choice(NEWS_INTRO_FALLBACKS)
    intro = intro.strip().strip('"')
    return intro or random.choice(NEWS_INTRO_FALLBACKS)


async def build_news_digest(interactive: bool = False) -> tuple[list[tuple[dict, list[discord.Embed]]], list[str]]:
    """Retorna as secoes prontas e os rotulos das categorias que ficaram de fora.

    Cada categoria e isolada em try/except de proposito: antes, um erro em uma
    (feed fora do ar, banco travado, timeout da NVIDIA) derrubava a geracao inteira
    e o digest chegava truncado sem explicacao nenhuma.

    `interactive` diferencia o /noticias (alguem esperando na frente da tela) do post
    automatico do meio-dia, que cede a vez pra qualquer conversa em andamento.
    """
    loop = asyncio.get_event_loop()
    sections = []
    skipped = []
    for key, category in NEWS_CATEGORIES.items():
        try:
            raw_items = await loop.run_in_executor(None, _collect_category_items, category)
            interest_hint = ""
            if key == "geek":
                interest_hint = await loop.run_in_executor(None, _fetch_anilist_interest_sync)
            # So a chamada de IA fica dentro do portao global - o post automatico e um
            # /noticias manual rodando ao mesmo tempo nao devem martelar a API da NVIDIA
            # em paralelo (isso agrava 504s la e ja causou digest incompleto). Como o
            # portao e por categoria, uma menção no meio do digest espera no maximo uma
            # categoria, e nao o digest inteiro.
            async with (ai_gate.interactive() if interactive else ai_gate.background()):
                curated = await _summarize_category(raw_items, interest_hint)
        except Exception:
            log.exception("Erro ao montar a categoria %s", category["label"])
            skipped.append(category["label"])
            continue

        embeds = [build_item_embed(category, item) for item in curated]
        if not embeds:
            log.warning("Categoria %s ficou sem nenhum item.", category["label"])
            skipped.append(category["label"])
            continue

        sections.append((category, embeds))
        await loop.run_in_executor(None, mark_news_posted, [it["link"] for it in curated])
    return sections, skipped


def find_news_channel(guild: discord.Guild) -> discord.TextChannel | None:
    for channel in guild.text_channels:
        if NEWS_CHANNEL_NAME in channel.name.lower():
            return channel
    return None


async def post_news_digest(channel: discord.TextChannel, interactive: bool = False):
    placeholder = await channel.send(
        embed=thinking_embed(
            "📰 Buscando e resumindo as noticias do dia...", eta_seconds=NEWS_THINKING_ETA_SECONDS
        )
    )
    sections, skipped = await build_news_digest(interactive=interactive)
    if not sections:
        await placeholder.edit(
            content="Nao encontrei noticias relevantes nas ultimas horas, tento de novo mais tarde.",
            embed=None,
        )
        return
    today = datetime.now(NEWS_TIMEZONE).strftime("%d/%m/%Y")
    intro = await build_news_intro(interactive=interactive)
    
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

    # Diz o que faltou em vez de simplesmente omitir - categoria sumindo em silencio
    # e indistinguivel de "nao teve noticia hoje" pra quem esta lendo.
    if skipped:
        await channel.send(f"-# Sem novidade em: {', '.join(skipped)}.")


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
            await post_news_digest(interaction.channel, interactive=True)
        except Exception:
            log.exception("Erro ao gerar resumo de noticias sob demanda")
            await interaction.channel.send("Deu erro ao gerar o resumo de noticias, tenta de novo.")


async def setup(bot: commands.Bot):
    await bot.add_cog(NewsCog(bot))
