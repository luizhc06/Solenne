from cogs.chat import SYSTEM_PROMPT, identidade_do_autor
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


def test_prompt_lista_as_features_novas_desta_leva_como_reais():
    """Regressao (auditoria final da leva 0be6725->11a55e9): boas-vindas automatica
    (cogs/welcome.py), enquetes (cogs/polls.py: /enquete, /encerrarenquete, /enquetes) e
    nivel/XP (cogs/leveling.py: /rank, /leaderboard) foram adicionadas nesta leva mas nao
    entraram no SYSTEM_PROMPT nem no /help - exatamente o mesmo bug que o commit da98192
    corrigiu pra moderacao automatica 20 minutos antes, na mesma leva. Sem aparecer aqui,
    a Solenne nega ter essas funcionalidades reais se perguntada em chat."""
    prompt = SYSTEM_PROMPT.lower()
    assert "boas-vindas automatica" in prompt
    assert "/enquete" in prompt and "/encerrarenquete" in prompt and "/enquetes" in prompt
    assert "/rank" in prompt and "/leaderboard" in prompt


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


def test_dono_e_tratado_em_segunda_pessoa():
    """Reclamacao do dono (06/09/2026): ela "nao reconhece que eu sou o dono". O ID batia
    e o prompt ja dizia isso - o que faltava era desfazer a terceira pessoa do resto do
    SYSTEM_PROMPT, que fazia ela responder "as series que o Rizu acompanha" PRO Rizu."""
    texto = identidade_do_autor(True)
    assert "VEIO do dono de verdade" in texto
    assert "segunda pessoa" in texto
    assert "Nao peca que ele prove quem e" in texto


def test_nao_dono_continua_sendo_avisado_que_nao_e():
    """A correcao nao pode virar porta de entrada: quem nao e dono continua marcado."""
    texto = identidade_do_autor(False)
    assert "NAO veio do dono" in texto
    assert "segunda pessoa" not in texto


def test_prompt_nao_afirma_que_a_lista_de_anime_e_fixa_no_codigo():
    """Ela respondeu que /anime e "uma lista fixa configurada no meu codigo". Nao e:
    cogs/anime.py consulta a conta AniList do dono na hora e le a lista "assistindo"."""
    assert "NAO e uma lista fixa" in SYSTEM_PROMPT
    assert "consulta a conta AniList do Rizu" in SYSTEM_PROMPT
