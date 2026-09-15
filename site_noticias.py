"""Publica o resumo de noticias no site (rizu.is-a.dev).

O site e estatico: nao consulta a VM, nao tem backend e nao sabe nada da
Solenne. A ponte e o Cloudflare KV — ela grava a chave "atual", e a pagina de
noticias le pela funcao /api/noticias do proprio Pages.

Por que KV e nao um endpoint aqui: a VM tem 1GB de RAM e divide espaco com
outro container. Expor rota HTTP faria o trafego de visitantes competir com o
laco de eventos do bot, e o sintoma seria a Solenne atrasando mensagem no
Discord. Do jeito atual ela escreve uma vez por dia e ninguem que abre o site
chega perto daqui.

Por que KV e nao commit no repositorio (era assim ate set/2026): gravar
arquivo enchia o historico de "Noticias de 14/09" e obrigava o Cloudflare a
recompilar o site inteiro pra trocar 15KB de JSON. No KV a noticia troca no
instante da escrita.

Sem CF_API_TOKEN configurado, tudo aqui vira no-op: o resumo continua indo pro
Discord normalmente.
"""

import json
import logging
import re
from datetime import datetime

import httpx

from config import CF_ACCOUNT_ID, CF_API_TOKEN, CF_KV_NAMESPACE, NEWS_TIMEZONE

log = logging.getLogger("hermes-bot")

API = "https://api.cloudflare.com/client/v4"
CHAVE = "atual"
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


async def publicar(payload: dict) -> bool:
    """Grava o JSON no KV do site. Devolve se chegou a escrever.

    Nunca levanta: o resumo do Discord ja foi postado quando isso roda, e falhar
    aqui nao pode derrubar o que ja deu certo la.
    """
    if not (CF_API_TOKEN and CF_ACCOUNT_ID and CF_KV_NAMESPACE):
        log.info("Publicacao no site desligada (CF_API_TOKEN vazio).")
        return False

    url = (
        f"{API}/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces"
        f"/{CF_KV_NAMESPACE}/values/{CHAVE}"
    )
    corpo = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            r = await cliente.put(
                url,
                headers={
                    "Authorization": f"Bearer {CF_API_TOKEN}",
                    "User-Agent": "SolenneBot",
                },
                # O endpoint espera multipart com um campo "value", nao um JSON
                # no corpo — o conteudo e opaco pro KV. Passar por `files` faz o
                # httpx montar o multipart e o Content-Type sozinho.
                files={"value": (None, corpo)},
            )
            r.raise_for_status()
            log.info("Noticias publicadas no site (%d categorias).", len(payload.get("categorias", [])))
            return True

    except httpx.HTTPStatusError as e:
        # 401/403 quase sempre e token expirado ou sem Workers KV Storage:Edit.
        # 404 costuma ser id de conta ou de namespace trocado.
        log.error(
            "Falha ao publicar noticias no site: HTTP %s — %s",
            e.response.status_code,
            e.response.text[:200],
        )
    except Exception:
        log.exception("Falha inesperada ao publicar noticias no site")
    return False
