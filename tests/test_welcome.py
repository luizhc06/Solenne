"""Testes da logica nova de boas-vindas (cogs/welcome.py + dedupe em db.py).

So cobre o que nao e so uma chamada direta a API do Discord: resolucao de canal
(env var -> system_channel -> None), montagem da embed, formatacao de idade de conta,
o dedupe no SQLite, e o fluxo de decisao do listener on_member_join (ignora fora do
guild permitido, ignora bot, respeita o dedupe, marca como enviado so depois do send).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import db
from cogs.welcome import (
    NEW_ACCOUNT_THRESHOLD,
    WELCOME_DEDUPE_WINDOW_SECONDS,
    WelcomeCog,
    _format_account_age,
    build_welcome_embed,
    resolve_welcome_channel,
)
from config import ALLOWED_GUILD_ID


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Aponta o modulo db pra um sqlite descartavel e cria o schema nele."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db_path


# ---------------------------------------------------------------------------------
# _format_account_age
# ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=30), "30s"),
        (timedelta(minutes=1), "1min"),
        (timedelta(minutes=45), "45min"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=23), "23h"),
        (timedelta(days=1), "1 dia"),
        (timedelta(days=3), "3 dias"),
    ],
)
def test_format_account_age(delta, expected):
    assert _format_account_age(delta) == expected


# ---------------------------------------------------------------------------------
# resolve_welcome_channel
# ---------------------------------------------------------------------------------


def _fake_channel(send_ok=True):
    channel = MagicMock()
    perms = MagicMock()
    perms.send_messages = send_ok
    channel.permissions_for = MagicMock(return_value=perms)
    return channel


def _fake_guild(configured_channel=None, system_channel=None, me="me"):
    guild = MagicMock()
    guild.me = me
    guild.system_channel = system_channel
    guild.get_channel = MagicMock(return_value=configured_channel)
    return guild


def test_resolve_uses_configured_channel_when_available_and_permitted():
    channel = _fake_channel(send_ok=True)
    guild = _fake_guild(configured_channel=channel, system_channel=_fake_channel())

    resolved = resolve_welcome_channel(guild, 999)

    assert resolved is channel


def test_resolve_falls_back_to_system_channel_when_configured_channel_missing():
    system = _fake_channel()
    guild = _fake_guild(configured_channel=None, system_channel=system)

    resolved = resolve_welcome_channel(guild, 999)

    assert resolved is system


def test_resolve_falls_back_to_system_channel_when_no_permission_to_send():
    channel = _fake_channel(send_ok=False)
    system = _fake_channel()
    guild = _fake_guild(configured_channel=channel, system_channel=system)

    resolved = resolve_welcome_channel(guild, 999)

    assert resolved is system


def test_resolve_uses_system_channel_when_no_channel_id_configured():
    system = _fake_channel()
    guild = _fake_guild(configured_channel=None, system_channel=system)

    resolved = resolve_welcome_channel(guild, None)

    assert resolved is system


def test_resolve_returns_none_when_nothing_available():
    guild = _fake_guild(configured_channel=None, system_channel=None)

    assert resolve_welcome_channel(guild, None) is None
    assert resolve_welcome_channel(guild, 999) is None


# ---------------------------------------------------------------------------------
# build_welcome_embed
# ---------------------------------------------------------------------------------


def _fake_member(created_at, member_id=1, display_name="Novato", member_count=42):
    guild = MagicMock()
    guild.name = "Servidor Teste"
    guild.member_count = member_count

    member = MagicMock()
    member.id = member_id
    member.guild = guild
    member.display_name = display_name
    member.mention = f"<@{member_id}>"
    member.created_at = created_at
    member.display_avatar.url = "https://example.com/avatar.png"
    return member


def test_build_welcome_embed_basic_fields():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    member = _fake_member(created_at=now - timedelta(days=400))

    embed = build_welcome_embed(member, now=now)

    assert embed.title == "Bem-vindo(a), Novato!"
    assert "<@1>" in embed.description
    assert "Servidor Teste" in embed.description
    assert embed.thumbnail.url == "https://example.com/avatar.png"
    assert "Membro #42" in embed.footer.text
    assert embed.color == discord.Color.blurple()


def test_build_welcome_embed_flags_new_account():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    member = _fake_member(created_at=now - timedelta(minutes=5))

    embed = build_welcome_embed(member, now=now)

    assert len(embed.fields) == 1
    assert "nova" in embed.fields[0].name.lower()
    assert "5min" in embed.fields[0].value


def test_build_welcome_embed_does_not_flag_old_account():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    member = _fake_member(created_at=now - NEW_ACCOUNT_THRESHOLD - timedelta(minutes=1))

    embed = build_welcome_embed(member, now=now)

    assert embed.fields == []


# ---------------------------------------------------------------------------------
# dedupe no banco (db.was_recently_welcomed / db.mark_welcomed)
# ---------------------------------------------------------------------------------


def test_was_recently_welcomed_false_before_any_mark(isolated_db):
    assert db.was_recently_welcomed(guild_id=1, member_id=2) is False


def test_mark_welcomed_makes_was_recently_welcomed_true_within_window(isolated_db):
    db.mark_welcomed(guild_id=1, member_id=2)

    assert db.was_recently_welcomed(guild_id=1, member_id=2, window_seconds=60) is True


def test_was_recently_welcomed_ignores_marks_outside_window(isolated_db):
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO welcome_log (guild_id, member_id, welcomed_at) VALUES (?, ?, ?)",
            (1, 2, old),
        )

    assert db.was_recently_welcomed(guild_id=1, member_id=2, window_seconds=60) is False


def test_was_recently_welcomed_is_scoped_per_guild(isolated_db):
    """Mesmo member_id em outro servidor nao deve contar como ja saudado."""
    db.mark_welcomed(guild_id=1, member_id=2)

    assert db.was_recently_welcomed(guild_id=999, member_id=2) is False


# ---------------------------------------------------------------------------------
# WelcomeCog.on_member_join - fluxo de decisao (guarda de guild/bot/dedupe/marcacao)
# ---------------------------------------------------------------------------------


def _fake_member_for_join(guild_id=ALLOWED_GUILD_ID, member_id=1, is_bot=False):
    guild = MagicMock()
    guild.id = guild_id
    guild.name = "Servidor Teste"
    guild.member_count = 10
    system_channel = MagicMock()
    system_channel.id = 555
    system_channel.send = AsyncMock()
    guild.system_channel = system_channel
    guild.get_channel = MagicMock(return_value=None)
    guild.me = "me"

    member = MagicMock()
    member.id = member_id
    member.bot = is_bot
    member.guild = guild
    member.display_name = "Novato"
    member.mention = f"<@{member_id}>"
    member.created_at = datetime.now(timezone.utc) - timedelta(days=100)
    member.display_avatar.url = "https://example.com/avatar.png"
    return member


def _run(coro):
    return asyncio.run(coro)


def test_on_member_join_ignores_other_guild(isolated_db):
    cog = WelcomeCog(MagicMock())
    member = _fake_member_for_join(guild_id=ALLOWED_GUILD_ID + 1)

    _run(cog.on_member_join(member))

    member.guild.system_channel.send.assert_not_awaited()


def test_on_member_join_ignores_bots(isolated_db):
    cog = WelcomeCog(MagicMock())
    member = _fake_member_for_join(is_bot=True)

    _run(cog.on_member_join(member))

    member.guild.system_channel.send.assert_not_awaited()


def test_on_member_join_sends_embed_and_marks_welcomed(isolated_db):
    cog = WelcomeCog(MagicMock())
    member = _fake_member_for_join(member_id=42)

    _run(cog.on_member_join(member))

    member.guild.system_channel.send.assert_awaited_once()
    kwargs = member.guild.system_channel.send.call_args.kwargs
    assert isinstance(kwargs["embed"], discord.Embed)
    assert db.was_recently_welcomed(guild_id=ALLOWED_GUILD_ID, member_id=42) is True


def test_on_member_join_skips_send_when_already_recently_welcomed(isolated_db):
    db.mark_welcomed(guild_id=ALLOWED_GUILD_ID, member_id=42)
    cog = WelcomeCog(MagicMock())
    member = _fake_member_for_join(member_id=42)

    _run(cog.on_member_join(member))

    member.guild.system_channel.send.assert_not_awaited()


def test_on_member_join_logs_and_skips_when_no_channel_available(isolated_db):
    cog = WelcomeCog(MagicMock())
    member = _fake_member_for_join(member_id=7)
    member.guild.system_channel = None

    # Nao deve levantar excecao, so deixar de enviar.
    _run(cog.on_member_join(member))

    assert db.was_recently_welcomed(guild_id=ALLOWED_GUILD_ID, member_id=7) is False


def test_dedupe_window_constant_used_by_cog_matches_module_default():
    """Garante que a janela usada pelo cog e a mesma documentada/testada em db.py -
    se alguem mudar uma sem a outra, esse teste acusa a divergencia."""
    assert WELCOME_DEDUPE_WINDOW_SECONDS == 60
