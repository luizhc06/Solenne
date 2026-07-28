from cogs.anime import latest_aired_episode, media_title


def test_latest_aired_is_one_before_next_scheduled():
    media = {"nextAiringEpisode": {"episode": 5, "airingAt": 1785425400}}
    assert latest_aired_episode(media) == 4


def test_no_next_episode_means_nothing_to_announce():
    """Serie ja terminada nao tem nextAiringEpisode - nao ha episodio novo."""
    assert latest_aired_episode({"nextAiringEpisode": None}) is None
    assert latest_aired_episode({}) is None


def test_first_episode_not_yet_aired_is_ignored():
    """Se o proximo agendado e o ep. 1, nada foi ao ar ainda."""
    assert latest_aired_episode({"nextAiringEpisode": {"episode": 1}}) is None


def test_media_title_prefers_english_then_romaji():
    assert media_title({"title": {"english": "The Demon Girl", "romaji": "Machikado Mazoku"}}) == "The Demon Girl"
    assert media_title({"title": {"english": None, "romaji": "Machikado Mazoku"}}) == "Machikado Mazoku"
    assert media_title({"title": {}}) == "Sem titulo"
