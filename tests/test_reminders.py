import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

import db
from cogs.reminders import REMINDER_MAX_PER_USER, RemindersCog, parse_when

TZ = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 7, 28, 14, 30, tzinfo=TZ)  # terca-feira


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Aponta o modulo db pra um sqlite descartavel e cria o schema nele."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


def _fake_interaction(user_id):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.channel_id = 555
    interaction.response.send_message = AsyncMock()
    return interaction


@pytest.mark.parametrize(
    "text,expected_delta",
    [
        ("30m", timedelta(minutes=30)),
        ("45min", timedelta(minutes=45)),
        ("2h", timedelta(hours=2)),
        ("3 dias", timedelta(days=3)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("em 20m", timedelta(minutes=20)),
        ("daqui a 2h", timedelta(hours=2)),
    ],
)
def test_parse_relative_durations(text, expected_delta):
    assert parse_when(text, NOW) == NOW + expected_delta


def test_parse_amanha_with_hour():
    assert parse_when("amanha as 9h", NOW) == datetime(2026, 7, 29, 9, 0, tzinfo=TZ)


def test_parse_amanha_defaults_to_morning():
    assert parse_when("amanha", NOW) == datetime(2026, 7, 29, 9, 0, tzinfo=TZ)


def test_parse_accepts_accented_amanha():
    assert parse_when("amanhã as 9h", NOW) == datetime(2026, 7, 29, 9, 0, tzinfo=TZ)


def test_parse_hoje_with_time():
    assert parse_when("hoje as 18:30", NOW) == datetime(2026, 7, 28, 18, 30, tzinfo=TZ)


def test_parse_explicit_date():
    assert parse_when("25/12 10:00", NOW) == datetime(2026, 12, 25, 10, 0, tzinfo=TZ)


def test_parse_date_already_passed_rolls_to_next_year():
    """Data sem ano que ja passou deve cair no ano seguinte, nao no passado."""
    assert parse_when("01/01 10:00", NOW) == datetime(2027, 1, 1, 10, 0, tzinfo=TZ)


def test_parse_explicit_year_is_respected():
    assert parse_when("25/12/2027 10:00", NOW) == datetime(2027, 12, 25, 10, 0, tzinfo=TZ)


def test_parse_bare_clock_picks_next_occurrence():
    # 18:30 ainda nao chegou hoje (agora e 14:30) - e hoje mesmo.
    assert parse_when("18:30", NOW) == datetime(2026, 7, 28, 18, 30, tzinfo=TZ)
    # 09:00 ja passou hoje - so amanha.
    assert parse_when("09:00", NOW) == datetime(2026, 7, 29, 9, 0, tzinfo=TZ)


def test_bare_hours_is_duration_not_clock():
    """"9h" sozinho e "daqui a 9 horas", a leitura mais comum num lembrete."""
    assert parse_when("9h", NOW) == NOW + timedelta(hours=9)


def test_as_prefix_forces_clock_reading():
    """"as 9h" e a forma explicita de pedir horario em vez de duracao."""
    assert parse_when("as 9h", NOW) == datetime(2026, 7, 29, 9, 0, tzinfo=TZ)
    assert parse_when("às 18:30", NOW) == datetime(2026, 7, 28, 18, 30, tzinfo=TZ)


def test_parse_rejects_plain_text():
    """Texto solto nao pode virar horario - "5 coisas" nao e "em 5 segundos"."""
    assert parse_when("quando der", NOW) is None
    assert parse_when("5 coisas pra fazer", NOW) is None
    assert parse_when("", NOW) is None


def test_parse_rejects_impossible_clock():
    assert parse_when("99:99", NOW) is None
    assert parse_when("32/13 10:00", NOW) is None


def test_cancelarlembrete_refuses_to_cancel_another_users_reminder(isolated_db):
    """Integracao de ponta a ponta do comando /cancelarlembrete: o id do lembrete e
    sequencial e visivel em /lembretes, entao sem o guard de posse em delete_reminder
    qualquer pessoa cancelaria o lembrete de outra so adivinhando o numero."""
    due_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    reminder_id = db.add_reminder(user_id=111, channel_id=1, text="lembrete da vitima", due_at=due_at)

    attacker = _fake_interaction(user_id=999)
    asyncio.run(RemindersCog.cancelarlembrete.callback(object(), attacker, id=reminder_id))

    attacker.response.send_message.assert_awaited_once()
    sent_text = attacker.response.send_message.call_args.args[0]
    assert "Nao achei" in sent_text

    # O lembrete da vitima sobrevive intacto.
    pending = db.list_reminders(user_id=111)
    assert len(pending) == 1
    assert pending[0]["id"] == reminder_id


def test_cancelarlembrete_lets_owner_cancel_their_own_reminder(isolated_db):
    due_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    reminder_id = db.add_reminder(user_id=111, channel_id=1, text="meu lembrete", due_at=due_at)

    owner = _fake_interaction(user_id=111)
    asyncio.run(RemindersCog.cancelarlembrete.callback(object(), owner, id=reminder_id))

    owner.response.send_message.assert_awaited_once()
    sent_text = owner.response.send_message.call_args.args[0]
    assert "cancelado" in sent_text
    assert db.list_reminders(user_id=111) == []


def test_lembrete_command_blocks_after_reaching_the_per_user_limit(isolated_db):
    due_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    for _ in range(REMINDER_MAX_PER_USER):
        db.add_reminder(user_id=42, channel_id=1, text="ja existente", due_at=due_at)

    interaction = _fake_interaction(user_id=42)
    asyncio.run(
        RemindersCog.lembrete.callback(object(), interaction, quando="30m", oque="mais um")
    )

    interaction.response.send_message.assert_awaited_once()
    sent_text = interaction.response.send_message.call_args.args[0]
    assert "limite" in sent_text
    # Nao deve ter criado um lembrete a mais alem do limite.
    assert len(db.list_reminders(user_id=42)) == REMINDER_MAX_PER_USER
