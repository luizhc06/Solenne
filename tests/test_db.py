from datetime import datetime, timedelta, timezone

import pytest

import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Aponta o modulo db pra um sqlite descartavel e cria o schema nele."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


def _due(minutes=10):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_add_and_list_reminders(isolated_db):
    reminder_id = db.add_reminder(user_id=1, channel_id=100, text="beber agua", due_at=_due())

    pending = db.list_reminders(user_id=1)

    assert len(pending) == 1
    assert pending[0]["id"] == reminder_id
    assert pending[0]["text"] == "beber agua"


def test_list_reminders_only_returns_the_given_user(isolated_db):
    db.add_reminder(user_id=1, channel_id=100, text="lembrete de A", due_at=_due())
    db.add_reminder(user_id=2, channel_id=100, text="lembrete de B", due_at=_due())

    assert [r["text"] for r in db.list_reminders(user_id=1)] == ["lembrete de A"]
    assert [r["text"] for r in db.list_reminders(user_id=2)] == ["lembrete de B"]


def test_delete_reminder_removes_own_reminder(isolated_db):
    reminder_id = db.add_reminder(user_id=1, channel_id=100, text="tarefa", due_at=_due())

    removed = db.delete_reminder(reminder_id, user_id=1)

    assert removed is True
    assert db.list_reminders(user_id=1) == []


def test_delete_reminder_refuses_other_users_reminder(isolated_db):
    """Guard critico: o id do lembrete e sequencial e visivel (ex: /lembretes mostra
    #12), entao sem o filtro por user_id qualquer pessoa cancelaria o lembrete de
    outra so adivinhando o numero."""
    victim_reminder_id = db.add_reminder(
        user_id=111, channel_id=100, text="lembrete da vitima", due_at=_due()
    )

    removed = db.delete_reminder(victim_reminder_id, user_id=999)

    assert removed is False
    # O lembrete da vitima continua intacto.
    pending = db.list_reminders(user_id=111)
    assert len(pending) == 1
    assert pending[0]["id"] == victim_reminder_id


def test_delete_reminder_unknown_id_returns_false(isolated_db):
    assert db.delete_reminder(999999, user_id=1) is False


def test_pop_due_reminders_only_returns_and_marks_reminders_that_are_due(isolated_db):
    due_id = db.add_reminder(
        user_id=1, channel_id=100, text="vencido", due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    future_id = db.add_reminder(user_id=1, channel_id=100, text="futuro", due_at=_due(minutes=60))

    due = db.pop_due_reminders()

    assert [r["id"] for r in due] == [due_id]
    # Marcado como entregue -> some do /lembretes e nao volta a ser "popped" de novo.
    assert [r["id"] for r in db.list_reminders(user_id=1)] == [future_id]
    assert db.pop_due_reminders() == []


def test_delete_reminder_does_not_remove_already_delivered_reminder(isolated_db):
    """Depois de entregue, /cancelarlembrete nao deve conseguir 'reviver' ou apagar
    um lembrete que ja foi disparado."""
    reminder_id = db.add_reminder(
        user_id=1, channel_id=100, text="vencido", due_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    db.pop_due_reminders()

    removed = db.delete_reminder(reminder_id, user_id=1)

    assert removed is False
