from cogs.moderation import (
    MASS_MENTION_DISTINCT,
    MENTION_PUNISH_AT,
    MENTION_SAME_TARGET_IN_ONE_MSG,
    MENTION_WARN_AT,
    mention_counts,
    mention_verdict,
)

RIZU = "111"
AMIGO = "222"


def test_conta_mencoes_repetidas_do_mesmo_alvo():
    """message.mentions do Discord vem deduplicado, entao "@rizu @rizu @rizu" chegaria
    como um alvo so - por isso a contagem sai do texto, nao do payload."""
    counts = mention_counts(f"<@{RIZU}> <@{RIZU}> <@{RIZU}>")
    assert counts[RIZU] == 3


def test_conta_mencao_com_e_sem_exclamacao():
    """O formato antigo <@!id> ainda aparece em clientes e mensagens velhas."""
    assert mention_counts(f"<@!{RIZU}> oi <@{RIZU}>")[RIZU] == 2


def test_mencao_de_cargo_conta_separado_do_usuario():
    counts = mention_counts(f"<@{RIZU}> <@&999>")
    assert counts[RIZU] == 1
    assert counts["role:999"] == 1


def test_texto_sem_mencao_nao_conta_nada():
    assert mention_counts("olha esse email fulano@dominio.com") == {}
    assert mention_counts("") == {}


def test_marcar_cinco_amigos_diferentes_nao_e_punido():
    """O FALSO POSITIVO QUE MOTIVOU A MUDANCA: em producao (ago/2026) a regra antiga era
    "5+ mencoes numa mensagem = spam" e apagou a mensagem de alguem marcando 5 amigos
    diferentes, sem aviso, com timeout e DM pro dono com botao de banir."""
    counts = mention_counts(" ".join(f"<@{i}>" for i in range(1, 6)))
    acao, _motivo = mention_verdict(counts, {str(i): 1 for i in range(1, 6)})
    assert acao is None


def test_avisa_na_segunda_mensagem_marcando_a_mesma_pessoa():
    counts = mention_counts(f"<@{RIZU}>")
    acao, _motivo = mention_verdict(counts, {RIZU: MENTION_WARN_AT})
    assert acao == "avisar"


def test_pune_na_terceira_mensagem_marcando_a_mesma_pessoa():
    counts = mention_counts(f"<@{RIZU}>")
    acao, motivo = mention_verdict(counts, {RIZU: MENTION_PUNISH_AT})
    assert acao == "punir"
    assert "mensagens seguidas" in motivo


def test_primeira_mencao_nao_faz_nada():
    acao, _motivo = mention_verdict(mention_counts(f"<@{RIZU}>"), {RIZU: 1})
    assert acao is None


def test_pune_quem_repete_o_mesmo_arroba_numa_mensagem_so():
    """"@rizu @rizu @rizu @rizu" numa mensagem unica e o mesmo incomodo, so que
    concentrado - nao da pra esperar a segunda mensagem pra reagir."""
    counts = mention_counts(f"<@{RIZU}> " * MENTION_SAME_TARGET_IN_ONE_MSG)
    acao, motivo = mention_verdict(counts, {RIZU: 1})
    assert acao == "punir"
    assert "mesma mensagem" in motivo


def test_pune_raid_de_muita_gente_distinta():
    """Marcar MUITA gente de uma vez continua sendo tratado como ataque - so que num
    patamar que nao da pra confundir com "marquei a galera"."""
    counts = mention_counts(" ".join(f"<@{i}>" for i in range(MASS_MENTION_DISTINCT)))
    acao, motivo = mention_verdict(counts, {str(i): 1 for i in range(MASS_MENTION_DISTINCT)})
    assert acao == "punir"
    assert "de uma vez" in motivo


def test_insistir_em_alvos_diferentes_nao_acumula():
    """Marcar o Rizu uma vez e o amigo uma vez nao pode somar como se fosse insistencia:
    a contagem e POR ALVO."""
    counts = mention_counts(f"<@{RIZU}> <@{AMIGO}>")
    acao, _motivo = mention_verdict(counts, {RIZU: 1, AMIGO: 1})
    assert acao is None


def test_mensagem_sem_mencao_nunca_gera_acao():
    assert mention_verdict(mention_counts("so uma mensagem normal"), {}) == (None, "")
