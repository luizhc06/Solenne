from cogs.search import SEARCH_SYNTHESIS_PROMPT


def test_prompt_estabelece_identidade_e_voz_da_solenne():
    """Regressao (auditoria): diferente de todo outro prompt gerador de texto do bot
    (SYSTEM_PROMPT, SUMMARY_PROMPT, NEWS_INTRO_PROMPT), o SEARCH_SYNTHESIS_PROMPT nunca
    se identificava como Solenne nem pedia a personalidade dela - so "resposta
    objetiva". Isso deixava a resposta de /pesquisa em risco de sair com voz de IA
    generica, destoando do resto do bot."""
    prompt = SEARCH_SYNTHESIS_PROMPT.lower()
    assert "voce e solenne" in prompt
    assert "personalidade" in prompt
