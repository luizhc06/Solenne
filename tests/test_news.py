from datetime import datetime, timezone

import discord

from cogs.news import (
    _summarize_anilist_entries,
    build_category_embed,
    build_curated_items,
    dedup_same_story,
    flatten_curated,
    interleave_by_source,
    pick_destaque,
    resolve_picked_item,
    same_story,
)


def _item(source, link, hours_ago=None):
    published = None
    if hours_ago is not None:
        published = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc).replace(
            hour=12 - hours_ago
        )
    return {"source": source, "link": link, "published": published}


def test_interleave_alternates_between_sources():
    """Antes isso era um extend sequencial cortado no fim: como cada feed devolve ate
    10 itens e o corte era em 8, a segunda fonte da categoria era descartada inteira
    antes da IA ver. O rodizio garante que toda fonte configurada disputa vaga."""
    feed_a = [_item("A", f"a{i}", hours_ago=i) for i in range(5)]
    feed_b = [_item("B", f"b{i}", hours_ago=i) for i in range(5)]

    merged = interleave_by_source([feed_a, feed_b])

    assert [it["source"] for it in merged[:4]] == ["A", "B", "A", "B"]
    # Mesmo cortando em 4 candidatos, as duas fontes aparecem.
    assert {it["source"] for it in merged[:4]} == {"A", "B"}


def test_interleave_sorts_each_source_by_recency():
    feed = [_item("A", "old", hours_ago=5), _item("A", "new", hours_ago=1)]
    merged = interleave_by_source([feed])
    assert [it["link"] for it in merged] == ["new", "old"]


def test_interleave_handles_uneven_and_empty_feeds():
    feed_a = [_item("A", "a0", hours_ago=1), _item("A", "a1", hours_ago=2)]
    merged = interleave_by_source([feed_a, []])
    assert [it["link"] for it in merged] == ["a0", "a1"]
    assert interleave_by_source([]) == []
    assert interleave_by_source([[], []]) == []


def test_interleave_keeps_items_without_date_last():
    """Feed sem data nao pode furar a fila dos itens datados recentes."""
    feed = [_item("A", "sem-data"), _item("A", "recente", hours_ago=1)]
    merged = interleave_by_source([feed])
    assert [it["link"] for it in merged] == ["recente", "sem-data"]


def _news(titulo, link=None, source="BBC"):
    return {"title": titulo, "link": link or titulo.lower().replace(" ", "-"),
            "summary": "resumo original", "source": source, "image": None, "published": None}


HELICOPTERO_A = "Two crew members killed after firefighting helicopters collide in Greece"
HELICOPTERO_B = "Two crew die in Greek firefighting helicopter crash"


def test_same_story_reconhece_a_mesma_materia_em_fontes_diferentes():
    """Caso real capturado da sonda: BBC e Al Jazeera publicando o mesmo acidente com
    manchetes diferentes. Antes as duas entravam no digest como noticias distintas."""
    assert same_story(HELICOPTERO_A, HELICOPTERO_B) is True


def test_same_story_nao_junta_materias_so_parecidas():
    """Assunto proximo nao e o mesmo fato - juntar aqui seria pior que repetir."""
    assert same_story(
        "UE aprova novo pacote de sancoes contra a Russia",
        "EUA impoem novas sancoes contra a Russia",
    ) is False


def test_same_story_ignora_acento_e_caixa():
    assert same_story("Inundacoes deslocam 200 mil no Paquistao",
                      "INUNDAÇÕES DESLOCAM 200 MIL NO PAQUISTÃO") is True


def test_dedup_same_story_mantem_a_primeira_ocorrencia():
    items = [_news(HELICOPTERO_A, "a"), _news(HELICOPTERO_B, "b"), _news("EU agrees new sanctions package on Russia", "c")]
    restantes = dedup_same_story(items)
    assert [it["link"] for it in restantes] == ["a", "c"]


def test_resolve_picked_item_usa_o_indice_quando_o_eco_confirma():
    items = [_news("Israel and Hamas agree to extend ceasefire"), _news(HELICOPTERO_A)]
    escolha = {"i": 1, "eco": "Two crew members killed after"}
    assert resolve_picked_item(escolha, items)["title"] == HELICOPTERO_A


def test_resolve_picked_item_corrige_indice_trocado_pelo_eco():
    """O bug mais grave possivel aqui: a IA devolve o texto de uma noticia com o indice
    de outra, e o card sai com titulo certo, link e imagem errados - parecendo correto.
    Reproduzido contra a API real antes do campo "eco" existir."""
    items = [_news(HELICOPTERO_A), _news("Israel and Hamas agree to extend ceasefire")]
    escolha = {"i": 0, "eco": "Israel and Hamas agree to"}
    assert resolve_picked_item(escolha, items)["title"] == "Israel and Hamas agree to extend ceasefire"


def test_resolve_picked_item_descarta_quando_nada_casa():
    items = [_news(HELICOPTERO_A)]
    assert resolve_picked_item({"i": 9, "eco": "Completely unrelated headline here"}, items) is None


def test_resolve_picked_item_aceita_indice_como_string():
    """Mesmo com response_format, o modelo as vezes devolve "i": "2" em vez de 2."""
    items = [_news("a"), _news("b"), _news(HELICOPTERO_A)]
    assert resolve_picked_item({"i": "2", "eco": "Two crew members killed after"}, items)["title"] == HELICOPTERO_A


def test_build_curated_items_monta_os_campos_traduzidos():
    items = [_news(HELICOPTERO_A, "link-a")]
    payload = {"noticias": [{
        "i": 0, "eco": "Two crew members killed after",
        "titulo": "Dois tripulantes morrem em colisao de helicopteros na Grecia",
        "resumo": "Um dinamarques e um grego morreram na colisao perto de Atenas.",
    }]}
    curados = build_curated_items(payload, items)
    assert len(curados) == 1
    assert curados[0]["link"] == "link-a"
    assert curados[0]["title_pt"].startswith("Dois tripulantes")


def test_build_curated_items_corta_titulo_longo_na_palavra():
    items = [_news(HELICOPTERO_A, "link-a")]
    payload = {"noticias": [{
        "i": 0, "eco": "Two crew members killed after",
        "titulo": "Dois tripulantes morrem apos colisao de helicopteros de combate a incendios "
                  "na Grecia enquanto piloto britanico sobrevive ao acidente perto de Atenas",
        "resumo": "Um dinamarques e um grego morreram na colisao perto de Atenas.",
    }]}
    titulo = build_curated_items(payload, items)[0]["title_pt"]
    assert len(titulo) <= 90
    assert titulo.endswith("…")
    assert not titulo[:-1].endswith(" ")


def test_build_curated_items_descarta_resumo_cortado_no_meio():
    """Resumo minusculo e sinal de resposta truncada - melhor pular o item do que
    mostrar um card com a descricao "O"."""
    items = [_news(HELICOPTERO_A, "link-a")]
    payload = {"noticias": [{"i": 0, "eco": "Two crew members killed after", "titulo": "Titulo ok", "resumo": "O"}]}
    assert build_curated_items(payload, items) == []


def test_build_curated_items_nao_repete_o_mesmo_link():
    items = [_news(HELICOPTERO_A, "link-a")]
    escolha = {"i": 0, "eco": "Two crew members killed after", "titulo": "Titulo ok",
               "resumo": "Resumo com tamanho suficiente pra passar."}
    assert len(build_curated_items({"noticias": [escolha, dict(escolha)]}, items)) == 1


def test_build_curated_items_rejeita_payload_sem_lista():
    items = [_news(HELICOPTERO_A)]
    try:
        build_curated_items({"resultado": "nada"}, items)
    except ValueError:
        return
    raise AssertionError("payload sem 'noticias' deveria levantar ValueError")


def _curado(titulo_pt, link, titulo_original="Original headline here"):
    item = _news(titulo_original, link)
    item["title_pt"] = titulo_pt
    item["summary_pt"] = "resumo traduzido com tamanho suficiente."
    return item


def _secao(label, itens):
    return ({"label": label, "color": None}, itens, [])


def test_flatten_curated_mantem_a_ordem_do_digest():
    """O indice que a IA devolve pro destaque e posicao nessa lista achatada - se a
    ordem divergir da que foi mostrada pra ela, o destaque aponta pro card errado."""
    secoes = [
        _secao("🌍 Mundo", [_curado("Primeira", "a"), _curado("Segunda", "b")]),
        _secao("💻 Tech", [_curado("Terceira", "c")]),
    ]
    assert [it["title_pt"] for it in flatten_curated(secoes)] == ["Primeira", "Segunda", "Terceira"]


def test_flatten_curated_com_digest_vazio():
    assert flatten_curated([]) == []


def test_pick_destaque_casa_pelo_titulo_traduzido():
    """Na curadoria a IA le titulos originais em ingles; na escolha do destaque ela ja
    le os traduzidos. Comparar o eco contra o titulo errado faria todo eco falhar."""
    todas = [
        _curado("Israel e Hamas estendem cessar-fogo por 48h", "a"),
        _curado("Inundacoes deslocam 200 mil no Paquistao", "b"),
    ]
    payload = {"destaque_i": 1, "eco": "Inundacoes deslocam 200 mil no"}
    assert pick_destaque(payload, todas)["link"] == "b"


def test_pick_destaque_corrige_indice_trocado():
    todas = [_curado("Israel e Hamas estendem cessar-fogo", "a"), _curado("Inundacoes no Paquistao", "b")]
    payload = {"destaque_i": 0, "eco": "Inundacoes no Paquistao"}
    assert pick_destaque(payload, todas)["link"] == "b"


def test_pick_destaque_sem_noticia_nenhuma():
    assert pick_destaque({"destaque_i": 0, "eco": "qualquer"}, []) is None


def test_pick_destaque_descarta_escolha_que_nao_casa():
    """Melhor digest sem destaque do que destaque apontando pra materia errada."""
    todas = [_curado("Israel e Hamas estendem cessar-fogo", "a")]
    assert pick_destaque({"destaque_i": 7, "eco": "Assunto totalmente diferente disso"}, todas) is None


def test_build_curated_items_aceita_lista_vazia_de_proposito():
    """Dia fraco: a curadoria pode dizer "nao teve nada relevante". Isso e resposta
    valida, nao falha - antes a cota fixa de 3 tornava isso impossivel."""
    assert build_curated_items({"noticias": []}, [_news(HELICOPTERO_A)]) == []


def test_summarize_anilist_entries_empty():
    assert _summarize_anilist_entries([]) == ""


def test_summarize_anilist_entries_picks_top_genres():
    entries = [
        {"score": 9, "media": {"title": {"romaji": "Show A"}, "genres": ["Action", "Fantasy"]}},
        {"score": 8, "media": {"title": {"romaji": "Show B"}, "genres": ["Action", "Comedy"]}},
        {"score": 0, "media": {"title": {"romaji": "Show C"}, "genres": ["Slice of Life"]}},
    ]
    result = _summarize_anilist_entries(entries)
    assert "Action" in result
    assert "generos favoritos" in result


def test_summarize_anilist_entries_only_highlights_high_scores():
    entries = [
        {"score": 9, "media": {"title": {"romaji": "Loved Show"}, "genres": ["Drama"]}},
        {"score": 3, "media": {"title": {"romaji": "Meh Show"}, "genres": ["Drama"]}},
    ]
    result = _summarize_anilist_entries(entries)
    assert "Loved Show" in result
    assert "Meh Show" not in result


def test_summarize_anilist_entries_handles_missing_fields():
    entries = [{"score": 9, "media": {"title": {}, "genres": None}}]
    assert _summarize_anilist_entries(entries) == ""


_CATEGORIA_TESTE = {"label": "🎌 Geek & Anime", "color": discord.Color.blue()}


def test_categoria_embed_usa_titulo_cor_e_imagem_do_destaque():
    """Redesign de aparencia (18/08/2026, 2a rodada - usuario reportou "muito ruim de
    visualizar" com print do celular): 1 embed por categoria em vez de 1 cabecalho +
    1 embed por noticia. A imagem grande vem só do primeiro item (destaque)."""
    itens = [
        {"title": "Original 1", "title_pt": "Primeiro", "summary_pt": "resumo 1 traduzido",
         "summary": "s1", "link": "https://x.com/1", "source": "X", "image": "https://x.com/img.png"},
        {"title": "Original 2", "title_pt": "Segundo", "summary_pt": "resumo 2 traduzido",
         "summary": "s2", "link": "https://x.com/2", "source": "Y", "image": None},
    ]
    embed = build_category_embed(_CATEGORIA_TESTE, itens)

    assert embed.title == "🎌 Geek & Anime"
    assert embed.color == discord.Color.blue()
    assert embed.image.url == "https://x.com/img.png"
    assert len(embed.fields) == 2
    assert embed.fields[0].name == "⭐ Primeiro"
    assert "resumo 1 traduzido" in embed.fields[0].value
    assert "https://x.com/1" in embed.fields[0].value
    assert "Fonte: X" in embed.fields[0].value
    assert embed.fields[1].name == "Segundo"
    assert not embed.fields[1].name.startswith("⭐")


def test_categoria_embed_sem_imagem_nao_quebra():
    itens = [{"title": "Sem imagem", "summary": "resumo", "link": "https://x.com", "source": "X", "image": None}]
    assert build_category_embed(_CATEGORIA_TESTE, itens).image.url is None


def test_categoria_embed_lista_vazia_nao_quebra():
    embed = build_category_embed(_CATEGORIA_TESTE, [])
    assert embed.fields == []
    assert embed.image.url is None
