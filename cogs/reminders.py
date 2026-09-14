import re
import json
import asyncio
import logging
import unicodedata
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import NEWS_TIMEZONE
import tools
from db import add_reminder, pop_due_reminders, list_reminders, delete_reminder

log = logging.getLogger("hermes-bot")

REMINDER_CHECK_SECONDS = 30
REMINDER_MAX_HORIZON_DAYS = 365
REMINDER_MAX_PER_USER = 25

# "2h30", "45min", "3 dias", "1h 30m" - soma todas as unidades que aparecerem.
# O fim da unidade e `(?![a-z])`, nao `\b`: em "1h30m" nao ha fronteira de palavra entre
# "h" e "3" (digito e letra sao ambos \w), entao com \b a primeira unidade nao casava e
# "1h30m" virava so "30m". O que precisamos garantir e que a unidade nao seja o comeco de
# uma palavra maior ("s" de "coisas"), e pra isso basta recusar letra na sequencia.
DURATION_UNIT_RE = re.compile(
    r"(\d+)\s*(dias|dia|d|horas|hora|h|minutos|minuto|mins|min|m|segundos|segundo|seg|s)(?![a-z])",
    re.IGNORECASE,
)
UNIT_SECONDS = {
    "d": 86400, "dia": 86400, "dias": 86400,
    "h": 3600, "hora": 3600, "horas": 3600,
    "m": 60, "min": 60, "mins": 60, "minuto": 60, "minutos": 60,
    "s": 1, "seg": 1, "segundo": 1, "segundos": 1,
}

# "amanha as 9h", "hoje 18:30"
DAY_WORD_RE = re.compile(r"^(hoje|amanha|depois de amanha)\b\s*(?:as\s*)?(.*)$", re.IGNORECASE)
# "25/12 10:00", "25/12/2026 as 10h"
DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*(?:as\s*)?(.*)$")
# "9h", "09:30", "18h45"
CLOCK_RE = re.compile(r"^(\d{1,2})(?:[:h](\d{2}))?h?$", re.IGNORECASE)


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _parse_clock(text: str) -> tuple[int, int] | None:
    match = CLOCK_RE.match(text.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_when(text: str, now: datetime) -> datetime | None:
    """Converte a expressao de tempo do usuario num datetime absoluto, ou None.

    Aceita duracao relativa ("30m", "2h30", "3 dias"), dia nomeado ("amanha as 9h"),
    data ("25/12 10:00") e horario ("18:30", "as 9h").

    Desambiguacao de "9h": sozinho e DURACAO (daqui a 9 horas), que e a leitura mais
    comum num lembrete. Pra dizer "as 9 da manha" use "as 9h" ou "09:00" - o "as" e a
    forma explicita de pedir horario do relogio. "18:30" com dois-pontos ja e horario
    sem ambiguidade, porque nao existe unidade de duracao com ":".

    Funcao pura pra poder ser testada sem relogio nem Discord.
    """
    cleaned = _strip_accents(text).strip().lower()
    if not cleaned:
        return None
    cleaned = re.sub(r"^(em|daqui a|daqui)\s+", "", cleaned)

    # "as 9h" / "as 18:30": a pessoa foi explicita que e horario, nao duracao.
    clock_only = re.match(r"^as\s+(.*)$", cleaned)
    if clock_only:
        clock = _parse_clock(clock_only.group(1))
        if clock is None:
            return None
        target = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    # Duracao relativa: so aceita se a string inteira for composta de pares
    # numero+unidade, senao "5 coisas pra fazer" viraria "em 5 segundos".
    if DURATION_UNIT_RE.fullmatch(cleaned) or re.fullmatch(
        r"(\s*\d+\s*[a-z]+\s*)+", cleaned
    ):
        matches = DURATION_UNIT_RE.findall(cleaned)
        consumed = "".join(f"{n}{u}" for n, u in matches)
        if matches and consumed == re.sub(r"\s+", "", cleaned):
            total = sum(int(n) * UNIT_SECONDS[u.lower()] for n, u in matches)
            if total <= 0:
                return None
            return now + timedelta(seconds=total)

    day_match = DAY_WORD_RE.match(cleaned)
    if day_match:
        word, rest = day_match.group(1), day_match.group(2).strip()
        offset = {"hoje": 0, "amanha": 1, "depois de amanha": 2}[word]
        clock = _parse_clock(rest) if rest else (9, 0)
        if clock is None:
            return None
        target = (now + timedelta(days=offset)).replace(
            hour=clock[0], minute=clock[1], second=0, microsecond=0
        )
        return target

    date_match = DATE_RE.match(cleaned)
    if date_match:
        day, month, year, rest = date_match.groups()
        clock = _parse_clock(rest) if rest.strip() else (9, 0)
        if clock is None:
            return None
        year_int = now.year if not year else int(year)
        if year and year_int < 100:
            year_int += 2000
        try:
            target = now.replace(
                year=year_int, month=int(month), day=int(day),
                hour=clock[0], minute=clock[1], second=0, microsecond=0,
            )
        except ValueError:
            return None
        # Data sem ano que ja passou = a pessoa quer o ano que vem.
        if not year and target < now:
            try:
                target = target.replace(year=year_int + 1)
            except ValueError:
                return None
        return target

    clock = _parse_clock(cleaned)
    if clock:
        target = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    return None


def format_due(due_at: datetime) -> str:
    local = due_at.astimezone(NEWS_TIMEZONE)
    return local.strftime("%d/%m/%Y as %H:%M")


class RemindersCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_reminders_task.start()

    def cog_unload(self):
        self.check_reminders_task.cancel()

    @tasks.loop(seconds=REMINDER_CHECK_SECONDS)
    async def check_reminders_task(self):
        loop = asyncio.get_event_loop()
        try:
            due = await loop.run_in_executor(None, pop_due_reminders)
        except Exception:
            log.exception("Erro ao buscar lembretes vencidos")
            return

        for reminder in due:
            channel = self.bot.get_channel(reminder["channel_id"])
            if channel is None:
                log.warning("Canal %s do lembrete %s sumiu.", reminder["channel_id"], reminder["id"])
                continue
            try:
                await channel.send(f"⏰ <@{reminder['user_id']}>, voce pediu pra lembrar: **{reminder['text']}**")
            except discord.HTTPException:
                log.exception("Erro ao entregar lembrete %s", reminder["id"])

    @check_reminders_task.before_loop
    async def before_check_reminders_task(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="lembrete", description="Marca um lembrete pra depois")
    @app_commands.describe(
        quando="Ex: 30m, 2h, amanha as 9h, 25/12 10:00, 18:30 (use 'as 9h' pra horario)",
        oque="O que voce quer que eu lembre",
    )
    async def lembrete(self, interaction: discord.Interaction, quando: str, oque: str):
        now = datetime.now(NEWS_TIMEZONE)
        due_at = parse_when(quando, now)
        if due_at is None:
            await interaction.response.send_message(
                f"Nao entendi \"{quando}\". Tenta assim: `30m`, `2h`, `amanha as 9h`, "
                "`25/12 10:00` ou `18:30`.\n"
                "-# `9h` sozinho eu leio como *daqui a 9 horas* — pra marcar as 9 da manha, use `as 9h`.",
                ephemeral=True,
            )
            return
        if due_at <= now:
            await interaction.response.send_message(
                "Esse horario ja passou. Me da um momento no futuro.", ephemeral=True
            )
            return
        if due_at > now + timedelta(days=REMINDER_MAX_HORIZON_DAYS):
            await interaction.response.send_message(
                f"Isso e daqui a mais de {REMINDER_MAX_HORIZON_DAYS} dias - longe demais pra eu garantir.",
                ephemeral=True,
            )
            return

        loop = asyncio.get_event_loop()
        existing = await loop.run_in_executor(None, list_reminders, interaction.user.id)
        if len(existing) >= REMINDER_MAX_PER_USER:
            await interaction.response.send_message(
                f"Voce ja tem {len(existing)} lembretes pendentes, esse e o limite. "
                "Cancela algum com `/lembretes` antes.",
                ephemeral=True,
            )
            return

        reminder_id = await loop.run_in_executor(
            None, add_reminder, interaction.user.id, interaction.channel_id, oque[:500], due_at
        )
        await interaction.response.send_message(
            f"⏰ Anotado (`#{reminder_id}`): **{oque[:200]}** — te aviso em {format_due(due_at)}."
        )

    @app_commands.command(name="lembretes", description="Lista seus lembretes pendentes")
    async def lembretes(self, interaction: discord.Interaction):
        loop = asyncio.get_event_loop()
        pending = await loop.run_in_executor(None, list_reminders, interaction.user.id)
        if not pending:
            await interaction.response.send_message("Voce nao tem nenhum lembrete pendente.", ephemeral=True)
            return
        linhas = "\n".join(
            f"`#{r['id']}` — **{r['text'][:100]}** ({format_due(r['due_at'])})" for r in pending
        )
        embed = discord.Embed(
            title="⏰ Seus lembretes",
            description=linhas[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Pra cancelar: /cancelarlembrete <id>")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="cancelarlembrete", description="Cancela um lembrete pendente seu")
    @app_commands.describe(id="O numero do lembrete (veja em /lembretes)")
    async def cancelarlembrete(self, interaction: discord.Interaction, id: int):
        loop = asyncio.get_event_loop()
        removed = await loop.run_in_executor(None, delete_reminder, id, interaction.user.id)
        if removed:
            await interaction.response.send_message(f"Lembrete `#{id}` cancelado.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Nao achei um lembrete pendente `#{id}` seu.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(RemindersCog(bot))


@tools.register(
    name="criar_lembrete",
    description=(
        "Marca um lembrete pra pessoa que esta falando com voce agora. Use quando ela "
        "pedir pra ser lembrada de algo ('me lembra de X amanha', 'me avisa em 30 min'). "
        "Voce NAO precisa mandar ninguem usar /lembrete - voce mesma cria."
    ),
    parameters={
        "type": "object",
        "properties": {
            "quando": {
                "type": "string",
                "description": (
                    "Expressao de tempo como a pessoa falou: '30m', '2h', '3 dias', "
                    "'amanha as 9h', '25/12 10:00', '18:30'. Atencao: '9h' sozinho e "
                    "DURACAO (daqui a 9 horas); 'as 9h' e horario do relogio."
                ),
            },
            "oque": {"type": "string", "description": "Do que lembrar, com as palavras da pessoa."},
        },
        "required": ["quando", "oque"],
    },
)
async def tool_criar_lembrete(quando: str, oque: str) -> tools.ToolResult:
    """Cria o lembrete SEMPRE pra quem esta falando, nunca pra terceiros.

    O dono e o canal vem do contexto da requisicao, nao de parametro: se o modelo
    pudesse escolher o user_id, bastaria pedir "cria um lembrete pro fulano" (ou ele se
    confundir) pra marcar em nome de outra pessoa.
    """
    contexto = tools.current_context()
    if contexto is None:
        return tools.ToolResult(json.dumps({"erro": "sem contexto de quem pediu"}, ensure_ascii=False))

    agora = datetime.now(NEWS_TIMEZONE)
    due_at = parse_when(quando, agora)
    if due_at is None:
        return tools.ToolResult(json.dumps(
            {"erro": f"nao entendi o prazo '{quando}'. Peca pra pessoa reformular (ex: '30m', 'amanha as 9h')."},
            ensure_ascii=False,
        ))
    if due_at <= agora:
        return tools.ToolResult(json.dumps(
            {"erro": "esse horario ja passou; confirme a data com a pessoa"}, ensure_ascii=False
        ))

    loop = asyncio.get_event_loop()
    reminder_id = await loop.run_in_executor(
        None, add_reminder, contexto.author_id, contexto.channel_id, oque[:500], due_at
    )
    return tools.ToolResult(json.dumps(
        {"criado": True, "id": reminder_id, "quando": format_due(due_at), "oque": oque[:500]},
        ensure_ascii=False,
    ))
