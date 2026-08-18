import asyncio
from unittest.mock import AsyncMock, MagicMock

from cogs.moderation import ModerationCog, ModerationView


def _fake_bot(owner):
    bot = MagicMock()
    bot.get_user = MagicMock(return_value=owner)
    bot.fetch_user = AsyncMock(return_value=owner)
    return bot


def _fake_message(guild, member, content="mensagem de teste"):
    message = MagicMock()
    message.guild = guild
    message.author = member
    message.content = content
    message.delete = AsyncMock()
    return message


def test_punish_completa_sem_excecao_e_manda_dm_pro_dono():
    """Reproduz o bug achado pelo conselho de agentes (18/08/2026): notify_owner() era
    chamado de dentro de punish() com timeout_seconds=... mas a assinatura da funcao
    nao aceitava esse parametro - TypeError garantido em TODO acionamento do automod
    (flood, duplicata ou mencao), e a DM pro dono com os botoes Banir/Ignorar nunca
    saia. O bug nao tinha teste cobrindo punish()/notify_owner() de ponta a ponta -
    so as funcoes puras de contagem de mencao - e foi isso que deixou passar.
    """
    owner = MagicMock()
    owner.send = AsyncMock()
    bot = _fake_bot(owner)
    cog = ModerationCog(bot)

    guild = MagicMock()
    guild.id = 111
    guild.name = "Servidor Teste"

    member = MagicMock()
    member.id = 222
    member.mention = "@membro"
    member.timeout = AsyncMock()

    message = _fake_message(guild, member)

    # Nao pode lancar excecao nenhuma - e exatamente isso que o bug quebrava.
    asyncio.run(cog.punish(message, "flood de teste", timeout_seconds=90))

    member.timeout.assert_awaited_once()
    owner.send.assert_awaited_once()

    kwargs = owner.send.call_args.kwargs
    embed = kwargs["embed"]
    assert "90s" in embed.description  # timeout real aplicado, nao um valor fixo generico

    view = kwargs["view"]
    assert isinstance(view, ModerationView)
    assert view.timeout_seconds == 90  # chega ate o botao "Ignorar" tambem


def test_botao_ignorar_mostra_o_timeout_real_aplicado():
    """Achado secundario do mesmo bug: o botao "Ignorar" mostrava sempre TIMEOUT_SECONDS
    (60s) fixo, mesmo quando a punicao real foi outra (ex: 600s pra insistencia em
    mencao) - porque ModerationView nao recebia o valor real."""
    guild = MagicMock()
    member = MagicMock()
    member.__str__ = MagicMock(return_value="membro#0001")

    async def cenario():
        return ModerationView(guild, member, "insistencia em mencao", timeout_seconds=600)

    view = asyncio.run(cenario())
    assert view.timeout_seconds == 600
