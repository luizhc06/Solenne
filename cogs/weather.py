import time
import asyncio
import logging
import unicodedata
from datetime import datetime

import httpx
import discord
from discord import app_commands
from discord.ext import commands

from config import DIAS_SEMANA

log = logging.getLogger("hermes-bot")

WEATHER_CODE_MAP = {
    0: ("☀️", "Ceu limpo"),
    1: ("🌤️", "Poucas nuvens"),
    2: ("⛅", "Parcialmente nublado"),
    3: ("☁️", "Nublado"),
    45: ("🌫️", "Nevoeiro"),
    48: ("🌫️", "Nevoeiro com geada"),
    51: ("🌦️", "Garoa fraca"),
    53: ("🌦️", "Garoa"),
    55: ("🌦️", "Garoa forte"),
    61: ("🌧️", "Chuva fraca"),
    63: ("🌧️", "Chuva"),
    65: ("🌧️", "Chuva forte"),
    71: ("❄️", "Neve fraca"),
    73: ("❄️", "Neve"),
    75: ("❄️", "Neve forte"),
    80: ("🌦️", "Pancadas de chuva fracas"),
    81: ("🌦️", "Pancadas de chuva"),
    82: ("⛈️", "Pancadas de chuva fortes"),
    95: ("⛈️", "Trovoada"),
    96: ("⛈️", "Trovoada com granizo"),
    99: ("⛈️", "Trovoada forte com granizo"),
}

BRAZIL_STATE_UF = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA",
    "ceara": "CE", "distrito federal": "DF", "espirito santo": "ES", "goias": "GO",
    "maranhao": "MA", "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG",
    "para": "PA", "paraiba": "PB", "parana": "PR", "pernambuco": "PE", "piaui": "PI",
    "rio de janeiro": "RJ", "rio grande do norte": "RN", "rio grande do sul": "RS",
    "rondonia": "RO", "roraima": "RR", "santa catarina": "SC", "sao paulo": "SP",
    "sergipe": "SE", "tocantins": "TO",
}

# Cache simples em memoria (TTL) para as chamadas de clima - evita bater
# repetidamente nas APIs externas quando varias pessoas pedem a mesma cidade
# em um curto periodo. Sem dependencia nova: so um dict com timestamp de expiracao.
WEATHER_CACHE_TTL_SECONDS = 20 * 60


class _TTLCache:
    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._store: dict = {}

    def get(self, key):
        entry = self._store.get(key)
        if entry is None:
            return None, False
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None, False
        return value, True

    def set(self, key, value):
        self._store[key] = (time.monotonic() + self.ttl, value)


_geocode_cache = _TTLCache(WEATHER_CACHE_TTL_SECONDS)
_forecast_cache = _TTLCache(WEATHER_CACHE_TTL_SECONDS)
_inmet_cache = _TTLCache(WEATHER_CACHE_TTL_SECONDS)


def _weather_desc(code) -> tuple[str, str]:
    return WEATHER_CODE_MAP.get(code, ("🌡️", "Condicao desconhecida"))


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _state_to_uf(state_name: str) -> str | None:
    return BRAZIL_STATE_UF.get(_strip_accents(state_name).lower())


def _geocode_city_sync(city: str) -> dict | None:
    cache_key = _strip_accents(city).lower().strip()
    cached, hit = _geocode_cache.get(cache_key)
    if hit:
        return cached
    try:
        resp = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "pt", "format": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.exception("Erro ao geocodificar cidade %s", city)
        return None
    results = data.get("results")
    if not results:
        _geocode_cache.set(cache_key, None)
        return None
    r = results[0]
    location = {
        "name": r["name"],
        "state": r.get("admin1", ""),
        "country": r.get("country", ""),
        "lat": r["latitude"],
        "lon": r["longitude"],
    }
    _geocode_cache.set(cache_key, location)
    return location


def _fetch_forecast_sync(lat: float, lon: float) -> dict | None:
    cache_key = (round(lat, 4), round(lon, 4))
    cached, hit = _forecast_cache.get(cache_key)
    if hit:
        return cached
    try:
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,relative_humidity_2m,apparent_temperature",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "timezone": "America/Sao_Paulo",
                "forecast_days": 7,
            },
            timeout=10,
        )
        resp.raise_for_status()
        forecast = resp.json()
    except Exception:
        log.exception("Erro ao buscar previsao do tempo")
        return None
    _forecast_cache.set(cache_key, forecast)
    return forecast


def _fetch_inmet_alerts_sync(city: str, uf: str) -> list[dict]:
    cached, hit = _inmet_cache.get("all")
    if hit:
        data = cached
    else:
        try:
            resp = httpx.get("https://apiprevmet3.inmet.gov.br/avisos/ativos", timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            log.exception("Erro ao buscar alertas INMET")
            return []
        _inmet_cache.set("all", data)
    target = f"{city} - {uf}"
    alerts = []
    for item in data.get("hoje", []) + data.get("futuro", []):
        if target in item.get("municipios", ""):
            alerts.append(item)
    return alerts


def build_weather_embed(location: dict, forecast: dict) -> discord.Embed:
    current = forecast.get("current", {})
    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    emoji, desc = _weather_desc(current.get("weather_code"))

    description = f"**{temp}°C** — {desc}"
    if feels is not None:
        description += f" (sensacao de {feels}°C)"

    embed = discord.Embed(
        title=f"{emoji} Clima em {location['name']}, {location['state']}",
        description=description,
        color=discord.Color.blue(),
    )
    if humidity is not None:
        embed.add_field(name="Umidade", value=f"{humidity}%", inline=True)

    daily = forecast.get("daily", {})
    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    max_t = daily.get("temperature_2m_max", [])
    min_t = daily.get("temperature_2m_min", [])

    forecast_lines = []
    for i, date_str in enumerate(dates):
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        dia = DIAS_SEMANA[dt.weekday()][:3]
        e, d = _weather_desc(codes[i] if i < len(codes) else None)
        forecast_lines.append(
            f"> {e} **{dia}** ({dt.strftime('%d/%m')}): {min_t[i]:.0f}°C - {max_t[i]:.0f}°C, {d}"
        )

    if forecast_lines:
        embed.add_field(name="Previsao da semana", value="\n".join(forecast_lines), inline=False)

    embed.set_footer(text="Fonte: Open-Meteo")
    return embed


def build_alert_embed(alerts: list[dict]) -> discord.Embed:
    embed = discord.Embed(title="⚠️ Alerta de Defesa Civil / INMET", color=discord.Color.red())
    for alert in alerts[:5]:
        cor_hex = str(alert.get("aviso_cor", "#FF0000")).lstrip("#")
        try:
            embed.color = discord.Color(int(cor_hex, 16))
        except ValueError:
            pass
        periodo = f"{alert.get('inicio', '?')} ate {alert.get('fim', '?')}"
        riscos = " ".join(alert.get("riscos", []))[:500]
        value = f"**Severidade:** {alert.get('severidade', '?')}\n**Periodo:** {periodo}\n{riscos}"
        embed.add_field(name=alert.get("descricao", "Aviso"), value=value[:1024], inline=False)
    embed.set_footer(text="Fonte: INMET (avisos.inmet.gov.br)")
    return embed


class WeatherCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="clima", description="Clima atual e previsao da semana pra uma cidade")
    @app_commands.describe(cidade="Nome da cidade")
    async def clima(self, interaction: discord.Interaction, cidade: str):
        await interaction.response.defer(thinking=True)
        loop = asyncio.get_event_loop()

        location = await loop.run_in_executor(None, _geocode_city_sync, cidade)
        if location is None:
            await interaction.followup.send(f"Nao encontrei a cidade '{cidade}'.")
            return

        forecast = await loop.run_in_executor(None, _fetch_forecast_sync, location["lat"], location["lon"])
        if forecast is None:
            await interaction.followup.send("Nao consegui buscar a previsao do tempo agora, tenta de novo.")
            return

        embeds = [build_weather_embed(location, forecast)]

        if location.get("country") == "Brasil":
            uf = _state_to_uf(location["state"])
            if uf:
                alerts = await loop.run_in_executor(None, _fetch_inmet_alerts_sync, location["name"], uf)
                if alerts:
                    embeds.append(build_alert_embed(alerts))

        await interaction.followup.send(embeds=embeds)


async def setup(bot: commands.Bot):
    await bot.add_cog(WeatherCog(bot))
