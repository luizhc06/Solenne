"""Publica o resumo de noticias no site (rizu.insurge.space).

O site e estatico: nao consulta a VM e nao sabe nada da Solenne. A ponte e uma
chamada que ela faz uma vez por dia: um PUT em /api/_kv/values/atual do proprio
site, com um token, e a pagina de noticias le pela rota /api/noticias.

Por que ela escreve no site e o site nao busca aqui: a VM tem 1GB de RAM e
divide espaco com outro container. Expor rota HTTP faria o trafego de
visitantes competir com o laco de eventos do bot, e o sintoma seria a Solenne
atrasando mensagem no Discord. Do jeito atual ela escreve uma vez por dia e
ninguem que abre o site chega perto daqui.

Por que nao commit no repositorio (era assim ate set/2026): gravar arquivo
enchia o historico de "Noticias de 14/09" e obrigava a recompilar o site
inteiro pra trocar 15KB de JSON. Assim a noticia troca no instante da escrita.

Sem SITE_TOKEN configurado, tudo aqui vira no-op: o resumo continua indo pro
Discord normalmente.
"""

import json
import logging
import re
from datetime import datetime

import httpx

from config import NEWS_TIMEZONE, SITE_TOKEN, SITE_URL

log = logging.getLogger("hermes-bot")

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
    if not SITE_TOKEN:
        log.info("Publicacao no site desligada (SITE_TOKEN vazio).")
        return False

    url = f"{SITE_URL}/api/_kv/values/{CHAVE}"
    corpo = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            r = await cliente.put(
                url,
                headers={
                    "Authorization": f"Bearer {SITE_TOKEN}",
                    "User-Agent": "SolenneBot",
                    "Content-Type": "application/json; charset=utf-8",
                },
                # O corpo e o JSON cru: o site guarda como esta.
                content=corpo.encode("utf-8"),
            )
            r.raise_for_status()
            log.info("Noticias publicadas no site (%d categorias).", len(payload.get("categorias", [])))
            return True

    except httpx.HTTPStatusError as e:
        # 401 e token diferente do arquivo storage/token do servidor (ou arquivo
        # ausente: ai a API do site fica desligada). 404 e SITE_URL errado.
        log.error(
            "Falha ao publicar noticias no site: HTTP %s — %s",
            e.response.status_code,
            e.response.text[:200],
        )
    except Exception:
        log.exception("Falha inesperada ao publicar noticias no site")
    return False
