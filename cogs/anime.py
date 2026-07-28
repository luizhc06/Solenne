import asyncio
import logging
from datetime import datetime, timezone

import httpx
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import ALLOWED_GUILD_ID, ANILIST_USERNAME
from db import filter_unannounced_episodes, mark_episodes_announced, has_announced_any_episode

log = logging.getLogger("hermes-bot")

ANILIST_API_URL = "https://graphql.anilist.co"
ANILIST_TIMEOUT_SECONDS = 15

# Onde anunciar episodio novo: primeiro canal cujo nome contenha um destes, na ordem.
ANIME_CHANNEL_NAMES = ("anime", "noticias", "geral")
AIRING_CHECK_MINUTES = 30

# Puxa so o que o dono marcou como "assistindo" - lista de planejados tem centenas
# de titulos e nenhum deles interessa como alerta de episodio novo.
AIRING_QUERY = """
query ($name: String) {
  MediaListCollection(userName: $name, type: ANIME, status: CURRENT) {
    lists {
      entries {
        progress
        media {
          id
          title { romaji english }
          siteUrl
          status
          episodes
          coverImage { medium }
          nextAiringEpisode { episode airingAt }
        }
      }
    }
  }
}
"""


def _fetch_watching_sync() -> list[dict]:
    try:
        resp = httpx.post(
            ANILIST_API_URL,
            json={"query": AIRING_QUERY, "variables": {"name": ANILIST_USERNAME}},
            timeout=ANILIST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception("Erro ao buscar lista de acompanhamento no AniList")
        return []

    if data.get("errors"):
        log.warning("AniList devolveu erro para o usuario %s: %s", ANILIST_USERNAME, data["errors"])
        return []

    entries = []
    for lst in (data.get("data", {}).get("MediaListCollection") or {}).get("lists") or []:
        entries.extend(lst.get("entries") or [])
    return entries


def media_title(media: dict) -> str:
    title = media.get("title") or {}
    return title.get("english") or title.get("romaji") or "Sem titulo"


def latest_aired_episode(media: dict) -> int | None:
    """Numero do ultimo episodio que ja foi ao ar, ou None se nao da pra saber.

    O AniList so expoe o PROXIMO episodio agendado, entao o ultimo lancado e o
    anterior a ele. Series que ja terminaram nao tem nextAiringEpisode - nesse caso
    nao ha episodio novo pra anunciar, e devolvemos None de proposito.
    """
    next_ep = media.get("nextAiringEpisode")
    if not next_ep:
        return None
    episode = next_ep.get("episode")
    if not episode or episode <= 1:
        return None
    return episode - 1


def build_episode_embed(media: dict, episode: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"{media_title(media)} — episodio {episode}",
        url=media.get("siteUrl"),
        description="Saiu episodio novo de uma serie que voce esta acompanhando.",
        color=discord.Color.fuchsia(),
    )
    cover = (media.get("coverImage") or {}).get("medium")
    if cover:
        embed.set_thumbnail(url=cover)
    total = media.get("episodes")
    if total:
        embed.add_field(name="Progresso da serie", value=f"{episode}/{total}", inline=True)
    embed.set_footer(text="Fonte: AniList")
    return embed


def find_anime_channel(guild: discord.Guild) -> discord.TextChannel | None:
    for target in ANIME_CHANNEL_NAMES:
        for channel in guild.text_channels:
            if target in channel.name.lower():
                return channel
    return None


class AnimeCog(commands.Cog):
    """Radar de episodios novos, baseado na lista 'assistindo' do AniList do dono."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.airing_radar_task.start()

    def cog_unload(self):
        self.airing_radar_task.cancel()

    @tasks.loop(minutes=AIRING_CHECK_MINUTES)
    async def airing_radar_task(self):
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(None, _fetch_watching_sync)
        if not entries:
            return

        candidates = {}
        for entry in entries:
            media = entry.get("media") or {}
            episode = latest_aired_episode(media)
            if episode is not None:
                candidates[(media["id"], episode)] = media

        pending = await loop.run_in_executor(
            None, filter_unannounced_episodes, list(candidates.keys())
        )
        if not pending:
            return

        # Primeira execucao: so registra o estado atual, sem anunciar. Senao o bot
        # despejaria o ultimo episodio de tudo que o dono acompanha de uma vez, como
        # se tivesse acabado de sair - inclusive de series paradas ha meses.
        ja_rodou = await loop.run_in_executor(None, has_announced_any_episode)
        if not ja_rodou:
            log.info("Radar de anime inicializando: registrando %s episodios sem anunciar.", len(pending))
            await loop.run_in_executor(None, mark_episodes_announced, pending)
            return

        guild = self.bot.get_guild(ALLOWED_GUILD_ID)
        if guild is None:
            return
        channel = find_anime_channel(guild)
        if channel is None:
            log.warning("Nenhum canal encontrado pro radar de anime (procurei por %s).", ANIME_CHANNEL_NAMES)
            return

        announced = []
        for key in pending:
            media = candidates[key]
            try:
                await channel.send(embed=build_episode_embed(media, key[1]))
                announced.append(key)
            except discord.HTTPException:
                log.exception("Erro ao anunciar episodio %s de %s", key[1], media_title(media))
        await loop.run_in_executor(None, mark_episodes_announced, announced)

    @airing_radar_task.before_loop
    async def before_airing_radar_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="anime", description="Proximos episodios das series que o Rizu acompanha")
    async def anime(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        loop = asyncio.get_event_loop()
        entries = await loop.run_in_executor(None, _fetch_watching_sync)

        if not entries:
            await interaction.followup.send(
                "Nao consegui ler a lista do AniList agora (ou ela esta vazia/privada)."
            )
            return

        agendados = []
        for entry in entries:
            media = entry.get("media") or {}
            next_ep = media.get("nextAiringEpisode")
            if next_ep and next_ep.get("airingAt"):
                agendados.append((next_ep["airingAt"], media, next_ep["episode"], entry.get("progress") or 0))
        agendados.sort(key=lambda x: x[0])

        embed = discord.Embed(
            title="📺 Radar de anime",
            color=discord.Color.fuchsia(),
            timestamp=datetime.now(timezone.utc),
        )
        if not agendados:
            embed.description = (
                "Nenhuma das series que voce esta acompanhando tem episodio agendado — "
                "provavelmente todas ja terminaram a temporada."
            )
        else:
            # <t:unix:R> deixa o Discord renderizar "em 2 dias" no fuso de quem le.
            embed.description = "\n".join(
                f"**{media_title(media)}** — ep. {episode} <t:{airing_at}:R>"
                + (f"\n-# voce parou no ep. {progress}" if progress else "")
                for airing_at, media, episode, progress in agendados[:15]
            )[:4000]
        embed.set_footer(text=f"AniList: {ANILIST_USERNAME}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AnimeCog(bot))
