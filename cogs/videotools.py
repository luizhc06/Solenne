import asyncio
import logging
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from utils import thinking_embed, safe_edit_original

log = logging.getLogger("hermes-bot")

FFMPEG_TIMEOUT_SECONDS = 90
# Anexo maior que isso nem baixa - poupa disco/tempo num arquivo que quase certo
# nao ia caber de volta no Discord de qualquer jeito.
MAX_INPUT_BYTES = 100 * 1024 * 1024
# Fallback pra quando o comando roda fora de um guild (nao deveria acontecer, o
# bot trava em um unico servidor - ver ALLOWED_GUILD_ID - mas guild.filesize_limit
# e a fonte de verdade real quando disponivel).
DEFAULT_FILESIZE_LIMIT = 10 * 1024 * 1024

AUDIO_CODECS = {"mp3": "libmp3lame", "wav": "pcm_s16le", "m4a": "aac"}

# /youtube: o yt-dlp baixa e o ffmpeg converte; o YouTube exige um runtime de JS
# (o deno, que vem pelo pip - ver requirements.txt). Um download por vez: a VM tem 1GB.
YTDLP_TIMEOUT_SECONDS = 180
YOUTUBE_MP3_KBPS = 128
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


class FFmpegError(Exception):
    pass


def build_gif_filter(fps: int, width: int) -> str:
    """Cadeia de filtros do ffmpeg compartilhada pelas duas passadas do GIF -
    funcao pura pra poder testar sem invocar o ffmpeg de verdade."""
    return f"fps={fps},scale={width}:-1:flags=lanczos"


def build_palettegen_cmd(input_path: str, palette_path: str, filter_str: str, duration: float) -> list[str]:
    return [
        "-t", str(duration), "-i", input_path,
        "-vf", f"{filter_str},palettegen=stats_mode=diff",
        palette_path,
    ]


def build_paletteuse_cmd(input_path: str, palette_path: str, output_path: str, filter_str: str, duration: float) -> list[str]:
    return [
        "-t", str(duration), "-i", input_path, "-i", palette_path,
        "-filter_complex", f"{filter_str}[x];[x][1:v]paletteuse=dither=bayer",
        output_path,
    ]


def is_youtube_url(url: str) -> bool:
    """So link do YouTube: o yt-dlp abre centenas de sites, e o bot nao e um baixador geral."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() in YOUTUBE_HOSTS


def max_mp3_seconds(limit_bytes: int, kbps: int = YOUTUBE_MP3_KBPS) -> int:
    """Duracao maxima cujo mp3 ainda cabe no limite do servidor, com 5% de folga."""
    return int(limit_bytes * 8 / (kbps * 1000) * 0.95)


def build_ytdlp_cmd(url: str, out_dir: str, fmt: str, limit_bytes: int) -> list[str]:
    """Video longo demais ou ao vivo e filtrado antes de baixar: o yt-dlp sai sem arquivo."""
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--quiet", "--no-warnings",
        "--restrict-filenames", "--paths", out_dir, "--output", "%(title).80B.%(ext)s",
    ]
    if fmt == "mp3":
        cmd += [
            "--match-filter", f"!is_live & duration <= {max_mp3_seconds(limit_bytes)}",
            "--extract-audio", "--audio-format", "mp3", "--audio-quality", f"{YOUTUBE_MP3_KBPS}K",
        ]
    else:
        # H.264 + AAC em mp4 toca no Discord em qualquer aparelho; AV1 nao.
        cmd += [
            "--match-filter", "!is_live", "--max-filesize", str(limit_bytes),
            "--format-sort", "res:480,vcodec:h264,ext:mp4:m4a", "--merge-output-format", "mp4",
        ]
    return cmd + ["--", url]


def build_audio_extract_cmd(input_path: str, output_path: str, fmt: str) -> list[str]:
    codec = AUDIO_CODECS[fmt]
    cmd = ["-i", input_path, "-vn", "-acodec", codec]
    if fmt == "mp3":
        cmd += ["-b:a", "128k"]
    cmd.append(output_path)
    return cmd


async def _run_ffmpeg(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FFmpegError("o ffmpeg demorou demais e foi cancelado")
    if proc.returncode != 0:
        # Corta o stderr - ffmpeg as vezes despeja bastante coisa, e so o fim
        # costuma ter a linha de erro real.
        raise FFmpegError(stderr.decode(errors="replace")[-800:])


class DownloadError(Exception):
    pass


async def _run_ytdlp(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=YTDLP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise DownloadError("o download demorou demais e foi cancelado")
    if proc.returncode != 0:
        raise DownloadError(stderr.decode(errors="replace")[-800:])


def youtube_error_message(stderr: str) -> str:
    if "Sign in to confirm" in stderr:
        return "O YouTube bloqueou o download a partir do servidor do bot (pediu login). Tenta mais tarde."
    if "Private video" in stderr or "unavailable" in stderr.lower():
        return "Esse video e privado ou nao esta disponivel."
    return "Deu erro baixando esse video. Confere o link e tenta de novo."


def guild_filesize_limit(interaction: discord.Interaction) -> int:
    if interaction.guild is not None:
        return interaction.guild.filesize_limit
    return DEFAULT_FILESIZE_LIMIT


async def _validate_attachment(interaction: discord.Interaction, video: discord.Attachment) -> str | None:
    """Devolve uma mensagem de erro amigavel, ou None se o anexo parece valido."""
    if video.content_type and not video.content_type.startswith("video/"):
        return f"Isso ai parece ser `{video.content_type}`, nao video. Anexa um arquivo de video."
    if video.size > MAX_INPUT_BYTES:
        mb = MAX_INPUT_BYTES // (1024 * 1024)
        return f"Esse video tem mais de {mb}MB, nao vou nem tentar baixar. Manda um menor."
    return None


class VideoToolsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_lock = asyncio.Lock()

    @app_commands.command(name="paragif", description="Converte um video anexado em GIF")
    @app_commands.describe(
        video="O arquivo de video",
        duracao="Segundos do inicio do video a converter (padrao 8)",
        fps="Quadros por segundo do GIF (padrao 12)",
        largura="Largura do GIF em pixels (padrao 480)",
    )
    async def paragif(
        self,
        interaction: discord.Interaction,
        video: discord.Attachment,
        duracao: app_commands.Range[int, 2, 20] = 8,
        fps: app_commands.Range[int, 4, 20] = 12,
        largura: app_commands.Range[int, 120, 640] = 480,
    ):
        erro = await _validate_attachment(interaction, video)
        if erro:
            await interaction.response.send_message(erro, ephemeral=True)
            return

        await interaction.response.send_message(
            embed=thinking_embed("🎞️ Convertendo pra GIF...")
        )

        try:
            with tempfile.TemporaryDirectory() as tmp:
                input_path = str(Path(tmp) / f"input{Path(video.filename).suffix or '.mp4'}")
                palette_path = str(Path(tmp) / "palette.png")
                output_path = str(Path(tmp) / "output.gif")

                await video.save(input_path)

                filter_str = build_gif_filter(fps, largura)
                await _run_ffmpeg(build_palettegen_cmd(input_path, palette_path, filter_str, duracao))
                await _run_ffmpeg(build_paletteuse_cmd(input_path, palette_path, output_path, filter_str, duracao))

                size = Path(output_path).stat().st_size
                limit = guild_filesize_limit(interaction)
                if size > limit:
                    await safe_edit_original(
                        interaction,
                        content=(
                            f"O GIF ficou com {size / 1024 / 1024:.1f}MB, passou do limite deste servidor "
                            f"({limit / 1024 / 1024:.0f}MB). Tenta com `duracao`, `fps` ou `largura` menores."
                        ),
                        embed=None,
                    )
                    return

                # Envio direto (nao safe_edit_original): o fallback do helper pra token
                # expirado so repassa content/embeds/view, nao attachments - usar ele
                # aqui derrubaria o GIF em silencio se o token tivesse expirado.
                await interaction.edit_original_response(
                    content=None, embed=None,
                    attachments=[discord.File(output_path, filename="convertido.gif")],
                )
        except FFmpegError:
            log.exception("Erro do ffmpeg ao converter %s pra GIF", video.filename)
            await safe_edit_original(
                interaction,
                content="Deu erro ao converter esse video pra GIF. Confere se o arquivo nao esta corrompido.",
                embed=None,
            )
        except discord.HTTPException:
            log.exception("Erro ao baixar/enviar anexo em /paragif")
            await safe_edit_original(
                interaction, content="Deu erro baixando ou mandando o arquivo, tenta de novo.", embed=None
            )

    @app_commands.command(name="extrairaudio", description="Extrai o audio de um video anexado")
    @app_commands.describe(video="O arquivo de video", formato="Formato do audio de saida")
    @app_commands.choices(formato=[
        app_commands.Choice(name="mp3", value="mp3"),
        app_commands.Choice(name="wav", value="wav"),
        app_commands.Choice(name="m4a", value="m4a"),
    ])
    async def extrairaudio(
        self,
        interaction: discord.Interaction,
        video: discord.Attachment,
        formato: app_commands.Choice[str] = None,
    ):
        fmt = formato.value if formato else "mp3"

        erro = await _validate_attachment(interaction, video)
        if erro:
            await interaction.response.send_message(erro, ephemeral=True)
            return

        await interaction.response.send_message(
            embed=thinking_embed("🎧 Extraindo o audio...")
        )

        try:
            with tempfile.TemporaryDirectory() as tmp:
                input_path = str(Path(tmp) / f"input{Path(video.filename).suffix or '.mp4'}")
                output_path = str(Path(tmp) / f"output.{fmt}")

                await video.save(input_path)
                await _run_ffmpeg(build_audio_extract_cmd(input_path, output_path, fmt))

                size = Path(output_path).stat().st_size
                limit = guild_filesize_limit(interaction)
                if size > limit:
                    await safe_edit_original(
                        interaction,
                        content=(
                            f"O audio ficou com {size / 1024 / 1024:.1f}MB, passou do limite deste servidor "
                            f"({limit / 1024 / 1024:.0f}MB). Tenta um video mais curto."
                        ),
                        embed=None,
                    )
                    return

                # Envio direto - ver comentario equivalente em /paragif sobre attachments
                # nao passarem pelo fallback do safe_edit_original.
                await interaction.edit_original_response(
                    content=None, embed=None,
                    attachments=[discord.File(output_path, filename=f"audio.{fmt}")],
                )
        except FFmpegError:
            log.exception("Erro do ffmpeg ao extrair audio de %s", video.filename)
            await safe_edit_original(
                interaction,
                content="Deu erro ao extrair o audio desse video. Confere se o arquivo nao esta corrompido.",
                embed=None,
            )
        except discord.HTTPException:
            log.exception("Erro ao baixar/enviar anexo em /extrairaudio")
            await safe_edit_original(
                interaction, content="Deu erro baixando ou mandando o arquivo, tenta de novo.", embed=None
            )


    @app_commands.command(name="youtube", description="Baixa o audio (mp3) ou o video de um link do YouTube")
    @app_commands.describe(link="Link do video no YouTube", formato="mp3 (padrao) ou video")
    @app_commands.choices(formato=[
        app_commands.Choice(name="mp3 (audio)", value="mp3"),
        app_commands.Choice(name="video (mp4)", value="video"),
    ])
    async def youtube(
        self,
        interaction: discord.Interaction,
        link: str,
        formato: app_commands.Choice[str] = None,
    ):
        fmt = formato.value if formato else "mp3"
        if not is_youtube_url(link):
            await interaction.response.send_message("Manda um link do YouTube (youtube.com ou youtu.be).", ephemeral=True)
            return
        if self.youtube_lock.locked():
            await interaction.response.send_message("Ja tem um download rodando, tenta de novo em instantes.", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=thinking_embed("🎧 Baixando o audio..." if fmt == "mp3" else "🎬 Baixando o video...")
        )

        limit = guild_filesize_limit(interaction)
        try:
            async with self.youtube_lock:
                with tempfile.TemporaryDirectory() as tmp:
                    await _run_ytdlp(build_ytdlp_cmd(link, tmp, fmt, limit))
                    arquivos = [p for p in Path(tmp).iterdir() if p.is_file()]
                    if not arquivos:
                        if fmt == "mp3":
                            minutos = max_mp3_seconds(limit) // 60
                            motivo = f"tem mais de {minutos} minutos (nao caberia no limite deste servidor) ou e ao vivo"
                        else:
                            motivo = "passou do limite deste servidor ou e ao vivo. Tenta em mp3"
                        await safe_edit_original(interaction, content=f"Nao baixei: esse video {motivo}.", embed=None)
                        return

                    arquivo = arquivos[0]
                    size = arquivo.stat().st_size
                    if size > limit:
                        await safe_edit_original(
                            interaction,
                            content=(
                                f"Ficou com {size / 1024 / 1024:.1f}MB, passou do limite deste servidor "
                                f"({limit / 1024 / 1024:.0f}MB). Tenta em mp3 ou um video mais curto."
                            ),
                            embed=None,
                        )
                        return

                    # Envio direto - ver comentario equivalente em /paragif sobre attachments
                    # nao passarem pelo fallback do safe_edit_original.
                    await interaction.edit_original_response(
                        content=None, embed=None, attachments=[discord.File(str(arquivo), filename=arquivo.name)],
                    )
        except DownloadError as exc:
            log.warning("Erro do yt-dlp em /youtube (%s): %s", link, exc)
            await safe_edit_original(interaction, content=youtube_error_message(str(exc)), embed=None)
        except discord.HTTPException:
            log.exception("Erro ao enviar arquivo em /youtube")
            await safe_edit_original(interaction, content="Deu erro mandando o arquivo, tenta de novo.", embed=None)


async def setup(bot: commands.Bot):
    await bot.add_cog(VideoToolsCog(bot))
