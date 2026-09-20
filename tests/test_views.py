import asyncio

import views


class _FakeUser:
    def __init__(self, user_id, display_name):
        self.id = user_id
        self.display_name = display_name


class _FakeResponse:
    def __init__(self):
        self.mensagens_enviadas = []

    async def send_message(self, content, ephemeral=False):
        self.mensagens_enviadas.append(content)


class _FakeInteraction:
    def __init__(self, user_id, display_name):
        self.user = _FakeUser(user_id, display_name)
        self.response = _FakeResponse()


def test_feedback_like_nao_afirma_que_a_pessoa_gosta_do_assunto(monkeypatch):
    """`topic` e a PERGUNTA da propria pessoa (pergunta[:200] em cogs/chat.py), nao um
    assunto que apareceu pra ela por acaso - a nota tem que deixar isso legivel pro
    PROFILE_UPDATE_PROMPT."""
    notas = []
    monkeypatch.setattr(views, "schedule_profile_update", lambda uid, nome, nota: notas.append(nota))

    async def cenario():
        view = views.FeedbackView("vale a pena migrar pra Postgres?")
        interaction = _FakeInteraction(1, "Fulano")
        await view.like.callback(interaction)

    asyncio.run(cenario())

    assert len(notas) == 1
    assert "vale a pena migrar pra Postgres?" in notas[0]
    assert "resposta" in notas[0].lower()


def test_feedback_dislike_nao_vira_nao_gosta_do_assunto_perguntado(monkeypatch):
    """O bug encontrado na verificacao geral de 20/09/2026: o proprio dono perguntou
    sobre personagens/animes, clicou (dislike) na RESPOSTA, e o perfil dele passou a
    registrar "Nao gostou / achou irrelevante conteudo sobre: [a pergunta dele mesmo]" -
    como se ele nao gostasse do assunto que ELE MESMO levantou. A nota agora precisa
    deixar explicito que o dislike e sobre a qualidade da resposta, nao sobre o
    assunto."""
    notas = []
    monkeypatch.setattr(views, "schedule_profile_update", lambda uid, nome, nota: notas.append(nota))

    pergunta_do_dono = "pesquise no meu perfil e faca uma lista de personagens favoritos e de animes favoritos"

    async def cenario():
        view = views.FeedbackView(pergunta_do_dono)
        interaction = _FakeInteraction(42, "Rizu")
        await view.dislike.callback(interaction)

    asyncio.run(cenario())

    assert len(notas) == 1
    nota = notas[0].lower()
    # A pergunta deve aparecer identificada como PERGUNTA/RESPOSTA, nao como "assunto
    # que a pessoa nao gosta".
    assert "resposta" in nota
    assert "nao gostou do assunto" not in nota
    assert "nao gosta do assunto" not in nota
    # A nota precisa deixar explicito que isso nao e uma preferencia de assunto.
    assert "nao" in nota and "assunto" in nota


def test_feedback_topic_continua_truncado_em_200_chars():
    async def cenario():
        view = views.FeedbackView("x" * 500)
        return view.topic

    assert len(asyncio.run(cenario())) == 200
