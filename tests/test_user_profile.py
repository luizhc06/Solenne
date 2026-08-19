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
