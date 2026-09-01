from cogs.videotools import (
    build_gif_filter,
    build_palettegen_cmd,
    build_paletteuse_cmd,
    build_audio_extract_cmd,
    AUDIO_CODECS,
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
