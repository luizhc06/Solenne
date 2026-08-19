from cogs.chat import SYSTEM_PROMPT
from cogs.search import PESQUISAR_WEB_DESCRICAO


def test_prompt_reusa_descricao_da_tool_pesquisar_web_sem_duplicar():
    """Regressao (auditoria): a explicacao de quando usar pesquisar_web tinha duas
    versoes escritas a mao - uma no SYSTEM_PROMPT e outra em PESQUISAR_WEB_DESCRICAO
    (a que a API de fato recebe, via tools.register em cogs/search.py) - e ja tinham
    divergido uma da outra. O SYSTEM_PROMPT agora reusa PESQUISAR_WEB_DESCRICAO em vez
    de reescreve-la, entao as duas nunca mais podem sair diferentes."""
    assert PESQUISAR_WEB_DESCRICAO in SYSTEM_PROMPT


def test_prompt_lista_moderacao_automatica_como_funcionalidade_real():
    """Regressao: o SYSTEM_PROMPT manda a Solenne negar qualquer coisa fora da lista de
    "funcionalidades reais", mas a moderacao automatica (flood -> apaga + timeout + DM pro
    dono, ver cogs/moderation.py e o embed de /help) e um recurso real dela. Sem aparecer
    aqui, ela podia negar em chat que modera o canal."""
    assert "moderacao automatica" in SYSTEM_PROMPT.lower()
    assert "timeout" in SYSTEM_PROMPT.lower()
    assert "dm pro dono" in SYSTEM_PROMPT.lower()


def test_prompt_emoji_restringe_so_o_texto_dela_nao_os_embeds_do_codigo():
    """Regressao (auditoria): a regra de "no maximo 1-2 emoji" fala do TEXTO que a
    Solenne escreve. Sem essa ressalva explicita, ela le como se valesse pra qualquer
    coisa que "represente a voz dela" - inclusive os embeds que o codigo monta (/help
    com um emoji por campo, cabecalhos de /noticias), que tem um padrao visual proprio
    e deliberado, nao regido por essa regra de escrita."""
    prompt = SYSTEM_PROMPT.lower()
    assert "no maximo 1 ou 2 por resposta" in prompt
    trecho = prompt[prompt.index("no maximo 1 ou 2 por resposta"):]
    assert "embeds fixos que o codigo monta" in trecho
    assert "/help" in trecho
