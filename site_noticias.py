"""Publica o resumo de noticias no repositorio do site.

O site (rizuw.pages.dev) e estatico: nao consulta a VM, nao tem backend e nao
sabe nada da Solenne. A ponte e um arquivo — ela grava src/data/noticias.json
no repositorio do site pela API do GitHub, o Cloudflare Pages percebe o commit
e recompila sozinho.

Por que arquivo e nao endpoint: a VM tem 1GB de RAM e divide espaco com outro
container. Expor rota HTTP faria o trafego de visitantes competir com o laco de
eventos do bot, e o sintoma seria a Solenne atrasando mensagem no Discord. Do
jeito atual ela escreve uma vez por dia e ninguem que abre o site chega perto
daqui.

De quebra a recompilacao diaria atualiza tambem a lista do AniList do site, que
e lida no build.

Sem SITE_REPO_TOKEN configurado, tudo aqui vira no-op: o resumo continua indo
pro Discord normalmente.
"""

import base64
import json
import logging
import re
from datetime import datetime

import httpx

from config import NEWS_TIMEZONE, SITE_REPO, SITE_REPO_TOKEN, SITE_ARQUIVO

log = logging.getLogger("hermes-bot")

API = "https://api.github.com"
TIMEOUT = 20.0

# Os rotulos do Discord vem com emoji ("🎌 Geek & Anime"). O site tem tipografia
# propria e nao usa emoji em titulo de secao, entao ele sai aqui.
_EMOJI_INICIAL = re.compile(r"^[^\w(]+", re.UNICODE)


def _rotulo_limpo(label: str) -> str:
    return _EMOJI_INICIAL.sub("", label).strip()


def _item_para_site(item: dict) -> dict:
    """So o que a pagina mostra — o resto do item e detalhe da curadoria."""
    return {
        "titulo": item.get("title_pt") or item.get("title") or "",
        "resumo": item.get("summary_pt") or item.get("summary") or "",
        "link": item.get("link") or "",
        "fonte": item.get("source") or "",
        "imagem": item.get("image") or None,
    }


def montar_payload(
    sections: list,
    sem_relevancia: list[str],
    falharam: list[str],
    abertura: str = "",
    destaque: dict | None = None,
    chaves_por_rotulo: dict[str, str] | None = None,
) -> dict:
    chaves = chaves_por_rotulo or {}
    categorias = []
    for category, curated, _embeds in sections:
        label = category.get("label", "")
        categorias.append({
            "chave": chaves.get(label, ""),
            "rotulo": _rotulo_limpo(label),
            "itens": [_item_para_site(it) for it in curated],
        })

    payload = {
        "gerado_em": datetime.now(NEWS_TIMEZONE).isoformat(timespec="seconds"),
        "abertura": abertura or "",
        "categorias": categorias,
        "sem_relevancia": [_rotulo_limpo(r) for r in sem_relevancia],
        "falharam": [_rotulo_limpo(r) for r in falharam],
    }
    if destaque:
        payload["destaque"] = {
            "titulo": destaque.get("title_pt") or destaque.get("title") or "",
            "link": destaque.get("link") or "",
        }
    return payload


async def _sha_atual(cliente: httpx.AsyncClient) -> tuple[str | None, str | None]:
    """Devolve (sha, conteudo) do arquivo que ja esta la, ou (None, None).

    O sha e obrigatorio pra sobrescrever: sem ele a API recusa, achando que e
    criacao de arquivo novo. O conteudo serve pra nao commitar igual.
    """
    r = await cliente.get(f"{API}/repos/{SITE_REPO}/contents/{SITE_ARQUIVO}")
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    dados = r.json()
    bruto = base64.b64decode(dados.get("content", "")).decode("utf-8", "replace")
    return dados.get("sha"), bruto


async def publicar(payload: dict) -> bool:
    """Grava o JSON no repositorio do site. Devolve se chegou a commitar.

    Nunca levanta: o resumo do Discord ja foi postado quando isso roda, e falhar
    aqui nao pode derrubar o que ja deu certo la.
    """
    if not SITE_REPO_TOKEN or not SITE_REPO:
        log.info("Publicacao no site desligada (SITE_REPO_TOKEN vazio).")
        return False

    corpo = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    cabecalhos = {
        "Authorization": f"Bearer {SITE_REPO_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SolenneBot",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=cabecalhos) as cliente:
            sha, anterior = await _sha_atual(cliente)

            # Commitar conteudo identico gera build no Cloudflare a toa. Compara
            # ignorando o campo de data, que muda toda execucao mesmo sem noticia nova.
            if anterior:
                try:
                    a = json.loads(anterior)
                    a.pop("gerado_em", None)
                    b = dict(payload)
                    b.pop("gerado_em", None)
                    if a == b:
                        log.info("Noticias do site inalteradas, nao commitei.")
                        return False
                except json.JSONDecodeError:
                    pass  # arquivo corrompido ou de exemplo: sobrescreve

            hoje = datetime.now(NEWS_TIMEZONE).strftime("%d/%m/%Y")
            envio = {
                "message": f"Noticias de {hoje}",
                "content": base64.b64encode(corpo.encode("utf-8")).decode("ascii"),
                "committer": {"name": "Solenne", "email": "solenne@users.noreply.github.com"},
            }
            if sha:
                envio["sha"] = sha

            r = await cliente.put(
                f"{API}/repos/{SITE_REPO}/contents/{SITE_ARQUIVO}", json=envio
            )
            r.raise_for_status()
            log.info("Noticias publicadas no site (%s).", SITE_ARQUIVO)
            return True

    except httpx.HTTPStatusError as e:
        # 401/403 quase sempre e token expirado ou sem permissao de Contents:write.
        log.error(
            "Falha ao publicar noticias no site: HTTP %s — %s",
            e.response.status_code,
            e.response.text[:200],
        )
    except Exception:
        log.exception("Falha inesperada ao publicar noticias no site")
    return False
