import discord

from user_profile import schedule_profile_update


class FeedbackView(discord.ui.View):
    """Botoes de like/dislike que alimentam o resumo de perfil da pessoa que clicou.

    `topic` e a PERGUNTA que a pessoa fez pra Solenne (`pergunta[:200]` em cogs/chat.py),
    nao um assunto que apareceu pra ela por acaso. Ate 20/09/2026 as notas diziam "Gostou/
    Nao gostou de conteudo sobre: {topic}" - lido pelo PROFILE_UPDATE_PROMPT como "essa
    pessoa gosta/nao gosta desse ASSUNTO", quando na verdade o 👎 so diz que a RESPOSTA
    dela aquela pergunta especifica foi ruim (achado na verificacao geral de 20/09/2026:
    o proprio dono clicou 👎 numa resposta e o perfil dele passou a registrar que ele
    "nao gostou" do assunto que ELE MESMO tinha perguntado). As notas agora deixam
    explicito que o feedback e sobre a RESPOSTA, nao sobre o assunto.
    """

    def __init__(self, topic: str):
        super().__init__(timeout=3600)
        self.topic = topic[:200]

    @discord.ui.button(label="👍", style=discord.ButtonStyle.success)
    async def like(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = f'Deu 👍 na resposta dela pra pergunta "{self.topic}" (feedback sobre a resposta, nao sobre o assunto).'
        schedule_profile_update(interaction.user.id, interaction.user.display_name, note)
        await interaction.response.send_message("Anotado, valeu pelo feedback! 👍", ephemeral=True)

    @discord.ui.button(label="👎", style=discord.ButtonStyle.danger)
    async def dislike(self, interaction: discord.Interaction, button: discord.ui.Button):
        note = (
            f'Deu 👎 na resposta dela pra pergunta "{self.topic}" - achou a resposta ruim, '
            "incompleta ou irrelevante. Isso NAO significa que a pessoa nao goste do assunto "
            "perguntado (ela pode ter perguntado exatamente porque tem interesse nele)."
        )
        schedule_profile_update(interaction.user.id, interaction.user.display_name, note)
        await interaction.response.send_message("Anotado, vou ajustar. 👎", ephemeral=True)
