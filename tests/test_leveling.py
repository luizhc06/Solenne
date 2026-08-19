"""Testes do sistema de nivel/XP (cogs/leveling.py, db.py, leveling_utils.py).

leveling_utils.xp_for_level/level_for_xp sao funcoes puras (mesmo padrao de parse_when
em cogs/reminders.py) - testadas isoladas, sem SQLite nem Discord. O resto (cooldown de
ganho em memoria, UPSERT no banco, comandos /rank e /leaderboard) segue o mesmo padrao
de fixtures/fakes ja usado em tests/test_moderation_flow.py e tests/test_reminders.py.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import db
from cogs.leveling import (
    MIN_XP_MESSAGE_LENGTH,
    XP_COOLDOWN_SECONDS,
    LevelingCog,
    progress_bar,
)
from config import ALLOWED_GUILD_ID
from leveling_utils import LEVEL_XP_BASE, level_for_xp, xp_for_level


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Aponta o modulo db pra um sqlite descartavel e cria o schema nele."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


class FakeClock:
    """Substitui time.monotonic() nos testes de cooldown (mesmo padrao de
    tests/test_moderation_flow.py e tests/test_polls.py)."""

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
    monkeypatch.setattr("cogs.leveling.time.monotonic", fake)
    return fake


@pytest.fixture()
def cog():
    return LevelingCog(MagicMock())


def _fake_guild(guild_id=ALLOWED_GUILD_ID):
    guild = MagicMock()
    guild.id = guild_id
    return guild


def _fake_member(member_id, bot=False, display_name=None):
    member = MagicMock()
    member.id = member_id
    member.bot = bot
    member.display_name = display_name or f"membro{member_id}"
    member.mention = f"<@{member_id}>"
    member.display_avatar.url = "https://example.com/avatar.png"
    return member


def _fake_message(guild, member, content="mensagem normal"):
    message = MagicMock()
    message.guild = guild
    message.author = member
    message.content = content
    message.channel.send = AsyncMock()
    return message


def _fake_interaction(user):
    interaction = MagicMock()
    interaction.user = user
    interaction.response.send_message = AsyncMock()
    return interaction


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------------
# Formula de nivel (leveling_utils.py) - funcoes puras
# ---------------------------------------------------------------------------------


def test_xp_for_level_zero_is_zero():
    assert xp_for_level(0) == 0


@pytest.mark.parametrize("level,expected_xp", [(1, LEVEL_XP_BASE), (2, LEVEL_XP_BASE * 4), (5, LEVEL_XP_BASE * 25)])
def test_xp_for_level_follows_quadratic_curve(level, expected_xp):
    assert xp_for_level(level) == expected_xp


def test_level_for_xp_zero_and_negative_is_level_zero():
    assert level_for_xp(0) == 0
    assert level_for_xp(-50) == 0


def test_level_for_xp_just_below_threshold_is_previous_level():
    """1 XP a menos do que o necessario pro nivel N nao pode contar como nivel N."""
    limiar = xp_for_level(3)
    assert level_for_xp(limiar - 1) == 2
    assert level_for_xp(limiar) == 3


@pytest.mark.parametrize("level", [0, 1, 2, 3, 5, 10, 25])
def test_level_for_xp_is_the_inverse_of_xp_for_level(level):
    """No limiar exato de cada nivel, o calculo inverso tem que bater com o nivel
    original - e a garantia central da formula (round-trip)."""
    assert level_for_xp(xp_for_level(level)) == level


# ---------------------------------------------------------------------------------
# Barra de progresso
# ---------------------------------------------------------------------------------


def test_progress_bar_empty_and_full():
    assert progress_bar(0, 100, width=8) == "▱▱▱▱▱▱▱▱"
    assert progress_bar(100, 100, width=8) == "▰▰▰▰▰▰▰▰"


def test_progress_bar_half():
    assert progress_bar(50, 100, width=8) == "▰▰▰▰▱▱▱▱"


def test_progress_bar_never_exceeds_width_even_if_current_overshoots():
    assert progress_bar(150, 100, width=8) == "▰▰▰▰▰▰▰▰"


# ---------------------------------------------------------------------------------
# db.add_xp / get_user_level / get_rank_position / get_leaderboard
# ---------------------------------------------------------------------------------


def test_add_xp_creates_row_on_first_message(isolated_db):
    subiu, novo_nivel = db.add_xp(user_id=1, display_name="Rizu", amount=30)

    assert subiu is False  # 30 XP nao fecha nivel 1 (precisa de 100)
    assert novo_nivel == 0
    dados = db.get_user_level(1)
    assert dados["xp"] == 30
    assert dados["level"] == 0
    assert dados["messages_count"] == 1
    assert dados["display_name"] == "Rizu"


def test_add_xp_accumulates_across_calls(isolated_db):
    db.add_xp(user_id=1, display_name="Rizu", amount=30)
    db.add_xp(user_id=1, display_name="Rizu", amount=40)

    dados = db.get_user_level(1)
    assert dados["xp"] == 70
    assert dados["messages_count"] == 2


def test_add_xp_reports_level_up_when_crossing_threshold(isolated_db):
    db.add_xp(user_id=1, display_name="Rizu", amount=95)  # ainda nivel 0

    subiu, novo_nivel = db.add_xp(user_id=1, display_name="Rizu", amount=10)  # cruza 100

    assert subiu is True
    assert novo_nivel == 1
    assert db.get_user_level(1)["level"] == 1


def test_add_xp_updates_display_name(isolated_db):
    db.add_xp(user_id=1, display_name="NomeAntigo", amount=10)
    db.add_xp(user_id=1, display_name="NomeNovo", amount=10)

    assert db.get_user_level(1)["display_name"] == "NomeNovo"


def test_get_user_level_returns_none_for_unknown_user(isolated_db):
    assert db.get_user_level(999) is None


def test_get_rank_position_returns_none_for_unknown_user(isolated_db):
    assert db.get_rank_position(999) is None


def test_get_rank_position_orders_by_xp_desc(isolated_db):
    db.add_xp(user_id=1, display_name="A", amount=300)
    db.add_xp(user_id=2, display_name="B", amount=100)
    db.add_xp(user_id=3, display_name="C", amount=200)

    assert db.get_rank_position(1) == 1  # mais XP
    assert db.get_rank_position(3) == 2
    assert db.get_rank_position(2) == 3  # menos XP


def test_get_user_levels_count(isolated_db):
    assert db.get_user_levels_count() == 0
    db.add_xp(user_id=1, display_name="A", amount=10)
    db.add_xp(user_id=2, display_name="B", amount=10)
    assert db.get_user_levels_count() == 2


def test_get_leaderboard_orders_and_limits(isolated_db):
    db.add_xp(user_id=1, display_name="A", amount=100)
    db.add_xp(user_id=2, display_name="B", amount=300)
    db.add_xp(user_id=3, display_name="C", amount=200)

    top2 = db.get_leaderboard(limit=2)

    assert [u["user_id"] for u in top2] == [2, 3]
    assert [u["xp"] for u in top2] == [300, 200]


def test_get_leaderboard_empty(isolated_db):
    assert db.get_leaderboard() == []


# ---------------------------------------------------------------------------------
# LevelingCog.on_message - filtros e cooldown
# ---------------------------------------------------------------------------------


def test_on_message_ignores_bot_author(cog, clock, isolated_db):
    guild = _fake_guild()
    member = _fake_member(1, bot=True)
    _run(cog.on_message(_fake_message(guild, member)))

    assert db.get_user_level(1) is None


def test_on_message_ignores_dm(cog, clock, isolated_db):
    member = _fake_member(1)
    msg = _fake_message(None, member)
    msg.guild = None
    _run(cog.on_message(msg))

    assert db.get_user_level(1) is None


def test_on_message_ignores_other_guild(cog, clock, isolated_db):
    guild = _fake_guild(guild_id=ALLOWED_GUILD_ID + 1)
    member = _fake_member(1)
    _run(cog.on_message(_fake_message(guild, member)))

    assert db.get_user_level(1) is None


def test_on_message_ignores_short_messages(cog, clock, isolated_db):
    guild = _fake_guild()
    member = _fake_member(1)
    curta = "k" * (MIN_XP_MESSAGE_LENGTH - 1)
    _run(cog.on_message(_fake_message(guild, member, content=curta)))

    assert db.get_user_level(1) is None


def test_on_message_grants_xp_for_valid_message(cog, clock, isolated_db, monkeypatch):
    monkeypatch.setattr("cogs.leveling.random.randint", lambda a, b: 20)
    guild = _fake_guild()
    member = _fake_member(1)

    _run(cog.on_message(_fake_message(guild, member, content="mensagem valida")))

    dados = db.get_user_level(1)
    assert dados is not None
    assert dados["xp"] == 20


def test_on_message_respects_cooldown_between_grants(cog, clock, isolated_db, monkeypatch):
    monkeypatch.setattr("cogs.leveling.random.randint", lambda a, b: 20)
    guild = _fake_guild()
    member = _fake_member(1)

    _run(cog.on_message(_fake_message(guild, member, content="primeira mensagem")))
    clock.advance(XP_COOLDOWN_SECONDS - 1)
    _run(cog.on_message(_fake_message(guild, member, content="segunda mensagem rapida")))

    # A segunda mensagem caiu dentro do cooldown - nao pode ter concedido XP de novo.
    assert db.get_user_level(1)["xp"] == 20


def test_on_message_grants_again_after_cooldown_expires(cog, clock, isolated_db, monkeypatch):
    monkeypatch.setattr("cogs.leveling.random.randint", lambda a, b: 20)
    guild = _fake_guild()
    member = _fake_member(1)

    _run(cog.on_message(_fake_message(guild, member, content="primeira mensagem")))
    clock.advance(XP_COOLDOWN_SECONDS + 1)
    _run(cog.on_message(_fake_message(guild, member, content="mensagem depois do cooldown")))

    assert db.get_user_level(1)["xp"] == 40


def test_on_message_announces_level_up(cog, clock, isolated_db, monkeypatch):
    monkeypatch.setattr("cogs.leveling.random.randint", lambda a, b: 1000)
    guild = _fake_guild()
    member = _fake_member(1)
    msg = _fake_message(guild, member, content="mensagem que fecha nivel")

    _run(cog.on_message(msg))

    msg.channel.send.assert_awaited_once()
    texto = msg.channel.send.call_args.args[0]
    assert member.mention in texto
    assert "nivel" in texto.lower()


def test_on_message_does_not_announce_without_level_up(cog, clock, isolated_db, monkeypatch):
    monkeypatch.setattr("cogs.leveling.random.randint", lambda a, b: 5)
    guild = _fake_guild()
    member = _fake_member(1)
    msg = _fake_message(guild, member, content="mensagem pequena de xp")

    _run(cog.on_message(msg))

    msg.channel.send.assert_not_awaited()


# ---------------------------------------------------------------------------------
# /rank
# ---------------------------------------------------------------------------------


def test_rank_command_without_xp_replies_ephemeral(isolated_db):
    user = _fake_member(1)
    interaction = _fake_interaction(user)

    _run(LevelingCog.rank.callback(object(), interaction, usuario=None))

    interaction.response.send_message.assert_awaited_once()
    _, kwargs = interaction.response.send_message.call_args
    assert "nao tem XP registrado" in interaction.response.send_message.call_args.args[0]
    assert kwargs.get("ephemeral") is True


def test_rank_command_shows_embed_for_user_with_xp(isolated_db):
    db.add_xp(user_id=1, display_name="Rizu", amount=150)
    user = _fake_member(1, display_name="Rizu")
    interaction = _fake_interaction(user)

    _run(LevelingCog.rank.callback(object(), interaction, usuario=None))

    interaction.response.send_message.assert_awaited_once()
    _, kwargs = interaction.response.send_message.call_args
    embed = kwargs["embed"]
    assert "Rizu" in embed.title
    campos = {f.name: f.value for f in embed.fields}
    assert campos["Nivel"] == "1"
    assert campos["Posicao"] == "#1 de 1"


def test_rank_command_for_another_user_is_not_ephemeral_when_no_data(isolated_db):
    quem_pergunta = _fake_member(1)
    outra_pessoa = _fake_member(2, display_name="Outra")
    interaction = _fake_interaction(quem_pergunta)

    _run(LevelingCog.rank.callback(object(), interaction, usuario=outra_pessoa))

    _, kwargs = interaction.response.send_message.call_args
    assert "Outra" in interaction.response.send_message.call_args.args[0]
    assert kwargs.get("ephemeral") is not True


# ---------------------------------------------------------------------------------
# /leaderboard
# ---------------------------------------------------------------------------------


def test_leaderboard_command_empty(isolated_db):
    interaction = _fake_interaction(_fake_member(1))

    _run(LevelingCog.leaderboard.callback(object(), interaction))

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "Ninguem" in args[0]
    assert kwargs.get("ephemeral") is True


def test_leaderboard_command_lists_top_users_in_order(isolated_db):
    db.add_xp(user_id=1, display_name="Primeiro", amount=500)
    db.add_xp(user_id=2, display_name="Segundo", amount=300)
    interaction = _fake_interaction(_fake_member(1))

    _run(LevelingCog.leaderboard.callback(object(), interaction))

    _, kwargs = interaction.response.send_message.call_args
    embed = kwargs["embed"]
    pos_primeiro = embed.description.find("Primeiro")
    pos_segundo = embed.description.find("Segundo")
    assert pos_primeiro != -1 and pos_segundo != -1
    assert pos_primeiro < pos_segundo
