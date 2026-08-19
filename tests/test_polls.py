import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import db
from cogs.polls import (
    MAX_OPTIONS,
    VOTE_CLICK_COOLDOWN_SECONDS,
    PollsCog,
    build_results_embed,
    can_close_poll,
    collect_options,
    progress_bar,
)
from config import OWNER_USER_ID


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Aponta o modulo db pra um sqlite descartavel e cria o schema nele."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


class FakeClock:
    """Substitui time.monotonic() nos testes pra controlar o cooldown de clique sem
    depender de sleep real (mesmo padrao de tests/test_moderation_flow.py)."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr("cogs.polls.time.monotonic", fake)
    return fake


def _fake_bot():
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    return bot


def _fake_interaction(user_id):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.channel_id = 555
    interaction.response.send_message = AsyncMock()
    interaction.original_response = AsyncMock(return_value=MagicMock(id=4242))
    return interaction


@pytest.fixture
def cog():
    return PollsCog(_fake_bot())


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------------
# db.py: persistencia das enquetes e votos
# ---------------------------------------------------------------------------------


def test_create_poll_and_get_poll_roundtrip(isolated_db):
    poll_id = db.create_poll(
        channel_id=10, creator_id=1, question="Pizza ou sushi?",
        options=["Pizza", "Sushi"], anonymous=True, closes_at=None,
    )

    poll = db.get_poll(poll_id)

    assert poll["question"] == "Pizza ou sushi?"
    assert poll["options"] == ["Pizza", "Sushi"]
    assert poll["creator_id"] == 1
    assert poll["closed"] is False
    assert poll["closes_at"] is None
    assert poll["message_id"] is None


def test_get_poll_unknown_id_returns_none(isolated_db):
    assert db.get_poll(999999) is None


def test_set_poll_message_id(isolated_db):
    poll_id = db.create_poll(
        channel_id=10, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )

    db.set_poll_message_id(poll_id, 555555)

    assert db.get_poll(poll_id)["message_id"] == 555555


def test_cast_vote_is_new_on_first_vote(isolated_db):
    poll_id = db.create_poll(
        channel_id=10, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )

    is_new = db.cast_vote(poll_id, user_id=42, option_index=0)

    assert is_new is True
    assert db.get_poll_results(poll_id) == {0: 1}


def test_cast_vote_trocando_de_opcao_nao_soma_voto_novo(isolated_db):
    """O voto e trocavel, nao cumulativo - votar de novo so move o voto de opcao."""
    poll_id = db.create_poll(
        channel_id=10, creator_id=1, question="Q?", options=["A", "B", "C"],
        anonymous=True, closes_at=None,
    )
    db.cast_vote(poll_id, user_id=42, option_index=0)

    is_new = db.cast_vote(poll_id, user_id=42, option_index=2)

    assert is_new is False
    results = db.get_poll_results(poll_id)
    assert results == {2: 1}
    assert sum(results.values()) == 1


def test_cast_vote_diferentes_usuarios_contam_separado(isolated_db):
    poll_id = db.create_poll(
        channel_id=10, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    db.cast_vote(poll_id, user_id=1, option_index=0)
    db.cast_vote(poll_id, user_id=2, option_index=0)
    db.cast_vote(poll_id, user_id=3, option_index=1)

    assert db.get_poll_results(poll_id) == {0: 2, 1: 1}


def test_get_open_polls_ignora_encerradas(isolated_db):
    aberta_id = db.create_poll(
        channel_id=1, creator_id=1, question="aberta", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    fechada_id = db.create_poll(
        channel_id=1, creator_id=1, question="fechada", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    db.close_poll(fechada_id)

    open_ids = [p["id"] for p in db.get_open_polls()]

    assert aberta_id in open_ids
    assert fechada_id not in open_ids


def test_get_expired_polls_so_devolve_com_prazo_vencido_e_ainda_aberta(isolated_db):
    vencida_id = db.create_poll(
        channel_id=1, creator_id=1, question="vencida", options=["A", "B"],
        anonymous=True, closes_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    futura_id = db.create_poll(
        channel_id=1, creator_id=1, question="futura", options=["A", "B"],
        anonymous=True, closes_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    sem_prazo_id = db.create_poll(
        channel_id=1, creator_id=1, question="sem prazo", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    ja_fechada_id = db.create_poll(
        channel_id=1, creator_id=1, question="ja fechada", options=["A", "B"],
        anonymous=True, closes_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.close_poll(ja_fechada_id)

    expired_ids = [p["id"] for p in db.get_expired_polls()]

    assert expired_ids == [vencida_id]
    assert futura_id not in expired_ids
    assert sem_prazo_id not in expired_ids
    assert ja_fechada_id not in expired_ids


def test_close_poll_marca_como_encerrada(isolated_db):
    poll_id = db.create_poll(
        channel_id=1, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )

    db.close_poll(poll_id)

    assert db.get_poll(poll_id)["closed"] is True
    assert db.get_expired_polls() == []


def test_get_poll_results_enquete_sem_votos_e_dict_vazio(isolated_db):
    poll_id = db.create_poll(
        channel_id=1, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    assert db.get_poll_results(poll_id) == {}


# ---------------------------------------------------------------------------------
# Funcoes puras da cog
# ---------------------------------------------------------------------------------


def test_collect_options_ignora_vazias_e_espacos():
    assert collect_options("Pizza", "Sushi", "  ", None, "") == ["Pizza", "Sushi"]


def test_collect_options_respeita_o_limite_maximo():
    result = collect_options("A", "B", "C", "D", "E")
    assert len(result) == MAX_OPTIONS
    assert result == ["A", "B", "C", "D", "E"]


def test_collect_options_tira_espacos_nas_pontas():
    assert collect_options("  Pizza  ", "Sushi", None, None, None) == ["Pizza", "Sushi"]


def test_can_close_poll_permite_criador():
    assert can_close_poll(user_id=111, creator_id=111) is True


def test_can_close_poll_permite_dono_do_bot():
    assert can_close_poll(user_id=OWNER_USER_ID, creator_id=111) is True


def test_can_close_poll_recusa_terceiros():
    assert can_close_poll(user_id=999, creator_id=111) is False


def test_progress_bar_zero_votos_fica_vazia():
    assert progress_bar(0, 0, width=10) == "░" * 10


def test_progress_bar_metade_dos_votos():
    assert progress_bar(5, 10, width=10) == "█" * 5 + "░" * 5


def test_progress_bar_todos_os_votos():
    assert progress_bar(3, 3, width=10) == "█" * 10


def test_build_results_embed_mostra_contagem_e_percentual_por_opcao():
    poll = {"question": "Pizza ou sushi?", "options": ["Pizza", "Sushi"], "closes_at": None}
    embed = build_results_embed(poll, {0: 3, 1: 1}, closed=False)

    assert "Pizza ou sushi?" in embed.description
    assert embed.fields[0].name.endswith("Pizza")
    assert "3" in embed.fields[0].value and "75%" in embed.fields[0].value
    assert embed.fields[1].name.endswith("Sushi")
    assert "1" in embed.fields[1].value and "25%" in embed.fields[1].value
    assert "sem prazo" in embed.footer.text


def test_build_results_embed_titulo_muda_quando_encerrada():
    poll = {"question": "Q?", "options": ["A", "B"], "closes_at": None}
    embed = build_results_embed(poll, {}, closed=True)
    assert "encerrada" in embed.title


# ---------------------------------------------------------------------------------
# PollsCog.handle_vote: fluxo de voto (troca, cooldown, enquete encerrada)
# ---------------------------------------------------------------------------------


def test_handle_vote_registra_voto_novo_e_confirma_ephemeral(isolated_db, cog):
    poll_id = db.create_poll(
        channel_id=1, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    interaction = _fake_interaction(user_id=55)

    _run(cog.handle_vote(interaction, poll_id, 0))

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    texto = interaction.response.send_message.call_args.args[0]
    assert "registrado" in texto
    assert kwargs.get("ephemeral") is True
    assert db.get_poll_results(poll_id) == {0: 1}


def test_handle_vote_troca_de_opcao_depois_do_cooldown(isolated_db, cog, clock):
    poll_id = db.create_poll(
        channel_id=1, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    _run(cog.handle_vote(_fake_interaction(user_id=55), poll_id, 0))
    clock.advance(VOTE_CLICK_COOLDOWN_SECONDS + 0.1)

    interaction2 = _fake_interaction(user_id=55)
    _run(cog.handle_vote(interaction2, poll_id, 1))

    texto = interaction2.response.send_message.call_args.args[0]
    assert "atualizado" in texto
    results = db.get_poll_results(poll_id)
    assert results == {1: 1}


def test_handle_vote_dentro_do_cooldown_e_ignorado_e_nao_muda_o_voto(isolated_db, cog, clock):
    poll_id = db.create_poll(
        channel_id=1, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    _run(cog.handle_vote(_fake_interaction(user_id=55), poll_id, 0))
    clock.advance(0.5)  # ainda dentro do VOTE_CLICK_COOLDOWN_SECONDS

    interaction2 = _fake_interaction(user_id=55)
    _run(cog.handle_vote(interaction2, poll_id, 1))

    texto = interaction2.response.send_message.call_args.args[0]
    assert "Calma" in texto
    # o voto original continua valendo - o clique dentro do cooldown nao trocou nada.
    assert db.get_poll_results(poll_id) == {0: 1}


def test_handle_vote_recusa_enquete_ja_encerrada(isolated_db, cog):
    poll_id = db.create_poll(
        channel_id=1, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    db.close_poll(poll_id)
    interaction = _fake_interaction(user_id=55)

    _run(cog.handle_vote(interaction, poll_id, 0))

    texto = interaction.response.send_message.call_args.args[0]
    assert "encerrada" in texto
    assert db.get_poll_results(poll_id) == {}


def test_handle_vote_cooldown_e_por_pessoa_nao_global(isolated_db, cog, clock):
    """Um clique de uma pessoa nao pode bloquear o voto de outra na mesma enquete."""
    poll_id = db.create_poll(
        channel_id=1, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    _run(cog.handle_vote(_fake_interaction(user_id=1), poll_id, 0))

    interaction2 = _fake_interaction(user_id=2)
    _run(cog.handle_vote(interaction2, poll_id, 1))

    texto = interaction2.response.send_message.call_args.args[0]
    assert "registrado" in texto
    assert db.get_poll_results(poll_id) == {0: 1, 1: 1}


# ---------------------------------------------------------------------------------
# Encerramento (comando /encerrarenquete e _close_and_announce)
# ---------------------------------------------------------------------------------


def test_encerrarenquete_recusa_quem_nao_e_criador_nem_dono(isolated_db, cog):
    poll_id = db.create_poll(
        channel_id=1, creator_id=111, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    attacker = _fake_interaction(user_id=999)

    _run(PollsCog.encerrarenquete.callback(cog, attacker, id=poll_id))

    attacker.response.send_message.assert_awaited_once()
    texto = attacker.response.send_message.call_args.args[0]
    assert "So quem criou" in texto
    assert db.get_poll(poll_id)["closed"] is False


def test_encerrarenquete_permite_o_criador(isolated_db, cog):
    poll_id = db.create_poll(
        channel_id=1, creator_id=111, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    interaction = _fake_interaction(user_id=111)

    _run(PollsCog.encerrarenquete.callback(cog, interaction, id=poll_id))

    interaction.response.send_message.assert_awaited_once()
    texto = interaction.response.send_message.call_args.args[0]
    assert "encerrada" in texto
    assert db.get_poll(poll_id)["closed"] is True


def test_encerrarenquete_permite_o_dono_do_bot_mesmo_sem_ter_criado(isolated_db, cog):
    poll_id = db.create_poll(
        channel_id=1, creator_id=111, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    interaction = _fake_interaction(user_id=OWNER_USER_ID)

    _run(PollsCog.encerrarenquete.callback(cog, interaction, id=poll_id))

    assert db.get_poll(poll_id)["closed"] is True


def test_encerrarenquete_enquete_inexistente(isolated_db, cog):
    interaction = _fake_interaction(user_id=1)

    _run(PollsCog.encerrarenquete.callback(cog, interaction, id=999999))

    texto = interaction.response.send_message.call_args.args[0]
    assert "Nao achei" in texto


def test_encerrarenquete_ja_encerrada_avisa_sem_reencerrar(isolated_db, cog):
    poll_id = db.create_poll(
        channel_id=1, creator_id=111, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    db.close_poll(poll_id)
    interaction = _fake_interaction(user_id=111)

    _run(PollsCog.encerrarenquete.callback(cog, interaction, id=poll_id))

    texto = interaction.response.send_message.call_args.args[0]
    assert "ja estava encerrada" in texto


def test_close_and_announce_desabilita_botoes_e_edita_resultado_final(isolated_db, cog):
    poll_id = db.create_poll(
        channel_id=42, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    db.set_poll_message_id(poll_id, 999)
    db.cast_vote(poll_id, user_id=5, option_index=0)

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    cog.bot.get_channel = MagicMock(return_value=channel)

    poll = db.get_poll(poll_id)
    _run(cog._close_and_announce(poll))

    message.edit.assert_awaited_once()
    edit_kwargs = message.edit.call_args.kwargs
    assert all(child.disabled for child in edit_kwargs["view"].children)
    assert "encerrada" in edit_kwargs["embed"].title
    assert db.get_poll(poll_id)["closed"] is True


def test_close_and_announce_sem_mensagem_salva_ainda_fecha_no_banco(isolated_db, cog):
    """Se por algum motivo a enquete nunca teve message_id salvo, o encerramento no
    banco tem que acontecer mesmo assim (nao pode travar preso a UI do Discord)."""
    poll_id = db.create_poll(
        channel_id=1, creator_id=1, question="Q?", options=["A", "B"],
        anonymous=True, closes_at=None,
    )
    poll = db.get_poll(poll_id)

    _run(cog._close_and_announce(poll))

    assert db.get_poll(poll_id)["closed"] is True


# ---------------------------------------------------------------------------------
# /enquete: criacao e validacao
# ---------------------------------------------------------------------------------


def test_enquete_rejeita_menos_de_duas_opcoes(isolated_db, cog):
    interaction = _fake_interaction(user_id=1)

    _run(PollsCog.enquete.callback(
        cog, interaction, pergunta="Q?", opcao1="Unica", opcao2="  ",
        opcao3=None, opcao4=None, opcao5=None, duracao_minutos=None, anonimo=True,
    ))

    texto = interaction.response.send_message.call_args.args[0]
    assert "pelo menos" in texto
    assert db.get_open_polls() == []


def test_enquete_rejeita_opcoes_duplicadas(isolated_db, cog):
    interaction = _fake_interaction(user_id=1)

    _run(PollsCog.enquete.callback(
        cog, interaction, pergunta="Q?", opcao1="Pizza", opcao2="Pizza",
        opcao3=None, opcao4=None, opcao5=None, duracao_minutos=None, anonimo=True,
    ))

    texto = interaction.response.send_message.call_args.args[0]
    assert "diferentes" in texto
    assert db.get_open_polls() == []


def test_enquete_rejeita_duracao_fora_do_limite(isolated_db, cog):
    interaction = _fake_interaction(user_id=1)

    _run(PollsCog.enquete.callback(
        cog, interaction, pergunta="Q?", opcao1="A", opcao2="B",
        opcao3=None, opcao4=None, opcao5=None, duracao_minutos=0, anonimo=True,
    ))

    texto = interaction.response.send_message.call_args.args[0]
    assert "Duracao" in texto
    assert db.get_open_polls() == []


def test_enquete_cria_e_salva_no_banco_com_message_id(isolated_db, cog):
    interaction = _fake_interaction(user_id=77)

    _run(PollsCog.enquete.callback(
        cog, interaction, pergunta="Pizza ou sushi?", opcao1="Pizza", opcao2="Sushi",
        opcao3=None, opcao4=None, opcao5=None, duracao_minutos=30, anonimo=True,
    ))

    interaction.response.send_message.assert_awaited_once()
    send_kwargs = interaction.response.send_message.call_args.kwargs
    assert send_kwargs["embed"].description == "**Pizza ou sushi?**"
    assert len(send_kwargs["view"].children) == 2

    open_polls = db.get_open_polls()
    assert len(open_polls) == 1
    poll = open_polls[0]
    assert poll["creator_id"] == 77
    assert poll["options"] == ["Pizza", "Sushi"]
    assert poll["message_id"] == 4242  # vem de interaction.original_response()
    assert poll["closes_at"] is not None
