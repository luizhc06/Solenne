import asyncio

import user_profile


def test_update_profile_concorrente_nao_perde_update(monkeypatch):
    """Duas tasks de update_profile() pro MESMO user_id, quase juntas (ex: uma mensagem
    de chat e um clique no botao de feedback da resposta anterior) - antes do lock por
    user_id, a segunda lia o `current` ORIGINAL (lido antes da primeira terminar,
    enquanto a primeira ainda esperava a IA) e podia sobrescrever o resultado da
    primeira silenciosamente (lost update, achado do conselho de agentes, 19/08/2026).
    Com o lock por user_id, a segunda so comeca a ler depois que a primeira termina
    de gravar - entao ela parte do resumo real, nao do desatualizado."""
    store = {}
    prompts_recebidos = []
    ordem_conclusao = []

    def fake_get_user_summary(user_id):
        return store.get(user_id, "")

    def fake_save_user_summary(user_id, display_name, summary):
        store[user_id] = summary

    async def fake_complete(messages, temperature=0.3, **kwargs):
        prompt = messages[0]["content"]
        prompts_recebidos.append(prompt)
        if "(vazio ainda)" in prompt:
            # Simula a chamada de IA lenta (varios segundos, ver auditoria) da PRIMEIRA
            # task - da tempo da segunda task ser disparada enquanto esta ainda roda.
            await asyncio.sleep(0.05)
            ordem_conclusao.append("A")
            return "resumo A: gosta de gatos"
        ordem_conclusao.append("B")
        return "resumo B: gosta de gatos e cachorros"

    monkeypatch.setattr(user_profile, "get_user_summary", fake_get_user_summary)
    monkeypatch.setattr(user_profile, "save_user_summary", fake_save_user_summary)
    monkeypatch.setattr(user_profile, "_complete", fake_complete)

    async def cenario():
        tarefa_a = asyncio.create_task(user_profile.update_profile(1, "Fulano", "mensagem A"))
        await asyncio.sleep(0)  # A comeca primeiro e entra na chamada de IA lenta
        tarefa_b = asyncio.create_task(user_profile.update_profile(1, "Fulano", "mensagem B"))
        await asyncio.gather(tarefa_a, tarefa_b)

    asyncio.run(cenario())

    # A termina antes de B conseguir nem comecar a chamada de IA - serializado de verdade.
    assert ordem_conclusao == ["A", "B"]
    # B leu o resumo que A gravou (nao o "(vazio ainda)" original) - sem isso o teste
    # falha e reproduz exatamente o lost update da auditoria.
    assert "resumo A: gosta de gatos" in prompts_recebidos[1]
    assert "(vazio ainda)" not in prompts_recebidos[1]
    # Nada se perde: o valor final reflete B, calculado em cima do resumo real de A.
    assert store[1] == "resumo B: gosta de gatos e cachorros"


def test_update_profile_usuarios_diferentes_nao_se_bloqueiam(monkeypatch):
    """O lock e por user_id - duas tasks de usuarios diferentes continuam rodando a
    chamada de IA em paralelo, sem serializar sem necessidade."""
    store = {}
    dentro_agora = 0
    pico_simultaneo = 0

    def fake_get_user_summary(user_id):
        return store.get(user_id, "")

    def fake_save_user_summary(user_id, display_name, summary):
        store[user_id] = summary

    async def fake_complete(messages, temperature=0.3, **kwargs):
        nonlocal dentro_agora, pico_simultaneo
        dentro_agora += 1
        pico_simultaneo = max(pico_simultaneo, dentro_agora)
        await asyncio.sleep(0.02)
        dentro_agora -= 1
        return "resumo"

    monkeypatch.setattr(user_profile, "get_user_summary", fake_get_user_summary)
    monkeypatch.setattr(user_profile, "save_user_summary", fake_save_user_summary)
    monkeypatch.setattr(user_profile, "_complete", fake_complete)

    async def cenario():
        await asyncio.gather(
            user_profile.update_profile(1, "Fulano", "oi"),
            user_profile.update_profile(2, "Beltrano", "oi"),
        )

    asyncio.run(cenario())

    assert pico_simultaneo == 2


def test_update_profile_truncado_mantem_o_resumo_anterior(monkeypatch):
    """Achado nos logs de producao (19/09/2026): o resumo estourou o max_tokens e, como
    _complete passou a barrar geracao truncada, a excecao subia e caia no except
    Exception generico - logada com stack trace como se fosse falha inesperada.

    O comportamento certo e o que o teste trava: um resumo cortado no meio NAO substitui
    o anterior. Esse texto entra no system prompt de toda conversa dela com a pessoa, e
    gravar meia frase sujaria pra sempre o que ela "sabe" sobre ela."""
    store = {7: "gosta de hardware e acompanha noticias de tecnologia"}
    gravacoes = []

    async def fake_complete(*args, **kwargs):
        raise user_profile.TruncatedAIResponse("geracao truncada (max_tokens=400)")

    monkeypatch.setattr(user_profile, "get_user_summary", lambda uid: store.get(uid, ""))
    monkeypatch.setattr(
        user_profile, "save_user_summary",
        lambda uid, nome, resumo: gravacoes.append((uid, resumo)),
    )
    monkeypatch.setattr(user_profile, "_complete", fake_complete)

    asyncio.run(user_profile.update_profile(7, "Rizu", "mensagem qualquer"))

    assert gravacoes == [], "um resumo truncado nunca pode ser gravado"
    assert store[7] == "gosta de hardware e acompanha noticias de tecnologia"


def test_cap_summary_corta_no_teto_de_linhas():
    """Achado na verificacao geral de 20/09/2026: o perfil do dono chegou a 25 linhas
    porque o teto de 5 linhas so existia como pedido em texto no prompt, nunca garantido
    no codigo. _cap_summary trava esse teto independente do modelo obedecer ou nao."""
    texto = "\n".join(f"linha {i}" for i in range(1, 26))
    resultado = user_profile._cap_summary(texto)
    assert resultado.count("\n") + 1 <= user_profile.MAX_SUMMARY_LINES
    assert resultado.splitlines() == ["linha 1", "linha 2", "linha 3", "linha 4", "linha 5"]


def test_cap_summary_corta_linha_unica_gigante():
    """Backstop de MAX_SUMMARY_CHARS: 5 linhas dentro do limite nao protege contra UMA
    linha absurdamente longa - precisa de teto por tamanho tambem."""
    linha_gigante = "gosta de " + ("tecnologia " * 200)
    resultado = user_profile._cap_summary(linha_gigante)
    assert len(resultado) <= user_profile.MAX_SUMMARY_CHARS + 1  # +1 pela reticencia


def test_cap_summary_preserva_resumo_curto_sem_alterar():
    texto = "Gosta de hardware.\nAcompanha noticias de tecnologia."
    assert user_profile._cap_summary(texto) == texto


def test_cap_summary_ignora_linhas_vazias():
    texto = "linha 1\n\n\nlinha 2\n"
    assert user_profile._cap_summary(texto) == "linha 1\nlinha 2"


def test_update_profile_tira_mencoes_antes_de_montar_o_prompt(monkeypatch):
    """Mesma familia do bug do ping de grupo (19/09/2026): sem isso, "<@111> <@222>"
    solto virava a "mensagem" que o modelo recebia como fato sobre a pessoa, e as vezes
    voltava gravado no resumo (achado na verificacao geral de 20/09/2026 - o perfil do
    dono tinha "Nova mensagem: <@298511422898569216>" registrado como se fosse relevante)."""
    prompts_recebidos = []

    async def fake_complete(messages, temperature=0.3, **kwargs):
        prompts_recebidos.append(messages[0]["content"])
        return "resumo atualizado"

    monkeypatch.setattr(user_profile, "get_user_summary", lambda uid: "")
    monkeypatch.setattr(user_profile, "save_user_summary", lambda uid, nome, resumo: None)
    monkeypatch.setattr(user_profile, "_complete", fake_complete)

    asyncio.run(user_profile.update_profile(1, "Fulano", "<@111> <@222> vale a pena aprender Rust?"))

    assert len(prompts_recebidos) == 1
    assert "<@111>" not in prompts_recebidos[0]
    assert "<@222>" not in prompts_recebidos[0]
    assert "vale a pena aprender Rust?" in prompts_recebidos[0]


def test_update_profile_ignora_mensagem_que_e_so_mencao(monkeypatch):
    """Ping de grupo sem texto ("@Hud @Rizu @KrekNeto @Solenne", o incidente de
    19/09/2026): depois de tirar as mencoes nao sobra nada util pra registrar. Nem
    vale gastar uma chamada de IA - retorna cedo, sem tocar _complete nem o banco."""
    chamou_ia = False

    async def fake_complete(*args, **kwargs):
        nonlocal chamou_ia
        chamou_ia = True
        return "nunca deveria rodar"

    gravacoes = []
    monkeypatch.setattr(user_profile, "get_user_summary", lambda uid: "")
    monkeypatch.setattr(user_profile, "save_user_summary", lambda uid, nome, resumo: gravacoes.append(resumo))
    monkeypatch.setattr(user_profile, "_complete", fake_complete)

    asyncio.run(user_profile.update_profile(1, "Fulano", "<@111> <@222> <@333>"))

    assert not chamou_ia
    assert gravacoes == []


def test_update_profile_aplica_cap_mesmo_se_modelo_ignorar_o_limite(monkeypatch):
    """Fim a fim: mesmo quando o modelo devolve um resumo enorme (ignorando o "no maximo
    5 linhas" do prompt), o que chega no banco respeita o teto do codigo."""
    resumo_enorme = "\n".join(f"fato numero {i} sobre a pessoa" for i in range(1, 30))
    gravacoes = []

    async def fake_complete(*args, **kwargs):
        return resumo_enorme

    monkeypatch.setattr(user_profile, "get_user_summary", lambda uid: "")
    monkeypatch.setattr(user_profile, "save_user_summary", lambda uid, nome, resumo: gravacoes.append(resumo))
    monkeypatch.setattr(user_profile, "_complete", fake_complete)

    asyncio.run(user_profile.update_profile(1, "Fulano", "mensagem qualquer"))

    assert len(gravacoes) == 1
    assert gravacoes[0].count("\n") + 1 <= user_profile.MAX_SUMMARY_LINES
