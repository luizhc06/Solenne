from cogs.admin import (
    _validar_imagem_emoji,
    _validar_imagem_figurinha,
    EMOJI_MAX_BYTES,
    STICKER_MAX_BYTES,
)


class _FakeAttachment:
    """So o que os validadores olham - content_type e size - sem precisar de um
    discord.Attachment de verdade (que exige uma resposta HTTP real por baixo)."""

    def __init__(self, content_type: str | None, size: int):
        self.content_type = content_type
        self.size = size


def test_validar_imagem_emoji_aceita_png_dentro_do_limite():
    assert _validar_imagem_emoji(_FakeAttachment("image/png", 10_000)) is None


def test_validar_imagem_emoji_aceita_gif_animado():
    """Emoji aceita GIF (animado) - diferente de figurinha, que nao aceita."""
    assert _validar_imagem_emoji(_FakeAttachment("image/gif", 50_000)) is None


def test_validar_imagem_emoji_rejeita_arquivo_que_nao_e_imagem():
    erro = _validar_imagem_emoji(_FakeAttachment("video/mp4", 10_000))
    assert erro is not None
    assert "video/mp4" in erro


def test_validar_imagem_emoji_rejeita_acima_do_limite_do_discord():
    erro = _validar_imagem_emoji(_FakeAttachment("image/png", EMOJI_MAX_BYTES + 1))
    assert erro is not None
    assert "256" in erro


def test_validar_imagem_emoji_aceita_no_limite_exato():
    assert _validar_imagem_emoji(_FakeAttachment("image/png", EMOJI_MAX_BYTES)) is None


def test_validar_imagem_emoji_sem_content_type_nao_bloqueia():
    """Discord as vezes nao manda content_type pro anexo - sem informacao pra checar,
    deixa passar (o proprio Discord valida na hora de criar, ver create_custom_emoji)."""
    assert _validar_imagem_emoji(_FakeAttachment(None, 10_000)) is None


def test_validar_imagem_figurinha_aceita_png_dentro_do_limite():
    assert _validar_imagem_figurinha(_FakeAttachment("image/png", 100_000)) is None


def test_validar_imagem_figurinha_aceita_apng():
    assert _validar_imagem_figurinha(_FakeAttachment("image/apng", 100_000)) is None


def test_validar_imagem_figurinha_rejeita_gif():
    """Diferente de emoji: o Discord NAO aceita GIF pra figurinha customizada via API,
    so PNG/APNG (ou Lottie em JSON, que este comando nao cobre)."""
    erro = _validar_imagem_figurinha(_FakeAttachment("image/gif", 100_000))
    assert erro is not None
    assert "image/gif" in erro


def test_validar_imagem_figurinha_rejeita_acima_do_limite_do_discord():
    erro = _validar_imagem_figurinha(_FakeAttachment("image/png", STICKER_MAX_BYTES + 1))
    assert erro is not None
    assert "512" in erro
