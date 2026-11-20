from cogs.videotools import (
    build_gif_filter,
    build_palettegen_cmd,
    build_paletteuse_cmd,
    build_audio_extract_cmd,
    AUDIO_CODECS,
    build_ytdlp_cmd,
    is_youtube_url,
    max_mp3_seconds,
    youtube_error_message,
)


def test_build_gif_filter_format():
    assert build_gif_filter(fps=12, width=480) == "fps=12,scale=480:-1:flags=lanczos"


def test_build_gif_filter_varies_with_params():
    assert build_gif_filter(fps=24, width=240) == "fps=24,scale=240:-1:flags=lanczos"


def test_palettegen_cmd_trims_duration_and_uses_shared_filter():
    filt = build_gif_filter(10, 480)
    cmd = build_palettegen_cmd("in.mp4", "palette.png", filt, duration=8)
    assert cmd[:4] == ["-t", "8", "-i", "in.mp4"]
    assert cmd[-1] == "palette.png"
    assert "palettegen" in cmd[5]
    assert filt in cmd[5]


def test_paletteuse_cmd_references_both_inputs_and_palette_filter():
    filt = build_gif_filter(10, 480)
    cmd = build_paletteuse_cmd("in.mp4", "palette.png", "out.gif", filt, duration=8)
    assert "in.mp4" in cmd
    assert "palette.png" in cmd
    assert cmd[-1] == "out.gif"
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert filt in filter_complex
    assert "paletteuse" in filter_complex


def test_audio_extract_cmd_strips_video_stream():
    cmd = build_audio_extract_cmd("in.mp4", "out.mp3", "mp3")
    assert "-vn" in cmd
    assert cmd[cmd.index("-acodec") + 1] == AUDIO_CODECS["mp3"]
    assert cmd[-1] == "out.mp3"


def test_audio_extract_cmd_sets_bitrate_only_for_mp3():
    mp3_cmd = build_audio_extract_cmd("in.mp4", "out.mp3", "mp3")
    wav_cmd = build_audio_extract_cmd("in.mp4", "out.wav", "wav")
    assert "-b:a" in mp3_cmd
    assert "-b:a" not in wav_cmd


def test_audio_extract_cmd_picks_correct_codec_per_format():
    assert build_audio_extract_cmd("in.mp4", "out.wav", "wav")[
        build_audio_extract_cmd("in.mp4", "out.wav", "wav").index("-acodec") + 1
    ] == "pcm_s16le"
    assert build_audio_extract_cmd("in.mp4", "out.m4a", "m4a")[
        build_audio_extract_cmd("in.mp4", "out.m4a", "m4a").index("-acodec") + 1
    ] == "aac"


TEN_MB = 10 * 1024 * 1024


def test_is_youtube_url_aceita_os_enderecos_do_youtube():
    for url in [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw",
        "http://m.youtube.com/watch?v=x",
        "https://music.youtube.com/watch?v=x",
        "  https://YouTube.com/shorts/x  ",
    ]:
        assert is_youtube_url(url), url


def test_is_youtube_url_recusa_outros_sites_e_truques():
    for url in [
        "https://youtube.com.evil.com/watch?v=x",
        "https://evil.com/?u=https://youtube.com",
        "ftp://youtube.com/x",
        "youtube.com/watch?v=x",
        "--exec rm",
        "https://vimeo.com/1",
    ]:
        assert not is_youtube_url(url), url


def test_max_mp3_seconds_cabe_no_limite():
    segundos = max_mp3_seconds(TEN_MB)
    assert segundos * 128_000 / 8 <= TEN_MB
    assert 600 <= segundos <= 660


def test_ytdlp_cmd_mp3_filtra_duracao_e_converte():
    cmd = build_ytdlp_cmd("https://youtu.be/x", "/tmp/a", "mp3", TEN_MB)
    assert cmd[-2:] == ["--", "https://youtu.be/x"]
    assert "--no-playlist" in cmd
    assert cmd[cmd.index("--audio-format") + 1] == "mp3"
    assert f"duration <= {max_mp3_seconds(TEN_MB)}" in cmd[cmd.index("--match-filter") + 1]
    assert "--max-filesize" not in cmd


def test_ytdlp_cmd_video_limita_tamanho_e_prefere_h264():
    cmd = build_ytdlp_cmd("https://youtu.be/x", "/tmp/a", "video", TEN_MB)
    assert cmd[-2:] == ["--", "https://youtu.be/x"]
    assert cmd[cmd.index("--max-filesize") + 1] == str(TEN_MB)
    assert "vcodec:h264" in cmd[cmd.index("--format-sort") + 1]
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert "--extract-audio" not in cmd


def test_youtube_error_message_reconhece_bloqueio_e_privado():
    assert "bloqueou" in youtube_error_message("ERROR: Sign in to confirm you're not a bot")
    assert "privado" in youtube_error_message("ERROR: Private video")
    assert "privado" in youtube_error_message("ERROR: [youtube] x: This video is unavailable")
    assert "Confere o link" in youtube_error_message("algo inesperado")
