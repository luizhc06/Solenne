from cogs.chat import SYSTEM_PROMPT


def test_prompt_lista_moderacao_automatica_como_funcionalidade_real():
    """Regressao: o SYSTEM_PROMPT manda a Solenne negar qualquer coisa fora da lista de
    "funcionalidades reais", mas a moderacao automatica (flood -> apaga + timeout + DM pro
    dono, ver cogs/moderation.py e o embed de /help) e um recurso real dela. Sem aparecer
    aqui, ela podia negar em chat que modera o canal."""
    assert "moderacao automatica" in SYSTEM_PROMPT.lower()
    assert "timeout" in SYSTEM_PROMPT.lower()
    assert "dm pro dono" in SYSTEM_PROMPT.lower()
