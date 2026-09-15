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
    model: str = "nvidia/nemotron-3-super-120b-a12b"
    db_path: str = "/app/data/solenne.db"
    refinement_rounds: int = 0
    reasoning_budget: int = 768
    humanize_pass: bool = False
    anilist_username: str = "Rizuw"
    ai_concurrency_limit: int = 4
    tavily_api_key: str = ""
    welcome_channel_id: int | None = None
    site_repo: str = ""
    site_repo_token: str = ""
    site_arquivo: str = "src/data/noticias.json"

    @classmethod
    def load(cls) -> "AppConfig":
        return cls(
            nvidia_api_key=os.environ["NVIDIA_API_KEY"],
            discord_token=os.environ["DISCORD_TOKEN"],
            allowed_guild_id=int(os.environ["ALLOWED_GUILD_ID"]),
            owner_user_id=int(os.environ["OWNER_USER_ID"]),
            # Trocado de openai/gpt-oss-120b (jul/2026): o Nemotron 3 Super tem 12B
            # parametros ativos contra ~5B do gpt-oss, mas 2.2x a vazao dele e supera
            # ele nos benchmarks da classe - e sendo modelo da propria NVIDIA, tende a
            # ser melhor servido no NIM. Motivo da troca foi latencia: o chat faz
            # REFINEMENT_ROUNDS + 2 chamadas sequenciais por resposta.
            model=os.environ.get("HERMES_MODEL", "nvidia/nemotron-3-super-120b-a12b"),
            db_path=os.environ.get("DB_PATH", "/app/data/solenne.db"),
            # Padrao 0 desde ago/2026: o refino manual (rascunho -> critica -> reescrita)
            # existia pra compensar um modelo que respondia de primeira, sem pensar. O
            # Nemotron 3 Super faz isso nativamente no proprio raciocinio, entao as
            # rodadas extras viraram so latencia - ver AI_REASONING_BUDGET.
            refinement_rounds=int(os.environ.get("REFINEMENT_ROUNDS", "0")),
            reasoning_budget=int(os.environ.get("AI_REASONING_BUDGET", "768")),
            humanize_pass=os.environ.get("HUMANIZE_PASS", "0") == "1",
            anilist_username=os.environ.get("ANILIST_USERNAME", "Rizuw"),
            # Achado do conselho de agentes (18/08/2026): o PriorityGate era um
            # asyncio.Lock unico (1 chamada de IA por vez, sempre) - com 20 pessoas
            # conversando ao mesmo tempo, a ultima esperava ate ~15min so pra sua vez
            # comecar (perto do limite de 15min do token de interacao do Discord). O
            # teto real de qualquer redesenho e a cota da API da NVIDIA (~40 req/min por
            # conta no tier gratuito, nao publicado oficialmente); 4 concorrentes com
            # turnos de ~30-45s cada fica bem abaixo disso com folga pra retry. Ajustar
            # aqui depois de observar 429/RateLimitError reais em producao.
            ai_concurrency_limit=int(os.environ.get("AI_CONCURRENCY_LIMIT", "4")),
            # Feature flag da troca de fonte de busca (18/08/2026, ver cogs/search.py):
            # vazio = continua 100% no scraping do DuckDuckGo, como sempre foi.
            tavily_api_key=os.environ.get("TAVILY_API_KEY", ""),
            # Canal fixo de boas-vindas (cogs/welcome.py). Vazio/ausente = cai pro
            # system_channel do proprio Discord em tempo de execucao, entao nao ha
            # default aqui - so None mesmo.
            welcome_channel_id=(
                int(os.environ["WELCOME_CHANNEL_ID"]) if os.environ.get("WELCOME_CHANNEL_ID") else None
            ),
            # Ponte com o site (site_noticias.py): o resumo diario tambem vira um
            # arquivo no repositorio do site, que recompila sozinho. Token vazio =
            # recurso desligado, o resumo continua indo so pro Discord.
            site_repo=os.environ.get("SITE_REPO", ""),
            site_repo_token=os.environ.get("SITE_REPO_TOKEN", ""),
            site_arquivo=os.environ.get("SITE_ARQUIVO", "src/data/noticias.json"),
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
REFINEMENT_ROUNDS = _cfg.refinement_rounds
REASONING_BUDGET = _cfg.reasoning_budget
HUMANIZE_PASS = _cfg.humanize_pass
ANILIST_USERNAME = _cfg.anilist_username
AI_CONCURRENCY_LIMIT = _cfg.ai_concurrency_limit
TAVILY_API_KEY = _cfg.tavily_api_key
WELCOME_CHANNEL_ID = _cfg.welcome_channel_id
SITE_REPO = _cfg.site_repo
SITE_REPO_TOKEN = _cfg.site_repo_token
SITE_ARQUIVO = _cfg.site_arquivo
HISTORY_WINDOW = 20

# Recomendacao oficial da NVIDIA pro Nemotron 3 Super: temperature 1.0 e top_p 0.95
# "across all tasks and serving backends - reasoning, tool calling, and general chat
# alike". O codigo antigo espalhava temperaturas de 0.3 a 0.9 por call site (herdadas
# da epoca do gpt-oss), o que empurrava o modelo pra fora da faixa em que ele foi
# calibrado e ajudava a fazer as respostas soarem rasas.
DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOP_P = 0.95

NEWS_TIMEZONE = ZoneInfo("America/Sao_Paulo")
DIAS_SEMANA = ["Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo"]

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
