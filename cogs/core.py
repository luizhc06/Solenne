import time
import asyncio
import logging
from datetime import datetime, timezone, time as dtime

import httpx
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import ALLOWED_GUILD_ID, NEWS_TIMEZONE
from db import backup_database_sync, get_db_stats
from notify import notify_owner_text

log = logging.getLogger("hermes-bot")

STATUS_MESSAGES = [
    "🛡️ Fazendo automod",
    "🌦️ Pesquisando clima",
    "📰 Investigando noticias",
    "🔎 Pesquisando na web",
    "🧠 Pensando em respostas",
    "👀 De olho no servidor",
    "📚 Resumindo o que rolou",
]
STATUS_ROTATE_MINUTES = 30
BACKUP_TIME = dtime(hour=3, minute=0, tzinfo=NEWS_TIMEZONE)

API_HEALTH_URL = "https://geocoding-api.open-meteo.com/v1/search?name=London"
API_HEALTH_TIMEOUT_SECONDS = 1.5


async def _check_api_health() -> str:
    async with httpx.AsyncClient() as client:
        try:
            start = time.monotonic()
            resp = await client.get(API_HEALTH_URL, timeout=API_HEALTH_TIMEOUT_SECONDS)
            latency_ms = (time.monotonic() - start) * 1000
            if resp.status_code == 200:
                return f"🟢 Online ({latency_ms:.0f}ms)"
            return "🟡 Instavel"
        except Exception:
            return "🔴 Offline"


class CoreCog(commands.Cog):
    """Eventos/tarefas de infraestrutura: trava de servidor, status rotativo e backup."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.start_time = time.time()
        self.rotate_status_task.start()
        self.daily_backup_task.start()

    def cog_unload(self):
        self.rotate_status_task.cancel()
        self.daily_backup_task.cancel()

    @tasks.loop(minutes=STATUS_ROTATE_MINUTES)
    async def rotate_status_task(self):
        text = STATUS_MESSAGES[self.rotate_status_task.current_loop % len(STATUS_MESSAGES)]
        await self.bot.change_presence(activity=discord.CustomActivity(name=text))

    @rotate_status_task.before_loop
    async def before_rotate_status_task(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=BACKUP_TIME)
    async def daily_backup_task(self):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, backup_database_sync)
        except Exception:
            log.exception("Erro ao fazer backup do banco")

    @daily_backup_task.before_loop
    async def before_daily_backup_task(self):
        await self.bot.wait_until_ready()

    async def enforce_guild_lock(self):
        for guild in list(self.bot.guilds):
            if guild.id != ALLOWED_GUILD_ID:
                log.warning("Saindo de servidor nao autorizado: %s (%s)", guild.name, guild.id)
                await guild.leave()

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("Logado como %s (%s)", self.bot.user, datetime.now().isoformat())
        await self.enforce_guild_lock()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if guild.id != ALLOWED_GUILD_ID:
            log.warning("Saindo de servidor nao autorizado: %s (%s)", guild.name, guild.id)

            inviter_text = "Nao consegui identificar quem adicionou (sem permissao de ver o log de auditoria la)."
            try:
                async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=5):
                    if entry.target and entry.target.id == self.bot.user.id:
                        inviter_text = f"**Adicionado por:** {entry.user} (`{entry.user.id}`)"
                        break
            except discord.Forbidden:
                pass
            except Exception:
                log.exception("Erro ao consultar audit log do servidor %s", guild.id)

            await notify_owner_text(
                self.bot,
                f"🚨 **Fui adicionada a um servidor nao autorizado e ja sai dele.**\n"
                f"**Servidor:** {guild.name} (`{guild.id}`)\n"
                f"{inviter_text}",
            )
            await guild.leave()

    @app_commands.command(name="status", description="Mostra uptime, latencia e saude da Solenne")
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        loop = asyncio.get_event_loop()

        uptime_seconds = int(time.time() - self.bot.start_time)
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"{days}d {hours}h {minutes}m"

        db_info = await loop.run_in_executor(None, get_db_stats)
        api_health = await _check_api_health()

        embed = discord.Embed(
            title="📊 Telemetria da Solenne",
            color=discord.Color.dark_grey(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="⏱️ Uptime", value=f"`{uptime_str}`", inline=True)
        embed.add_field(name="⚡ Latencia Discord", value=f"`{self.bot.latency * 1000:.0f}ms`", inline=True)
        embed.add_field(name="🗺️ Open-Meteo API", value=api_health, inline=True)
        embed.add_field(
            name="💾 Banco de dados (SQLite)",
            value=f"• Mensagens salvas: `{db_info['messages']}`\n• Perfis mapeados: `{db_info['profiles']}`",
            inline=False,
        )
        embed.set_footer(text="Solenne - VM.Standard.E2.1.Micro")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CoreCog(bot))
