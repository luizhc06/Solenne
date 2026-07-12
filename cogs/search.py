import asyncio
import re
import html
import logging
import urllib.parse

import httpx
import discord
from discord import app_commands
from discord.ext import commands

from db import save_message
from ai_client import _complete
from utils import thinking_embed
from views import FeedbackView

log = logging.getLogger("hermes-bot")

SEARCH_MIN_SOURCES = 5
SEARCH_MAX_SOURCES = 8

DDG_RESULT_RE = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
DDG_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
TAG_RE = re.compile(r"<[^<]+?>")


def _clean_html_text(raw: str) -> str:
    return html.unescape(TAG_RE.sub("", raw)).strip()


def _extract_real_url(ddg_href: str) -> str:
    full = ddg_href if ddg_href.startswith("http") else "https:" + ddg_href
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(full).query)
    if "uddg" in qs:
        return qs["uddg"][0]
    return full


def _web_search_sync(query: str, max_results: int = SEARCH_MAX_SOURCES) -> list[dict]:
    try:
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception:
        log.exception("Erro ao pesquisar '%s'", query)
        return []

    titles = DDG_RESULT_RE.findall(resp.text)
    snippets = DDG_SNIPPET_RE.findall(resp.text)

    results = []
    for i, (href, title_raw) in enumerate(titles[:max_results]):
        title = _clean_html_text(title_raw)
        url = _extract_real_url(href)
        snippet = _clean_html_text(snippets[i]) if i < len(snippets) else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


SEARCH_SYNTHESIS_PROMPT = """Voce recebeu resultados reais de uma busca na web sobre uma pergunta.
Baseie sua resposta SOMENTE no conteudo desses resultados - nunca invente informacao que nao esteja
neles. Se os resultados nao derem informacao suficiente, diga isso claramente em vez de completar
com achismo.

Escreva uma resposta objetiva em portugues (paragrafos curtos ou lista), citando as fontes usando
[1], [2] etc conforme o numero do resultado, no ponto onde aquela informacao foi usada. Nao repita a
lista de fontes no final, isso e adicionado separadamente. Responda somente com o texto da resposta.

Pergunta: {query}

Resultados da busca:
{results_text}"""


async def _synthesize_search(query: str, results: list[dict]) -> str:
    results_text = "\n".join(
        f"[{i + 1}] {r['title']} - {r['snippet']}" for i, r in enumerate(results)
    )
    prompt = SEARCH_SYNTHESIS_PROMPT.format(query=query, results_text=results_text)
    return await _complete([{"role": "user", "content": prompt}], temperature=0.4)


def build_search_embed(results: list[dict]) -> discord.Embed:
    embed = discord.Embed(color=discord.Color.light_grey())
    embed.set_author(name="Fontes utilizadas", icon_url="https://duckduckgo.com/favicon.ico")
    fontes = "\n".join(f"**[{i + 1}]** [{r['title'][:80]}]({r['url']})" for i, r in enumerate(results))
    embed.description = fontes[:4000]
    return embed


# Gatilho de busca automatica em chat livre: restrito a variacoes de "pesquis-"
# (pesquise, pesquisa, pesquisar) de proposito, pra nao disparar em palavras do
# dia a dia tipo "buscar"/"procurar" e evitar estourar contexto/armazenamento
# com buscas nao intencionais.
SEARCH_TRIGGER_RE = re.compile(r"\b(pesquis\w*|busc\w*|procur\w*)\b", re.IGNORECASE)


def wants_web_search(content: str) -> bool:
    return bool(SEARCH_TRIGGER_RE.search(content))


async def auto_search_reply(
    query: str, author_name: str, author_id: int, channel_id: int
) -> tuple[str | None, discord.Embed | None]:
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _web_search_sync, query)
    if len(results) < SEARCH_MIN_SOURCES:
        return (
            f"So encontrei {len(results)} fonte(s) confiavel(is) pra isso, menos do que o "
            "minimo de 5. Tenta reformular.",
            None,
        )
    answer = await _synthesize_search(query, results)
    embed = build_search_embed(results)
    # Guarda so a pergunta e um resumo curto na memoria, nao os resultados brutos da
    # busca - evita inchar o banco e o contexto de conversas futuras nesse canal.
    await loop.run_in_executor(None, save_message, channel_id, "user", author_name, query)
    await loop.run_in_executor(None, save_message, channel_id, "assistant", None, answer[:500])
    # Content de mensagem do Discord tem limite de 2000 chars (diferente do embed,
    # que aguentava ate 4096) - trunca aqui pra nao estourar HTTPException no caller.
    return (answer[:2000], embed)


class SearchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="pesquisa", description="Pesquisa na web (minimo 5 fontes) e resume com links")
    @app_commands.describe(termo="O que voce quer pesquisar")
    async def pesquisa(self, interaction: discord.Interaction, termo: str):
        await interaction.response.send_message(
            embed=thinking_embed(f"🔎 Pesquisando sobre \"{termo}\"...", eta_seconds=20)
        )
        loop = asyncio.get_event_loop()

        results = await loop.run_in_executor(None, _web_search_sync, termo)
        if len(results) < SEARCH_MIN_SOURCES:
            await interaction.edit_original_response(
                content=(
                    f"So encontrei {len(results)} fonte(s) confiavel(is) pra isso, "
                    "menos do que o minimo de 5. Tenta reformular a pesquisa."
                ),
                embed=None,
            )
            return

        try:
            answer = await _synthesize_search(termo, results)
        except Exception:
            log.exception("Erro ao sintetizar pesquisa sobre '%s'", termo)
            await interaction.edit_original_response(
                content="Encontrei fontes mas deu erro ao resumir, tenta de novo.", embed=None
            )
            return

        embed = build_search_embed(results)
        await interaction.edit_original_response(content=answer[:2000], embed=embed, view=FeedbackView(termo))


async def setup(bot: commands.Bot):
    await bot.add_cog(SearchCog(bot))
