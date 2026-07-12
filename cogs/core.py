import asyncio
import logging
from datetime import datetime, time as dtime

import discord
from discord.ext import commands, tasks

from config import ALLOWED_GUILD_ID, NEWS_TIMEZONE
from db import backup_database_sync
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


class CoreCog(commands.Cog):
    """Eventos/tarefas de infraestrutura: trava de servidor, status rotativo e backup."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
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


async def setup(bot: commands.Bot):
    await bot.add_cog(CoreCog(bot))
