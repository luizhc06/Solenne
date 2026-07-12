import discord
from discord import app_commands
from discord.ext import commands

from config import DISCORD_TOKEN, ALLOWED_GUILD_ID, INTENTS, log
from db import init_db

EXTENSIONS = [
    "cogs.core",
    "cogs.moderation",
    "cogs.search",
    "cogs.chat",
    "cogs.weather",
    "cogs.news",
    "cogs.admin",
]


class HermesBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS, help_command=None)

    async def setup_hook(self):
        init_db()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        guild = discord.Object(id=ALLOWED_GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        # Limpa comandos globais antigos (registrados globalmente antes do sync
        # passar a ser por servidor) para nao aparecerem duplicados no Discord.
        self.tree.clear_commands(guild=None)
        await self.tree.sync(guild=None)


bot = HermesBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
    log.exception("Erro em comando", exc_info=error)
    if not interaction.response.is_done():
        await interaction.response.send_message("Deu erro ao executar o comando.", ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
