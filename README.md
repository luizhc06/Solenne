# Solenne

IA pessoal do Rizu, rodando como bot de Discord. Usa a API da NVIDIA (NIM) como
motor de inferencia e discord.py para integracao com o Discord.

## Stack

- Python 3.12 + [discord.py](https://github.com/Rapptz/discord.py)
- [openai](https://github.com/openai/openai-python) (SDK usado apontando para o endpoint OpenAI-compatible da NVIDIA)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg para musica
- Docker / Docker Compose para deploy
- VM Oracle Cloud (Always Free, VM.Standard.E2.1.Micro)

## Funcionalidades

- **Chat com IA** (`/ask`, ou mencionando o bot): conversa via modelo `openai/gpt-oss-120b`
  na API da NVIDIA, com persona propria (ver `SYSTEM_PROMPT` em `bot.py`).
- **Modo ambiente**: em canais especificos (`geral`, `comidas`, `bot`, `videojogos-geral`),
  o bot responde perguntas sem precisar ser mencionado, com cooldown de 3 minutos por canal
  para nao estourar o limite de requisicoes da API.
- **Musica**: `/play`, `/skip`, `/pause`, `/resume`, `/stop`, `/queue`.
- **Anti-flood / automod**: detecta flood de mensagens (muitas mensagens seguidas, mensagens
  repetidas ou spam de mencoes), apaga as mensagens e aplica timeout de 60s automaticamente.
  Manda uma DM para o dono com botoes de aprovacao para banir ou ignorar.
- **Comandos administrativos** (`/kick`, `/addrole`, `/removerole`, `/criarcanal`,
  `/apagarcanal`, `/lock`, `/unlock`): o bot tem as permissoes no Discord, mas os comandos
  so executam se quem chamou for o dono (`OWNER_USER_ID`).
- **Trava de servidor**: o bot sai automaticamente de qualquer servidor que nao seja o
  configurado em `ALLOWED_GUILD_ID`.

## Variaveis de ambiente

Ver `.env.example`. Copie para `.env` e preencha:

| Variavel | Descricao |
|---|---|
| `NVIDIA_API_KEY` | Chave da API NVIDIA NIM (integrate.api.nvidia.com) |
| `DISCORD_TOKEN` | Token do bot no Discord Developer Portal |
| `HERMES_MODEL` | Modelo usado no NIM (padrao: `openai/gpt-oss-120b`) |
| `ALLOWED_GUILD_ID` | ID do unico servidor onde o bot pode ficar |
| `OWNER_USER_ID` | Seu ID de usuario no Discord (dono, recebe DMs de moderacao) |

## Deploy

```bash
docker compose up -d --build
```

Requer `ffmpeg` (ja incluso na imagem Docker) e as intents privilegiadas
**Message Content** e **Server Members** habilitadas no Developer Portal do bot.

## Permissoes do bot no Discord

O bot tem permissoes amplas (cargos, canais, kick, ban) porque os comandos
administrativos passam por essas APIs — mas cada comando sensivel valida
`interaction.user.id == OWNER_USER_ID` antes de executar. Ninguem alem do dono
consegue de fato usar esses comandos, mesmo tendo acesso ao servidor.
