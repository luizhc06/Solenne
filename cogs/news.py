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
from ai_client import ai_gate, complete_json
from utils import (
    thinking_embed,
    TTLCache,
    truncate_sentences,
    truncate_words,
)
from views import FeedbackView
from notify import notify_owner_text
from site_noticias import montar_payload, publicar

log = logging.getLogger("hermes-bot")

NEWS_CHANNEL_NAME = "noticias"
NEWS_POST_TIME = dtime(hour=12, minute=0, tzinfo=NEWS_TIMEZONE)
NEWS_LOOKBACK_HOURS = 30
# TETO por categoria, nao cota fixa. Antes eram 3 cards sempre, em toda categoria, todo
# dia - o digest nao distinguia "hoje aconteceu algo grande" de "hoje nao aconteceu
# nada" e completava dia fraco com enchimento. Agora a IA devolve de 0 a 4 conforme a
# relevancia real, e categoria vazia e reportada como "nada que valesse a pena" em vez
# de virar tres manchetes mornas.
NEWS_MAX_ITEMS_PER_CATEGORY = 4
# Quantos candidatos a IA recebe pra escolher. Precisa ser bem maior que o teto,
# senao ela nao tem de onde escolher e o "mais relevante" vira so "os primeiros do feed".
NEWS_CANDIDATES_PER_CATEGORY = NEWS_MAX_ITEMS_PER_CATEGORY * 3

# Limites do que sai no card. Titulo curto e o pedido central: o modelo tende a
# traduzir a manchete inteira, com subtitulo e aposto, e o embed virava um paragrafo
# em negrito. O prompt pede o corte e isso aqui garante.
NEWS_TITLE_MAX_CHARS = 90
NEWS_SUMMARY_MAX_CHARS = 300

# Tentativas de curadoria por categoria, com pausa entre elas. Antes eram 2 seguidas,
# sem intervalo: um soluco de poucos segundos na API pegava as duas e a categoria caia
# pro fallback sem traducao. As retentativas de dentro do _complete cuidam do erro
# transitorio de rede; estas aqui cuidam de resposta que chegou mas veio inutil.
NEWS_SUMMARY_ATTEMPTS = 2
NEWS_SUMMARY_RETRY_DELAY_SECONDS = 5

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

# Jogos que o dono acompanha de perto (pedido explicito, 18/08/2026 - perfil real da
# Steam: Counter-Strike 2 e Rainbow Six Siege sao disparados os mais jogados, 1.368h e
# 460h respectivamente). Injetado junto do perfil AniList no contexto de curadoria da
# categoria Geek - prioriza SEM excluir o resto (a categoria continua "Geek & Anime",
# nao vira feed exclusivo de jogo especifico).
GEEK_JOGOS_ACOMPANHADOS = (
    "Counter-Strike 2, Rainbow Six Siege, jogos da Rockstar Games (GTA, Red Dead "
    "Redemption), Atomic Heart, Team Fortress 2, Deep Rock Galactic, Left 4 Dead, "
    "Apex Legends, Cyberpunk 2077, War Thunder"
)


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
        "foco": "anime, manga, jogos e cultura geek",
        "color": discord.Color.blue(),
        # PC Gamer adicionado em 18/08/2026 (pedido do usuario: mais noticia dos jogos
        # que ele acompanha) - os 2 feeds antigos sao so anime, "Geek & Anime" nunca
        # teve cobertura de jogo de verdade. Testado ao vivo antes de adicionar (feed
        # atualizado no mesmo dia). Ver GEEK_JOGOS_ACOMPANHADOS pra qual jogo priorizar.
        "feeds": [
            ("Anime News Network", "https://www.animenewsnetwork.com/newsfeed/rss.xml"),
            ("MyAnimeList", "https://myanimelist.net/rss/news.xml"),
            ("PC Gamer", "https://www.pcgamer.com/rss/"),
        ],
    },
    "tecnologia": {
        "label": "💻 Tecnologia & Hardware",
        # Achado do conselho de agentes (18/08/2026): sem uma barra explicita de
        # relevancia, a curadoria deixava passar review de produto de nicho e rumor
        # fraco so por estarem no feed. Agora o "foco" pede explicitamente pra ser
        # exigente - o TETO por categoria (NEWS_MAX_ITEMS_PER_CATEGORY) ja permite
        # devolver so 1 ou 0 itens num dia fraco, isso so reforça o criterio.
        "foco": (
            "hardware, componentes, PCs, consoles e a industria de tecnologia - SO o que "
            "tem impacto real: lancamento importante, mudanca real de mercado, avanco "
            "tecnico significativo. Descarte review de produto de nicho, rumor fraco/nao "
            "confirmado, e noticia de interesse so regional ou pequeno - preferio devolver "
            "menos itens (ou nenhum) do que encher com noticia morna"
        ),
        "color": discord.Color.dark_blue(),
        "feeds": [
            ("Tom's Hardware", "https://www.tomshardware.com/feeds/all"),
            ("Wccftech", "https://wccftech.com/feed/"),
        ],
    },
    "ciencia": {
        "label": "🔬 Ciencia",
        "foco": "descobertas cientificas, pesquisa, saude e medicina",
        "color": discord.Color.green(),
        "feeds": [
            ("ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"),
            ("Nature News", "https://www.nature.com/nature.rss"),
        ],
    },
    "ia": {
        "label": "🤖 Inteligencia Artificial",
        "foco": "inteligencia artificial: modelos, empresas de IA, pesquisa e regulacao do setor",
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
        # Achado do conselho de agentes (18/08/2026, pedido explicito do usuario):
        # politica de rotina virou maioria dos dias porque os feeds (G1 geral) sao
        # dominados por ela - a curadoria escolhia entre o que tinha, e o que tinha
        # era politica. Agora o foco pede explicitamente pra so entrar politica
        # quando for impacto real, nao debate/disputa do dia a dia de Brasilia.
        "foco": (
            "acontecimentos NO Brasil ou que afetam diretamente o Brasil. Noticia de outro "
            "pais que so foi publicada por um veiculo brasileiro NAO conta. Politica de "
            "ROTINA (embate entre politicos, disputa partidaria, declaracao de autoridade, "
            "movimentacao eleitoral comum) NAO e prioridade e deve ser descartada na "
            "maioria dos casos - so inclua politica se for algo de impacto real e direto na "
            "vida das pessoas (mudanca de lei que afeta o bolso, decisao economica "
            "relevante, crise institucional grave). Prefira economia, tecnologia, ciencia e "
            "fatos que afetam o dia a dia de verdade"
        ),
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
        "foco": "geopolitica, guerras, conflitos e governos fora do Brasil",
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

Esta categoria e sobre: {foco}. Descarte o que nao encaixa nesse foco, mesmo que seja noticia
importante - o feed as vezes traz assunto de fora e a materia provavelmente cabe em outra categoria.

Escolha ATE {n} das mais relevantes e importantes, em ordem de importancia. {n} e um TETO, nao uma
cota: seja exigente. Se so 1 ou 2 merecerem de verdade, devolva so 1 ou 2. Se NENHUMA for relevante
(so materia morna, fofoca, publicidade, lista de ofertas), devolva a lista vazia - dia fraco existe,
e dizer isso e melhor do que encher com noticia que ninguem quer ler. Regras:
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


def _titulo_original(item: dict) -> str:
    return item["title"]


def resolve_picked_item(pick: dict, items: list[dict], titulo_de=_titulo_original) -> dict | None:
    """Casa uma escolha da IA com o item real da lista, validando pelo eco do titulo.

    Se o indice nao bater com o eco, tenta achar por eco qual item ela quis dizer, em
    vez de descartar - e o mesmo conteudo, so o numero que saiu errado.

    `titulo_de` diz contra qual titulo comparar o eco, porque isso muda com o momento:
    na curadoria a IA le os titulos ORIGINAIS (em ingles) e ecoa deles; na escolha do
    destaque do dia ela ja le os titulos traduzidos. Comparar com o titulo errado faz
    todo eco falhar em silencio.
    """
    idx = pick.get("i")
    if isinstance(idx, str) and idx.strip().lstrip("-").isdigit():
        idx = int(idx)
    if not isinstance(idx, int):
        idx = None

    eco = (pick.get("eco") or "").strip()
    if idx is not None and 0 <= idx < len(items):
        if not eco or _eco_bate(eco, titulo_de(items[idx])):
            return items[idx]

    if eco:
        for item in items:
            if _eco_bate(eco, titulo_de(item)):
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


async def _summarize_category(items: list[dict], interest_hint: str = "", foco: str = "") -> list[dict]:
    if not items:
        return []
    items_text = "\n".join(
        f"{i}. [{it['source']}] {it['title']} - {it['summary']}" for i, it in enumerate(items)
    )
    prompt = NEWS_SUMMARY_PROMPT.format(
        n=min(NEWS_MAX_ITEMS_PER_CATEGORY, len(items)),
        foco=foco or "o assunto da categoria",
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

    for attempt in range(NEWS_SUMMARY_ATTEMPTS):
        try:
            # JSON estrito (response_format) no lugar do formato "INDICE ||| titulo |||
            # resumo": o separador de texto quebrava sozinho (o modelo copiava o " - "
            # da lista de entrada) e derrubava a categoria inteira pro fallback sem
            # traducao. Medido contra a API de producao, o JSON saiu valido em 9/9.
            payload = await complete_json(prompt, max_tokens=1800)
            escolhas_brutas = payload.get("noticias") or []
            curated = build_curated_items(payload, items)
        except Exception:
            log.exception("Erro ao resumir noticias (tentativa %s)", attempt + 1)
        else:
            # Lista vazia PEDIDA pela IA ("nao teve nada relevante hoje") e resposta
            # valida, nao falha - nao retenta nem cai pro fallback. Ja lista cheia que
            # ficou vazia depois da validacao (eco que nao casou, resumo truncado) e
            # falha de verdade e merece outra tentativa.
            if curated or not escolhas_brutas:
                return curated[:NEWS_MAX_ITEMS_PER_CATEGORY]
            log.warning("Nenhuma das %s escolhas passou na validacao", len(escolhas_brutas))
        if attempt < NEWS_SUMMARY_ATTEMPTS - 1:
            # Sem essa pausa as duas tentativas caiam dentro do mesmo soluco da API e
            # falhavam juntas - foi o que aconteceu em producao em 06/08/2026.
            await asyncio.sleep(NEWS_SUMMARY_RETRY_DELAY_SECONDS)

    log.warning("Resumo de noticias falhou 2x, mostrando itens sem traducao")
    return items[:NEWS_MAX_ITEMS_PER_CATEGORY]


def build_category_embed(category: dict, curated: list[dict]) -> discord.Embed:
    """UM embed por categoria, com cada noticia como field (18/08/2026, 2a rodada de
    ajuste de aparencia: a versao anterior mandava 1 embed de cabecalho + 1 embed por
    noticia na mesma mensagem - no Discord mobile isso virava uma parede de caixas
    repetidas (borda colorida + padding + rodape "Fonte" em cada uma), alem de imagem
    de miniatura ficando espremida do lado do texto em telas estreitas. Reportado pelo
    usuario com print do app: "muito ruim de visualizar, imagens bugadas, texto paia".

    Consolidar em 1 embed por categoria reduz pra 1 caixa colorida por categoria (em
    vez de ate 5), e usa só a imagem do destaque como imagem grande do embed inteiro -
    sem miniatura por item, que era o ponto mais estreito/espremido no celular."""
    embed = discord.Embed(title=category["label"], color=category["color"])
    if curated and curated[0].get("image"):
        embed.set_image(url=curated[0]["image"])
    for i, item in enumerate(curated):
        titulo = item.get("title_pt") or item["title"]
        resumo = item.get("summary_pt") or item["summary"] or "(sem resumo disponivel)"
        estrela = "⭐ " if i == 0 else ""
        # Corta na palavra em vez de no caractere: o corte seco em 250/400 deixava
        # titulo terminando no meio de uma palavra quando o fallback sem traducao entrava.
        nome = estrela + truncate_words(titulo, NEWS_TITLE_MAX_CHARS)
        valor = (
            f"{truncate_words(resumo, NEWS_SUMMARY_MAX_CHARS)}\n"
            f"[Ler mais]({item['link']}) · *Fonte: {item['source']}*"
        )
        embed.add_field(name=nome, value=valor, inline=False)
    return embed


# A abertura antiga era escrita ANTES da curadoria: o prompt nao recebia noticia
# nenhuma, entao so dava pra pedir uma frase generica ("bora ver no que o mundo se meteu
# hoje"). Era a razao principal do digest soar vazio - a Solenne tem voz em todo canto,
# menos justamente no que ela entrega todo dia. Agora ela le o que foi selecionado antes
# de abrir a boca, e aponta o que mais importa.
NEWS_INTRO_PROMPT = """Voce e Solenne e vai postar agora o resumo do dia no Discord. Estas sao as
noticias que a sua curadoria ja selecionou:

{manchetes}

Primeiro escolha qual e O destaque do dia entre as noticias acima: a que voce acha mais importante.

Depois escreva a abertura desse resumo com a SUA personalidade: direta, sem bajulacao, humor leve
quando couber, zero tom de telejornal.

Regras duras da abertura:
- Fale SOMENTE do destaque que voce escolheu. Voce NAO pode citar nenhum fato que nao esteja na
  lista acima - nada de trazer assunto de fora, nem de memoria, nem inventado.
- UM assunto so. NAO faca lista, NAO cite varias manchetes de enfiada, NAO use "de X a Y, passando
  por Z". Isso soa a locutor, nao a voce.
- Uma ou duas frases curtas, no maximo 200 caracteres, terminando em ponto final.
- Comente o assunto de verdade (o que voce achou dele) em vez de anunciar que existe um resumo.
- Se o destaque for tragedia (morte, violencia, desastre), largue o humor e seja sobria.
- Nao inclua data, nao use as palavras "resumo" ou "noticias", nao use emoji (essa regra e
  so pra sua frase - o campo "Destaque do dia" que o codigo monta depois usa icone de UI
  normalmente, ver comentario em post_news_digest).

Responda SOMENTE com JSON valido, com os campos NESTA ordem - escolha o destaque ANTES de escrever:
{{"destaque_i": 0, "eco": "as 5 primeiras palavras do titulo do destaque", "abertura": "..."}}"""

NEWS_INTRO_FALLBACKS = [
    "Vamo que vamo, direto ao ponto.",
    "Separei o que importou de verdade hoje.",
    "Nada de enrolacao, so o essencial.",
    "Bora ver no que o mundo se meteu hoje.",
]

NEWS_INTRO_MAX_CHARS = 220


def flatten_curated(sections: list[tuple[dict, list[dict], list[discord.Embed]]]) -> list[dict]:
    """Lista unica com todas as noticias do digest, pra IA escolher o destaque do dia."""
    return [item for _, curated, _ in sections for item in curated]


def titulo_exibido(item: dict) -> str:
    """O titulo que o card mostra - traduzido quando houve traducao."""
    return item.get("title_pt") or item["title"]


def pick_destaque(payload: dict, todas: list[dict]) -> dict | None:
    """Resolve qual noticia a IA elegeu como destaque, validando pelo eco.

    Mesma ancora da curadoria e pelo mesmo motivo: indice sozinho ja saiu trocado
    contra a API real, e destaque errado aponta o leitor pra materia errada.
    """
    if not todas:
        return None
    escolha = {"i": payload.get("destaque_i"), "eco": payload.get("eco")}
    return resolve_picked_item(escolha, todas, titulo_de=titulo_exibido)


async def build_news_intro(
    sections: list[tuple[dict, list[dict], list[discord.Embed]]], interactive: bool = False
) -> tuple[str, dict | None]:
    """Devolve (abertura na voz dela, noticia de destaque do dia)."""
    todas = flatten_curated(sections)
    if not todas:
        return random.choice(NEWS_INTRO_FALLBACKS), None

    # Mesma ordem de flatten_curated, pra o indice que a IA devolver bater com `todas`.
    rotulos = [categoria["label"] for categoria, curated, _ in sections for _ in curated]
    manchetes = "\n".join(
        f"{i}. [{rotulo}] {titulo_exibido(item)}"
        for i, (rotulo, item) in enumerate(zip(rotulos, todas))
    )
    prompt = NEWS_INTRO_PROMPT.format(manchetes=manchetes)

    slot = ai_gate.interactive() if interactive else ai_gate.background()
    async with slot:
        try:
            payload = await complete_json(prompt, max_tokens=600)
        except Exception:
            log.exception("Erro ao gerar introducao das noticias")
            return random.choice(NEWS_INTRO_FALLBACKS), None

    abertura = (payload.get("abertura") or "").strip().strip('"')
    abertura = truncate_sentences(abertura, NEWS_INTRO_MAX_CHARS) or random.choice(NEWS_INTRO_FALLBACKS)
    return abertura, pick_destaque(payload, todas)


async def build_news_digest(interactive: bool = False):
    """Retorna (secoes, categorias que falharam, categorias sem nada relevante).

    Cada secao e uma tupla (categoria, itens curados, embeds) - os itens vao junto
    porque a abertura do digest precisa ler o que foi selecionado pra escolher o
    destaque do dia.

    Cada categoria e isolada em try/except de proposito: antes, um erro em uma
    (feed fora do ar, banco travado, timeout da NVIDIA) derrubava a geracao inteira
    e o digest chegava truncado sem explicacao nenhuma.

    Falha e "dia fraco" sao listas separadas de proposito: as duas somem do digest, mas
    uma e problema e a outra e informacao legitima, e juntar as duas escondia bug.

    `interactive` diferencia o /noticias (alguem esperando na frente da tela) do post
    automatico do meio-dia, que cede a vez pra qualquer conversa em andamento.
    """
    loop = asyncio.get_event_loop()
    sections = []
    skipped = []
    sem_relevancia = []
    for key, category in NEWS_CATEGORIES.items():
        try:
            raw_items = await loop.run_in_executor(None, _collect_category_items, category)
            interest_hint = ""
            if key == "geek":
                anilist_hint = await loop.run_in_executor(None, _fetch_anilist_interest_sync)
                interest_hint = (
                    f"{anilist_hint} Jogos que acompanha de perto: {GEEK_JOGOS_ACOMPANHADOS}."
                    if anilist_hint
                    else f"Jogos que acompanha de perto: {GEEK_JOGOS_ACOMPANHADOS}."
                )
            # So a chamada de IA fica dentro do portao global - o post automatico e um
            # /noticias manual rodando ao mesmo tempo nao devem martelar a API da NVIDIA
            # em paralelo (isso agrava 504s la e ja causou digest incompleto). Como o
            # portao e por categoria, uma menção no meio do digest espera no maximo uma
            # categoria, e nao o digest inteiro.
            async with (ai_gate.interactive() if interactive else ai_gate.background()):
                curated = await _summarize_category(
                    raw_items, interest_hint, foco=category.get("foco", "")
                )
        except Exception:
            log.exception("Erro ao montar a categoria %s", category["label"])
            skipped.append(category["label"])
            continue

        if not curated:
            # Nao e erro: a curadoria olhou os candidatos e nao achou nada que merecesse
            # o card. Antes isso era impossivel (a cota fixa sempre preenchia).
            log.info("Categoria %s sem nada relevante hoje.", category["label"])
            sem_relevancia.append(category["label"])
            continue

        sections.append((category, curated, [build_category_embed(category, curated)]))
        await loop.run_in_executor(None, mark_news_posted, [it["link"] for it in curated])
    return sections, skipped, sem_relevancia


def find_news_channel(guild: discord.Guild) -> discord.TextChannel | None:
    for channel in guild.text_channels:
        if NEWS_CHANNEL_NAME in channel.name.lower():
            return channel
    return None


async def post_news_digest(channel: discord.TextChannel, interactive: bool = False):
    placeholder = await channel.send(
        embed=thinking_embed("📰 Buscando e resumindo as noticias do dia...")
    )
    sections, skipped, sem_relevancia = await build_news_digest(interactive=interactive)
    if not sections:
        await placeholder.edit(
            content="Nao encontrei noticias relevantes nas ultimas horas, tento de novo mais tarde.",
            embed=None,
        )
        return
    today = datetime.now(NEWS_TIMEZONE).strftime("%d/%m/%Y")
    intro, destaque = await build_news_intro(sections, interactive=interactive)

    header_embed = discord.Embed(description=f"**{intro}**", color=discord.Color.purple())
    header_embed.set_author(name=f"Resumo de Notícias — {today}", icon_url=channel.guild.me.display_avatar.url)
    if destaque:
        # O emoji aqui e icone de UI do embed, nao "fala" da Solenne - a regra de "nao use
        # emoji" do NEWS_INTRO_PROMPT vale so pra frase que ela escreve (`intro` acima),
        # igual ao restante dos embeds do bot (/help, cards de categoria). Ver SYSTEM_PROMPT.
        header_embed.add_field(
            name="📌 Destaque do dia",
            value=f"[{truncate_words(titulo_exibido(destaque), NEWS_TITLE_MAX_CHARS)}]({destaque['link']})",
            inline=False,
        )

    await placeholder.edit(
        content=None, embed=header_embed
    )
    for category, _curated, embeds in sections:
        try:
            await channel.send(embed=embeds[0], view=FeedbackView(category["label"]))
        except discord.HTTPException:
            log.exception("Erro ao enviar embed da categoria %s", category["label"])
            await channel.send("(deu erro ao mostrar essa categoria, pulando pra proxima)")

    # Diz o que faltou em vez de simplesmente omitir - categoria sumindo em silencio
    # e indistinguivel de "nao teve noticia hoje" pra quem esta lendo. Dia fraco e falha
    # aparecem separados: um e curadoria funcionando, o outro e coisa pra investigar.
    if sem_relevancia:
        await channel.send(f"-# Nada que valesse a pena em: {', '.join(sem_relevancia)}.")
    if skipped:
        await channel.send(f"-# Deu erro em: {', '.join(skipped)}.")

    # O site recebe o mesmo conteudo que acabou de ir pro Discord. Fica por ultimo
    # e engole o proprio erro de proposito: o resumo aqui ja foi entregue, e falhar
    # na publicacao nao pode desfazer isso nem sujar o canal com aviso tecnico.
    try:
        chaves = {cat["label"]: chave for chave, cat in NEWS_CATEGORIES.items()}
        await publicar(
            montar_payload(
                sections,
                sem_relevancia,
                skipped,
                abertura=intro,
                destaque=destaque,
                chaves_por_rotulo=chaves,
            )
        )
    except Exception:
        log.exception("Falha ao publicar o resumo no site")


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
