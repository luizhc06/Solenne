from datetime import datetime, timezone

from cogs.news import _summarize_anilist_entries, interleave_by_source


def _item(source, link, hours_ago=None):
    published = None
    if hours_ago is not None:
        published = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc).replace(
            hour=12 - hours_ago
        )
    return {"source": source, "link": link, "published": published}


def test_interleave_alternates_between_sources():
    """Antes isso era um extend sequencial cortado no fim: como cada feed devolve ate
    10 itens e o corte era em 8, a segunda fonte da categoria era descartada inteira
    antes da IA ver. O rodizio garante que toda fonte configurada disputa vaga."""
    feed_a = [_item("A", f"a{i}", hours_ago=i) for i in range(5)]
    feed_b = [_item("B", f"b{i}", hours_ago=i) for i in range(5)]

    merged = interleave_by_source([feed_a, feed_b])

    assert [it["source"] for it in merged[:4]] == ["A", "B", "A", "B"]
    # Mesmo cortando em 4 candidatos, as duas fontes aparecem.
    assert {it["source"] for it in merged[:4]} == {"A", "B"}


def test_interleave_sorts_each_source_by_recency():
    feed = [_item("A", "old", hours_ago=5), _item("A", "new", hours_ago=1)]
    merged = interleave_by_source([feed])
    assert [it["link"] for it in merged] == ["new", "old"]


def test_interleave_handles_uneven_and_empty_feeds():
    feed_a = [_item("A", "a0", hours_ago=1), _item("A", "a1", hours_ago=2)]
    merged = interleave_by_source([feed_a, []])
    assert [it["link"] for it in merged] == ["a0", "a1"]
    assert interleave_by_source([]) == []
    assert interleave_by_source([[], []]) == []


def test_interleave_keeps_items_without_date_last():
    """Feed sem data nao pode furar a fila dos itens datados recentes."""
    feed = [_item("A", "sem-data"), _item("A", "recente", hours_ago=1)]
    merged = interleave_by_source([feed])
    assert [it["link"] for it in merged] == ["recente", "sem-data"]


def test_summarize_anilist_entries_empty():
    assert _summarize_anilist_entries([]) == ""


def test_summarize_anilist_entries_picks_top_genres():
    entries = [
        {"score": 9, "media": {"title": {"romaji": "Show A"}, "genres": ["Action", "Fantasy"]}},
        {"score": 8, "media": {"title": {"romaji": "Show B"}, "genres": ["Action", "Comedy"]}},
        {"score": 0, "media": {"title": {"romaji": "Show C"}, "genres": ["Slice of Life"]}},
    ]
    result = _summarize_anilist_entries(entries)
    assert "Action" in result
    assert "generos favoritos" in result


def test_summarize_anilist_entries_only_highlights_high_scores():
    entries = [
        {"score": 9, "media": {"title": {"romaji": "Loved Show"}, "genres": ["Drama"]}},
        {"score": 3, "media": {"title": {"romaji": "Meh Show"}, "genres": ["Drama"]}},
    ]
    result = _summarize_anilist_entries(entries)
    assert "Loved Show" in result
    assert "Meh Show" not in result


def test_summarize_anilist_entries_handles_missing_fields():
    entries = [{"score": 9, "media": {"title": {}, "genres": None}}]
    assert _summarize_anilist_entries(entries) == ""
