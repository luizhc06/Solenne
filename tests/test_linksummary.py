import socket

import pytest

from cogs.linksummary import extract_text, ensure_public_http_url, UnsafeURLError, _fetch_page_sync


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


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b"<title>t</title><p>x</p>"):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.encoding = "utf-8"

    @property
    def has_redirect_location(self):
        return 300 <= self.status_code <= 399 and "location" in self.headers

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield self._body


class _FakeStreamCM:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc_info):
        return False


_REAL_GETADDRINFO = socket.getaddrinfo


def _fake_getaddrinfo_public(hostname, *_args, **_kwargs):
    if hostname == "safe.example.com":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    return _REAL_GETADDRINFO(hostname, *_args, **_kwargs)


def test_fetch_page_pins_resolved_ip_and_preserves_host(monkeypatch):
    """A conexao real deve ir pro IP ja validado (nao re-resolver o hostname), pra
    fechar a janela de TOCTOU entre a checagem de DNS e a requisicao."""
    import cogs.linksummary as linksummary

    monkeypatch.setattr(linksummary.socket, "getaddrinfo", _fake_getaddrinfo_public)

    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _FakeStreamCM(_FakeResponse(headers={"content-type": "text/html"}))

    monkeypatch.setattr(linksummary.httpx, "stream", fake_stream)

    title, text = linksummary._fetch_page_sync("http://safe.example.com/page")

    assert len(calls) == 1
    _, called_url, kwargs = calls[0]
    assert called_url.startswith("http://93.184.216.34:80/page")
    assert kwargs["headers"]["Host"] == "safe.example.com"
    assert kwargs["extensions"]["sni_hostname"] == "safe.example.com"
    assert kwargs["follow_redirects"] is False
    assert title == "t"


def test_fetch_page_blocks_redirect_to_internal_address(monkeypatch):
    """Um dominio publico nao pode escapar da checagem respondendo 302 pra um IP
    interno (ex.: o endpoint de metadados da Oracle Cloud)."""
    import cogs.linksummary as linksummary

    monkeypatch.setattr(linksummary.socket, "getaddrinfo", _fake_getaddrinfo_public)

    calls = []

    def fake_stream(method, url, **kwargs):
        calls.append(url)
        return _FakeStreamCM(
            _FakeResponse(
                status_code=302,
                headers={"location": "http://169.254.169.254/opc/v2/instance/"},
            )
        )

    monkeypatch.setattr(linksummary.httpx, "stream", fake_stream)

    with pytest.raises(UnsafeURLError):
        linksummary._fetch_page_sync("http://safe.example.com/page")

    # nao deve nem tentar conectar no destino interno do redirect
    assert len(calls) == 1


def test_fetch_page_gives_up_after_too_many_redirects(monkeypatch):
    import cogs.linksummary as linksummary

    monkeypatch.setattr(linksummary.socket, "getaddrinfo", _fake_getaddrinfo_public)

    def fake_stream(method, url, **kwargs):
        return _FakeStreamCM(
            _FakeResponse(status_code=302, headers={"location": "http://safe.example.com/page"})
        )

    monkeypatch.setattr(linksummary.httpx, "stream", fake_stream)

    with pytest.raises(UnsafeURLError):
        linksummary._fetch_page_sync("http://safe.example.com/page")
