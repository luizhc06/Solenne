"""Testes de integracao pra maquina de estados do automod.

check_flood() e check_mentions() (cogs/moderation.py) sao o codigo que de fato decide
quando avisar/apagar/punir com base em estado compartilhado entre mensagens (deques de
historico + cooldowns). Antes deste arquivo, so as pecas puras (mention_counts,
mention_verdict) e punish() isolado tinham teste - o proprio bug documentado no topo do
arquivo (regra 1 do flood punindo sem avisar) so foi descoberto em producao. Aqui a
sequencia de mensagens e simulada de verdade, avancando um relogio falso, pra pegar
regressao de janela/cooldown ANTES de producao.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.moderation import (
    FLOOD_MAX_DUPLICATES,
    FLOOD_MAX_MESSAGES,
    FLOOD_WARN_COOLDOWN_SECONDS,
    ModerationCog,
)
from config import ALLOWED_GUILD_ID, OWNER_USER_ID


class FakeClock:
    """Substitui time.monotonic() nos testes pra controlar a passagem do tempo sem
    depender de sleep real - flood e mencao dependem de janelas de poucos segundos."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t

    def __call__(self) -> float:
        return self.t


def _fake_bot(owner):
    bot = MagicMock()
    bot.get_user = MagicMock(return_value=owner)
    bot.fetch_user = AsyncMock(return_value=owner)
    return bot


def _fake_guild(guild_id=ALLOWED_GUILD_ID):
    guild = MagicMock()
    guild.id = guild_id
    guild.name = "Servidor Teste"
    return guild


def _fake_member(member_id, bot=False, admin=False):
    member = MagicMock()
    member.id = member_id
    member.bot = bot
    member.guild_permissions.administrator = admin
    member.mention = f"<@{member_id}>"
    member.display_name = f"membro{member_id}"
    member.timeout = AsyncMock()
    return member


def _fake_message(guild, member, content=""):
    message = MagicMock()
    message.guild = guild
    message.author = member
    message.content = content
    message.reply = AsyncMock()
    message.delete = AsyncMock()
    return message


@pytest.fixture()
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr("cogs.moderation.time.monotonic", fake)
    return fake


@pytest.fixture()
def cog():
    owner = MagicMock()
    owner.send = AsyncMock()
    return ModerationCog(_fake_bot(owner))


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------------
# Flood regra 1: muitas mensagens seguidas
# ---------------------------------------------------------------------------------


def test_flood_regra1_primeiro_estouro_so_avisa_sem_apagar_nem_punir(cog, clock):
    """O bug real (achado do conselho, 18/08/2026): a regra 1 apagava e dava timeout na
    hora, sem aviso algum - facil de bater organicamente numa sequencia de reacoes
    empolgadas. A primeira vez que o limiar e atingido tem que SO avisar."""
    guild = _fake_guild()
    member = _fake_member(1)

    msg = None
    for i in range(FLOOD_MAX_MESSAGES):
        msg = _fake_message(guild, member, content=f"mensagem {i}")
        _run(cog.check_flood(msg))
        clock.advance(0.1)

    msg.reply.assert_awaited_once()
    msg.delete.assert_not_awaited()
    member.timeout.assert_not_awaited()


def test_flood_regra1_persistindo_dentro_do_cooldown_pune(cog, clock):
    """Se a pessoa continua no mesmo ritmo DEPOIS do aviso (ainda dentro do cooldown de
    aviso), a proxima mensagem que reestoura o limiar tem que punir de verdade."""
    guild = _fake_guild()
    member = _fake_member(2)

    for i in range(FLOOD_MAX_MESSAGES):
        _run(cog.check_flood(_fake_message(guild, member, content=f"mensagem {i}")))
        clock.advance(0.1)
    # nesse ponto ja foi so avisado (ver teste acima) - continua no mesmo ritmo:
    clock.advance(0.1)
    punicao_msg = _fake_message(guild, member, content="mais uma")
    _run(cog.check_flood(punicao_msg))

    punicao_msg.reply.assert_not_awaited()
    member.timeout.assert_awaited_once()


def test_flood_regra1_depois_do_cooldown_volta_a_avisar_primeiro(cog, clock):
    """O cooldown de aviso precisa resetar: depois que ele expira, um novo estouro do
    limiar tem que avisar de novo antes de punir - nao pode ficar punindo pra sempre."""
    guild = _fake_guild()
    member = _fake_member(3)

    for i in range(FLOOD_MAX_MESSAGES):
        _run(cog.check_flood(_fake_message(guild, member, content=f"mensagem {i}")))
        clock.advance(0.1)

    # deixa o cooldown de aviso expirar e a janela de flood tambem esvaziar
    clock.advance(FLOOD_WARN_COOLDOWN_SECONDS + 1)

    msg = None
    for i in range(FLOOD_MAX_MESSAGES):
        msg = _fake_message(guild, member, content=f"nova rodada {i}")
        _run(cog.check_flood(msg))
        clock.advance(0.1)

    msg.reply.assert_awaited_once()
    member.timeout.assert_not_awaited()


def test_flood_ignora_bot_dono_e_administrador(cog, clock):
    """check_flood tem que sair cedo pra bot, dono do bot e administrador - nenhum
    desses pode acumular estado nem ser avisado/punido."""
    guild = _fake_guild()
    for member in (
        _fake_member(999, bot=True),
        _fake_member(OWNER_USER_ID),
        _fake_member(4, admin=True),
    ):
        msg = None
        for i in range(FLOOD_MAX_MESSAGES):
            msg = _fake_message(guild, member, content=f"mensagem {i}")
            _run(cog.check_flood(msg))
            clock.advance(0.1)

        msg.reply.assert_not_awaited()
        msg.delete.assert_not_awaited()
        member.timeout.assert_not_awaited()


# ---------------------------------------------------------------------------------
# Flood regra 2: mensagem repetida - pune direto, sem estagio de aviso
# ---------------------------------------------------------------------------------


def test_flood_regra2_mensagem_repetida_pune_sem_avisar(cog, clock):
    assert FLOOD_MAX_DUPLICATES < FLOOD_MAX_MESSAGES  # senao a regra 1 dispara primeiro
    guild = _fake_guild()
    member = _fake_member(5)

    msg = None
    for _ in range(FLOOD_MAX_DUPLICATES):
        msg = _fake_message(guild, member, content="kkkkkkkk")
        _run(cog.check_flood(msg))
        clock.advance(0.1)

    msg.reply.assert_not_awaited()
    member.timeout.assert_awaited_once()


# ---------------------------------------------------------------------------------
# Mencoes: check_flood roteia pra check_mentions (regra 3) quando 1 e 2 nao disparam
# ---------------------------------------------------------------------------------


def test_flood_roteia_mensagem_normal_com_mencao_pra_check_mentions(cog, clock):
    """Uma mensagem isolada, sem estourar flood, ainda passa pela checagem de mencao
    (regra 3) - e o encadeamento real que on_message usa em producao."""
    guild = _fake_guild()
    member = _fake_member(6)
    alvo = "111"

    _run(cog.check_flood(_fake_message(guild, member, content=f"<@{alvo}>")))

    assert (guild.id, member.id, alvo) in cog.mention_log


def test_mencao_avisa_na_segunda_e_pune_na_terceira_mensagem_seguida(cog, clock):
    """Reproduz o fluxo real: 1a mencao nao faz nada, 2a avisa, 3a pune - e o historico
    e por deque/cooldown compartilhado entre mensagens, nao um calculo isolado."""
    guild = _fake_guild()
    member = _fake_member(7)
    alvo_msg = f"<@111>"

    m1 = _fake_message(guild, member, content=alvo_msg)
    _run(cog.check_mentions(m1))
    m1.reply.assert_not_awaited()
    member.timeout.assert_not_awaited()

    clock.advance(1)
    m2 = _fake_message(guild, member, content=alvo_msg)
    _run(cog.check_mentions(m2))
    m2.reply.assert_awaited_once()
    member.timeout.assert_not_awaited()

    clock.advance(1)
    m3 = _fake_message(guild, member, content=alvo_msg)
    _run(cog.check_mentions(m3))
    member.timeout.assert_awaited_once()
    m3.delete.assert_awaited()

    # depois de punir, o historico daquele alvo e limpo - nao pode repunir na sequencia
    # seguinte so porque sobrou contagem acumulada de antes.
    assert (guild.id, member.id, "111") not in cog.mention_log


def test_mencao_nao_repete_aviso_dentro_da_janela(cog, clock):
    """Uma vez avisado, nao pode ficar mandando o mesmo aviso a cada mensagem dentro da
    janela - so quando o pico muda pra punir (testado acima) ou a janela expira."""
    guild = _fake_guild()
    member = _fake_member(8)
    alvo_msg = f"<@222>"

    _run(cog.check_mentions(_fake_message(guild, member, content=alvo_msg)))
    clock.advance(1)
    m2 = _fake_message(guild, member, content=alvo_msg)
    _run(cog.check_mentions(m2))
    m2.reply.assert_awaited_once()

    clock.advance(1)
    m3 = _fake_message(guild, member, content="oi de novo sem mencionar")
    _run(cog.check_mentions(m3))
    m3.reply.assert_not_awaited()
