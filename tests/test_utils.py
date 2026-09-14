from utils import (
    THINKING_GIF_URL,
    looks_like_question,
    thinking_embed,
    mentions_solenne,
    split_discord_message,
    truncate_sentences,
    truncate_words,
    TTLCache,
)


def test_mentions_solenne_pelo_nome_no_meio_da_frase():
    """O gesto mais natural - chamar pelo nome - era justamente o que nao funcionava:
    sem @ e sem "?", a mensagem nao passava por nenhum gatilho e ela ficava muda."""
    assert mentions_solenne("solenne o que voce acha disso") is True
    assert mentions_solenne("Ei Solenne, me ajuda aqui") is True
    assert mentions_solenne("soleninha me explica") is True


def test_mentions_solenne_ignora_palavra_parecida():
    assert mentions_solenne("insolente do caramba") is False
    assert mentions_solenne("que sol quente hoje") is False
    assert mentions_solenne("") is False


def test_mentions_solenne_ignora_nome_dentro_de_url():
    """Colar o link do repo no chat nao pode acordar a Solenne."""
    assert mentions_solenne("olha https://github.com/luizhc06/Solenne") is False


def test_looks_like_question_aceita_pergunta_sem_interrogacao():
    """Quase ninguem digita "?" no Discord - exigir o literal fazia o modo ambiente
    perder a maioria das perguntas de verdade."""
    assert looks_like_question("alguem sabe se vai chover amanha") is True
    assert looks_like_question("qual o melhor mouse ate 200 reais") is True
    assert looks_like_question("me explica como funciona isso ai") is True


def test_looks_like_question_ainda_ignora_afirmacao():
    assert looks_like_question("acabei de comprar um teclado novo") is False


def test_truncate_words_nao_corta_no_meio_da_palavra():
    texto = "Dois tripulantes morrem em colisao de helicopteros de combate a incendios"
    cortado = truncate_words(texto, 40)
    assert len(cortado) <= 40
    assert cortado.endswith("…")
    assert cortado[:-1].strip() in texto


def test_truncate_sentences_corta_no_fim_da_frase():
    """A abertura do digest saiu cortada em "Tudo ao…" no teste contra o modelo real -
    reticencias no meio de uma fala dela fica so quebrado, diferente de um titulo."""
    texto = "A Amazon pediu pra reduzir uso de EC2. Isso diz muito sobre o custo da IA. E tem mais coisa."
    cortado = truncate_sentences(texto, 70)
    assert cortado == "A Amazon pediu pra reduzir uso de EC2. Isso diz muito sobre o custo da IA."[:len(cortado)]
    assert cortado.endswith(".")
    assert "…" not in cortado


def test_truncate_sentences_deixa_texto_curto_intacto():
    assert truncate_sentences("Uma frase curta.", 200) == "Uma frase curta."


def test_truncate_sentences_cai_pro_corte_por_palavra_sem_pontuacao():
    """Se nem a primeira frase couber, melhor reticencias do que devolver vazio."""
    cortado = truncate_sentences("palavra " * 40, 50)
    assert len(cortado) <= 50
    assert cortado.endswith("…")


def test_truncate_words_deixa_texto_curto_intacto():
    assert truncate_words("Titulo curto", 90) == "Titulo curto"


def test_split_discord_message_prefere_quebra_de_linha():
    texto = "x" * 50 + "\n" + "y" * 100
    partes = split_discord_message(texto, limit=60)
    assert partes[0] == "x" * 50
    assert partes[1].startswith("y")
    assert all(len(p) <= 60 for p in partes)


def test_split_discord_message_ignora_quebra_cedo_demais():
    """Quebrar num "\\n" logo no comeco desperdicaria o resto do limite e picotaria a
    resposta em mensagens minusculas - so vale a pena depois da metade."""
    partes = split_discord_message("titulo\n" + "x" * 100, limit=60)
    assert len(partes[0]) == 60


def test_split_discord_message_nunca_devolve_pedaco_vazio():
    """Mandar string vazia pro Discord levanta HTTPException DEPOIS do try/except do
    cog: a resposta some sem erro visivel. Era uma das causas do "nao respondeu"."""
    assert split_discord_message("") == []
    assert split_discord_message("   \n  ") == []
    assert all(p.strip() for p in split_discord_message("a" * 5000))


def test_looks_like_question_true_for_real_question():
    assert looks_like_question("Qual e a capital do Brasil?") is True


def test_looks_like_question_false_for_slash_command():
    assert looks_like_question("/ask oi?") is False


def test_looks_like_question_false_without_question_mark():
    assert looks_like_question("oi tudo bem") is False


def test_looks_like_question_false_too_short():
    assert looks_like_question("oi?") is False


def test_looks_like_question_ignores_question_mark_inside_url():
    assert looks_like_question("confere https://youtu.be/watch?v=abc123") is False


def test_ttlcache_miss_on_unknown_key():
    cache = TTLCache(ttl_seconds=60)
    value, hit = cache.get("missing")
    assert hit is False
    assert value is None


def test_ttlcache_hit_after_set():
    cache = TTLCache(ttl_seconds=60)
    cache.set("curitiba", {"lat": -25.43, "lon": -49.27})
    value, hit = cache.get("curitiba")
    assert hit is True
    assert value == {"lat": -25.43, "lon": -49.27}


def test_ttlcache_expires_after_ttl():
    cache = TTLCache(ttl_seconds=-1)
    cache.set("curitiba", "algo")
    value, hit = cache.get("curitiba")
    assert hit is False
    assert value is None


def test_ttlcache_caches_negative_result():
    cache = TTLCache(ttl_seconds=60)
    cache.set("cidadeinexistente", None)
    value, hit = cache.get("cidadeinexistente")
    assert hit is True
    assert value is None


def test_thinking_embed_nao_promete_tempo():
    """Ate 13/09/2026 o padrao era "Pensando... (resposta em ~20s)". O numero fixo
    errava nos dois sentidos - as vezes ela responde antes, as vezes passa dos 45s -
    e promessa quebrada e pior que nenhuma estimativa."""
    autor = thinking_embed().author.name
    assert "Pensando" in autor
    assert "~" not in autor
    assert "20s" not in autor
    assert "resposta em" not in autor


def test_thinking_embed_poe_o_gif_no_icone_do_author_em_24px():
    """Escolha do dono: o placeholder e um indicador de "nao travei", nao o assunto da
    mensagem. O thumbnail (~80px, tamanho fixo pelo Discord) chegou a ser usado e voltou."""
    embed = thinking_embed()
    assert embed.author.icon_url == THINKING_GIF_URL
    assert embed.thumbnail.url is None


def test_thinking_embed_preserva_texto_customizado():
    """Os comandos passam a propria legenda (/pesquisa, /noticias, /paragif...)."""
    assert thinking_embed("🔎 Pesquisando...").author.name == "🔎 Pesquisando..."
