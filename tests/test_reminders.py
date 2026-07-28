from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cogs.reminders import parse_when

TZ = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 7, 28, 14, 30, tzinfo=TZ)  # terca-feira


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
