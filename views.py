import asyncio

import discord

from user_profile import update_profile


class FeedbackView(discord.ui.View):
    """Botoes de like/dislike que alimentam o resumo de perfil da pessoa que clicou."""

    def __init__(self, topic: str):
        super().__init__(timeout=3600)
        self.topic = topic[:200]

    @discord.ui.button(label="👍", style=discord.ButtonStyle.success)
    async def like(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = f"Gostou de conteudo sobre: {self.topic}"
        asyncio.create_task(update_profile(interaction.user.id, interaction.user.display_name, note))
        await interaction.response.send_message("Anotado, valeu pelo feedback! 👍", ephemeral=True)

    @discord.ui.button(label="👎", style=discord.ButtonStyle.danger)
    async def dislike(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = f"Nao gostou / achou irrelevante conteudo sobre: {self.topic}"
        asyncio.create_task(update_profile(interaction.user.id, interaction.user.display_name, note))
        await interaction.response.send_message("Anotado, vou ajustar. 👎", ephemeral=True)
