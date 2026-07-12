import os

# config.py valida as variaveis de ambiente obrigatorias assim que e importado
# (AppConfig.load() roda no escopo do modulo) - isso precisa rodar ANTES de
# qualquer teste importar config/cogs, senao a coleta de testes quebra.
os.environ.setdefault("NVIDIA_API_KEY", "test-key")
os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_GUILD_ID", "123456789")
os.environ.setdefault("OWNER_USER_ID", "987654321")
