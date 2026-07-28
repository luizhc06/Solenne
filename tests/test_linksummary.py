import pytest

from cogs.linksummary import extract_text, ensure_public_http_url, UnsafeURLError


def test_extract_text_drops_scripts_and_tags():
    html = """
    <html><head><title>Titulo da Pagina</title>
    <style>body { color: red; }</style></head>
    <body><script>alert('x')</script><p>Primeiro paragrafo.</p><p>Segundo.</p></body></html>
    """
    title, text = extract_text(html)
    assert title == "Titulo da Pagina"
    assert "Primeiro paragrafo." in text
    assert "Segundo." in text
    assert "alert" not in text
    assert "color: red" not in text


def test_extract_text_unescapes_entities():
    _, text = extract_text("<p>caf&eacute; &amp; leite</p>")
    assert "café & leite" in text


def test_extract_text_without_title():
    title, text = extract_text("<p>so corpo</p>")
    assert title == ""
    assert "so corpo" in text


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/opc/v2/instance/",  # metadados da VM na Oracle Cloud
        "http://127.0.0.1:8080/admin",
        "http://localhost/",
        "http://10.0.0.5/interno",
        "http://192.168.1.1/",
    ],
)
def test_rejects_internal_addresses(url):
    """O bot roda numa VM cujo endpoint de metadados serve credenciais - resumir
    um link interno vazaria isso pra qualquer pessoa do servidor."""
    with pytest.raises(UnsafeURLError):
        ensure_public_http_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)"])
def test_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeURLError):
        ensure_public_http_url(url)


def test_rejects_url_without_host():
    with pytest.raises(UnsafeURLError):
        ensure_public_http_url("http://")
