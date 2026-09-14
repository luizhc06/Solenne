import re
import html
import socket
import json
import asyncio
import logging
import ipaddress
import urllib.parse

import httpx
import discord
from discord import app_commands
from discord.ext import commands

import tools
from ai_client import ai_gate, _complete, THINK_LOW
from utils import thinking_embed, safe_edit_original
from views import FeedbackView

log = logging.getLogger("hermes-bot")

FETCH_TIMEOUT_SECONDS = 15
FETCH_MAX_BYTES = 2_000_000
# Quanto texto da pagina vai pro modelo. Passar a pagina inteira estoura o contexto
# e nao melhora o resumo - o comeco de um artigo ja carrega o essencial.
MAX_TEXT_CHARS = 12_000
FETCH_USER_AGENT = "Mozilla/5.0 (compatible; SolenneBot/1.0; +https://github.com/luizhc06/Solenne)"

SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


class UnsafeURLError(Exception):
    pass


def _resolve_safe_ip(hostname: str) -> str:
    """Resolve o hostname, barra IP privado/loopback/link-local/reservado e devolve
    o IP escolhido, pra quem for conectar de verdade pinar nesse mesmo IP.

    Devolver o IP (em vez de so validar e descartar) fecha a janela de TOCTOU: se a
    checagem so validasse o hostname e a conexao real resolvesse o DNS de novo depois,
    um atacante controlando o DNS poderia responder um IP publico na hora da checagem
    e um IP interno na hora da conexao.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise UnsafeURLError("Nao consegui resolver o endereco desse link.")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise UnsafeURLError("Esse endereco e interno, nao vou abrir.")
    return infos[0][4][0]


def ensure_public_http_url(url: str):
    """Recusa qualquer coisa que nao seja http(s) para um IP publico.

    A Solenne roda numa VM da Oracle Cloud, onde 169.254.169.254 serve o endpoint de
    metadados da instancia. Sem essa checagem, qualquer pessoa do servidor poderia
    mandar `/resumolink http://169.254.169.254/...` e receber de volta credenciais da
    VM resumidas num embed. Vale tambem pra 127.0.0.1 e pra rede interna.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError("So consigo abrir links http/https.")
    if not parsed.hostname:
        raise UnsafeURLError("Esse link nao tem um endereco valido.")

    _resolve_safe_ip(parsed.hostname)


def extract_text(raw_html: str) -> tuple[str, str]:
    """Devolve (titulo, texto limpo) a partir do HTML bruto."""
    title_match = TITLE_RE.search(raw_html)
    title = html.unescape(TAG_RE.sub("", title_match.group(1))).strip() if title_match else ""

    body = SCRIPT_STYLE_RE.sub(" ", raw_html)
    text = html.unescape(TAG_RE.sub(" ", body))
    return title, WHITESPACE_RE.sub(" ", text).strip()


MAX_REDIRECTS = 5


def _fetch_page_sync(url: str) -> tuple[str, str]:
    # follow_redirects=True do httpx nao revalida cada hop: um dominio publico
    # controlado por um atacante pode responder 302 pra um IP interno (ex.:
    # 169.254.169.254) e o httpx seguiria sem checar nada. Por isso o redirect e
    # seguido manualmente aqui, validando e pinando o IP a cada hop.
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise UnsafeURLError("Esse link nao tem um endereco valido.")

        ip = _resolve_safe_ip(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        pinned_host = f"[{ip}]" if ":" in ip else ip
        pinned_netloc = f"{pinned_host}:{port}"
        pinned_url = urllib.parse.urlunparse(parsed._replace(netloc=pinned_netloc))

        with httpx.stream(
            "GET",
            pinned_url,
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"User-Agent": FETCH_USER_AGENT, "Host": parsed.hostname},
            extensions={"sni_hostname": parsed.hostname},
        ) as resp:
            if resp.has_redirect_location:
                url = urllib.parse.urljoin(url, resp.headers["location"])
                continue

            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "html" not in content_type and "text" not in content_type:
                raise UnsafeURLError(f"Esse link nao e uma pagina de texto (e {content_type or 'desconhecido'}).")

            chunks = []
            total = 0
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= FETCH_MAX_BYTES:
                    break
            raw = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")

        return extract_text(raw)

    raise UnsafeURLError("Esse link tem redirecionamentos demais.")


LINK_SUMMARY_PROMPT = """Voce recebeu o texto extraido de uma pagina da web. Resuma em portugues,
baseando-se SOMENTE no que esta no texto - nunca complete com conhecimento proprio nem invente
detalhes que nao aparecem ali. Se o texto estiver truncado, incompleto ou for so menu/navegacao
sem conteudo de verdade, diga isso claramente em vez de inventar um resumo.

Formato: um paragrafo curto dizendo do que se trata, seguido de 3 a 5 bullets com os pontos
principais. Direto, sem floreio. Responda somente com o resumo.

Titulo da pagina: {title}
URL: {url}

Texto da pagina:
{text}"""


async def summarize_url(url: str) -> tuple[str, str]:
    loop = asyncio.get_event_loop()
    title, text = await loop.run_in_executor(None, _fetch_page_sync, url)
    if len(text) < 200:
        raise UnsafeURLError("Essa pagina nao tem texto suficiente pra resumir (talvez carregue por JavaScript).")

    prompt = LINK_SUMMARY_PROMPT.format(title=title or "(sem titulo)", url=url, text=text[:MAX_TEXT_CHARS])
    async with ai_gate.interactive():
        summary = await _complete(
            [{"role": "user", "content": prompt}], max_tokens=900, thinking=THINK_LOW
        )
    return title, summary


class LinkSummaryCog(commands.Cog):
    @app_commands.command(name="resumolink", description="Abre um link e resume o conteudo da pagina")
    @app_commands.describe(url="O link que voce quer que eu leia")
    async def resumolink(self, interaction: discord.Interaction, url: str):
        await interaction.response.send_message(embed=thinking_embed("🔗 Abrindo e lendo a pagina..."))

        try:
            title, summary = await summarize_url(url.strip())
        except UnsafeURLError as e:
            await safe_edit_original(interaction, content=str(e), embed=None)
            return
        except httpx.HTTPStatusError as e:
            await safe_edit_original(
                interaction,
                content=f"A pagina respondeu {e.response.status_code}, nao consegui ler.", embed=None
            )
            return
        except Exception:
            log.exception("Erro ao resumir link %s", url)
            await safe_edit_original(
                interaction,
                content="Deu erro ao abrir esse link, tenta de novo ou confere se ele esta certo.", embed=None
            )
            return

        embed = discord.Embed(
            title=(title or url)[:250],
            url=url,
            description=summary[:4000],
            color=discord.Color.teal(),
        )
        embed.set_footer(text="Resumo do conteudo real da pagina")
        await safe_edit_original(interaction, content=None, embed=embed, view=FeedbackView(title[:200] or url))


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkSummaryCog(bot))


@tools.register(
    name="resumir_link",
    description=(
        "Abre uma pagina da web e devolve o texto real dela. Use quando alguem mandar um "
        "link e quiser saber o que tem nele, ou quando precisar do conteudo de uma URL "
        "especifica que ja apareceu na conversa. Nao serve pra buscar - so pra abrir um "
        "endereco que voce ja tem."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL completa, comecando com http:// ou https://"}
        },
        "required": ["url"],
    },
)
async def tool_resumir_link(url: str) -> tools.ToolResult:
    loop = asyncio.get_event_loop()
    try:
        # Mesma validacao anti-SSRF do /resumolink: sem isso a Solenne viraria um jeito
        # de fazer o servidor buscar endereco interno so pedindo por chat.
        ensure_public_http_url(url)
        titulo, texto = await loop.run_in_executor(None, _fetch_page_sync, url)
    except UnsafeURLError as exc:
        return tools.ToolResult(json.dumps({"erro": str(exc)}, ensure_ascii=False))
    except Exception as exc:
        return tools.ToolResult(json.dumps({"erro": f"nao consegui abrir: {exc}"}, ensure_ascii=False))

    if len(texto) < 200:
        return tools.ToolResult(
            json.dumps({"erro": "pagina sem texto suficiente (talvez carregue por JavaScript)"}, ensure_ascii=False)
        )
    return tools.ToolResult(
        json.dumps({"titulo": titulo, "url": url, "texto": texto[:MAX_TEXT_CHARS]}, ensure_ascii=False)
    )
