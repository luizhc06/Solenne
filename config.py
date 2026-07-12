import os
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import discord

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hermes-bot")


@dataclass
class AppConfig:
    nvidia_api_key: str
    discord_token: str
    allowed_guild_id: int
    owner_user_id: int
    model: str = "openai/gpt-oss-120b"
    db_path: str = "/app/data/solenne.db"

    @classmethod
    def load(cls) -> "AppConfig":
        return cls(
            nvidia_api_key=os.environ["NVIDIA_API_KEY"],
            discord_token=os.environ["DISCORD_TOKEN"],
            allowed_guild_id=int(os.environ["ALLOWED_GUILD_ID"]),
            owner_user_id=int(os.environ["OWNER_USER_ID"]),
            model=os.environ.get("HERMES_MODEL", "openai/gpt-oss-120b"),
            db_path=os.environ.get("DB_PATH", "/app/data/solenne.db"),
        )

try:
    _cfg = AppConfig.load()
except KeyError as e:
    raise RuntimeError(f"Variavel de ambiente obrigatoria ausente: {e}")

NVIDIA_API_KEY = _cfg.nvidia_api_key
DISCORD_TOKEN = _cfg.discord_token
MODEL = _cfg.model
ALLOWED_GUILD_ID = _cfg.allowed_guild_id
OWNER_USER_ID = _cfg.owner_user_id
DB_PATH = _cfg.db_path
HISTORY_WINDOW = 20

NEWS_TIMEZONE = ZoneInfo("America/Sao_Paulo")
DIAS_SEMANA = ["Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo"]

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
