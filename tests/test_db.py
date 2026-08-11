import os
import tempfile

import pytest


@pytest.fixture()
def banco(monkeypatch):
    """Banco temporario: os testes nao podem tocar no solenne.db de producao."""
    import db

    with tempfile.TemporaryDirectory() as pasta:
        caminho = os.path.join(pasta, "teste.db")
        monkeypatch.setattr(db, "DB_PATH", caminho)
        db.init_db()
        yield db


def test_search_history_encontra_mensagem_antiga(banco):
    """O historico inteiro sempre esteve no SQLite, mas so as ultimas 20 mensagens eram
    alcancaveis - "o que a gente falou sobre X mes passado" nao tinha resposta possivel."""
    banco.save_message(1, "user", "rizu", "vamos usar postgres no carbonlog")
    for i in range(30):
        banco.save_message(1, "user", "outro", f"mensagem de enchimento {i}")

    achados = banco.search_history(1, "postgres")
    assert len(achados) == 1
    assert achados[0]["autor"] == "rizu"
    assert "postgres" in achados[0]["conteudo"]


def test_search_history_nao_vaza_outro_canal(banco):
    banco.save_message(1, "user", "rizu", "segredo do canal um")
    assert banco.search_history(2, "segredo") == []


def test_search_history_identifica_falas_da_solenne(banco):
    banco.save_message(1, "assistant", None, "eu disse que postgres era melhor")
    assert banco.search_history(1, "postgres")[0]["autor"] == "Solenne"


def test_search_history_devolve_do_mais_recente_pro_mais_antigo(banco):
    banco.save_message(1, "user", "rizu", "postgres versao 1")
    banco.save_message(1, "user", "rizu", "postgres versao 2")
    achados = banco.search_history(1, "postgres")
    assert achados[0]["conteudo"].endswith("2")


def test_search_history_com_termo_vazio_nao_devolve_tudo(banco):
    banco.save_message(1, "user", "rizu", "qualquer coisa")
    assert banco.search_history(1, "") == []
    assert banco.search_history(1, "   ") == []


def test_search_history_nao_deixa_curinga_do_like_vazar(banco):
    """Procurar por "100%" nao pode virar "casa com tudo" - % e _ sao curingas do LIKE."""
    banco.save_message(1, "user", "rizu", "a bateria chegou a 100% ontem")
    banco.save_message(1, "user", "rizu", "assunto totalmente diferente")
    achados = banco.search_history(1, "100%")
    assert len(achados) == 1
    assert "bateria" in achados[0]["conteudo"]
