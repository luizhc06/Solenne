from cogs.linkfix import _convert_links


def test_convert_links_twitter():
    assert _convert_links("olha isso https://twitter.com/user/status/123") == [
        "https://fxtwitter.com/user/status/123"
    ]


def test_convert_links_x_domain():
    assert _convert_links("https://x.com/user/status/123") == ["https://fxtwitter.com/user/status/123"]


def test_convert_links_instagram_reel():
    assert _convert_links("https://instagram.com/reel/abc123") == ["https://kkinstagram.com/reel/abc123"]


def test_convert_links_tiktok():
    assert _convert_links("https://www.tiktok.com/@user/video/123") == [
        "https://www.vxtiktok.com/@user/video/123"
    ]


def test_convert_links_multiple_in_one_message():
    content = "https://twitter.com/a/status/1 e tambem https://tiktok.com/@b/video/2"
    result = _convert_links(content)
    assert "https://fxtwitter.com/a/status/1" in result
    assert "https://vxtiktok.com/@b/video/2" in result


def test_convert_links_ignores_unrelated_links():
    assert _convert_links("https://example.com/page") == []


def test_convert_links_ignores_plain_text():
    assert _convert_links("oi tudo bem?") == []
