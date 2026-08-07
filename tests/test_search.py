from cogs.search import _extract_real_url


def test_extract_real_url_from_ddg_redirect():
    ddg_href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert _extract_real_url(ddg_href) == "https://example.com/page"


def test_extract_real_url_passthrough_for_direct_url():
    assert _extract_real_url("https://example.com/direct") == "https://example.com/direct"
