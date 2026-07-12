from utils import looks_like_question


def test_looks_like_question_true_for_real_question():
    assert looks_like_question("Qual e a capital do Brasil?") is True


def test_looks_like_question_false_for_slash_command():
    assert looks_like_question("/ask oi?") is False


def test_looks_like_question_false_without_question_mark():
    assert looks_like_question("oi tudo bem") is False


def test_looks_like_question_false_too_short():
    assert looks_like_question("oi?") is False


def test_looks_like_question_ignores_question_mark_inside_url():
    assert looks_like_question("confere https://youtu.be/watch?v=abc123") is False
