# Solenne

IA pessoal do Rizu, rodando como bot de Discord. Usa a API da NVIDIA (NIM) como
motor de inferencia e discord.py para integracao com o Discord.

## Stack

- Python 3.12 + [discord.py](https://github.com/Rapptz/discord.py)
- [openai](https://github.com/openai/openai-python) (SDK usado apontando para o endpoint OpenAI-compatible da NVIDIA)
- [feedparser](https://github.com/kurtmckee/feedparser) para os feeds RSS de noticias
- [httpx](https://github.com/encode/httpx) para as APIs de clima, alertas e busca web
- SQLite (memoria persistente, sem dependencia externa)
- Docker / Docker Compose para deploy
- VM Oracle Cloud (Always Free, VM.Standard.E2.1.Micro)

## Funcionalidades

### Chat com IA
- `/ask <pergunta>` ou mencionar o bot: conversa via modelo `openai/gpt-oss-120b` na API
  da NVIDIA, com persona propria (ver `SYSTEM_PROMPT` em `bot.py`) e raciocinio em multiplas
  passadas (rascunho, autocritica/refino e humanizacao) antes de responder.
- **Modo ambiente**: em canais especificos (`geral`, `comidas`, `bot`, `videojogos-geral`),
  o bot responde perguntas sem precisar ser mencionado, com cooldown de 3 minutos por canal
  para nao estourar o limite de requisicoes da API.
- **Memoria persistente**: historico de conversa por canal (SQLite, sobrevive a restart) e
  um resumo curto por pessoa, atualizado automaticamente em segundo plano com fatos uteis
  (preferencias, contexto recorrente).
- Fila global de respostas: so processa um pedido de IA por vez, sem atropelar respostas
  quando varias pessoas usam comandos ao mesmo tempo.
- Indicador de "pensando" (embed com GIF e estimativa de tempo) enquanto gera a resposta.
- Nunca responde mensagens recebidas em DM.

### Pesquisa
- `/pesquisa <termo>`: busca real na web (minimo 5 fontes via DuckDuckGo), resume com a IA
  citando `[1] [2]` etc. e sempre anexa os links reais das fontes usadas.

### Noticias
- `/noticias`: resumo sob demanda, ou automatico todo dia ao meio-dia (horario de Brasilia)
  em um canal com "noticias" no nome.
- Fontes reais via RSS (nunca inventadas): Geek/Tecnologia, Ciencia, Inteligencia Artificial,
  Brasil (G1) e Mundo/Geopolitica (BBC, Al Jazeera).
- Titulo e resumo traduzidos/resumidos para PT-BR pela IA a partir do texto real do feed,
  com imagem por noticia (quando disponivel) e link da fonte original sempre presente.

### Clima
- `/clima <cidade>`: temperatura atual, sensacao termica, umidade e previsao dos proximos
  7 dias via Open-Meteo (sem precisar de API key).
- Alertas oficiais ativos do INMET (que alimentam a Defesa Civil) para a regiao da cidade,
  quando existentes: severidade, periodo de validade, riscos e instrucoes.

### Moderacao e seguranca
- **Anti-flood / automod**: detecta flood de mensagens (muitas mensagens seguidas, mensagens
  repetidas ou spam de mencoes), apaga as mensagens e aplica timeout de 60s automaticamente.
  Manda uma DM para o dono com botoes de aprovacao para banir ou ignorar.
- **Comandos administrativos** (`/kick`, `/addrole`, `/removerole`, `/criarcanal`,
  `/apagarcanal`, `/lock`, `/unlock`): o bot tem as permissoes no Discord, mas os comandos
  so executam se quem chamou for o dono (`OWNER_USER_ID`).
- **Trava de servidor**: o bot sai automaticamente de qualquer servidor que nao seja o
  configurado em `ALLOWED_GUILD_ID`. Se alguem adicionar o bot em outro servidor, o dono
  recebe uma DM com o nome do servidor e, quando possivel (via audit log), quem adicionou.
- **Notificacao de tentativas de comando restrito**: se alguem sem permissao tentar usar
  um comando administrativo, o dono recebe uma DM com quem tentou, o comando e onde.

### Status
- O status/atividade do bot roda a cada 30 minutos entre frases como "Fazendo automod",
  "Pesquisando clima", "Investigando noticias", etc.

## Variaveis de ambiente

Ver `.env.example`. Copie para `.env` e preencha:

| Variavel | Descricao |
|---|---|
| `NVIDIA_API_KEY` | Chave da API NVIDIA NIM (integrate.api.nvidia.com) |
| `DISCORD_TOKEN` | Token do bot no Discord Developer Portal |
| `HERMES_MODEL` | Modelo usado no NIM (padrao: `openai/gpt-oss-120b`) |
| `ALLOWED_GUILD_ID` | ID do unico servidor onde o bot pode ficar |
| `OWNER_USER_ID` | Seu ID de usuario no Discord (dono, recebe DMs de moderacao/seguranca) |

## Deploy

```bash
docker compose up -d --build
```

Requer as intents privilegiadas **Message Content** e **Server Members**
habilitadas no Developer Portal do bot.

> Nota: o bot ja teve suporte a musica (`/play`, `/join`, etc.), removido porque
> a VM Always Free (1 vCPU / 1GB RAM) nao sustenta conexao de voz estavel com o
> Discord (handshakes de voz falhando por limitacao de rede/CPU).

## Permissoes do bot no Discord

O bot tem permissoes amplas (cargos, canais, kick, ban) porque os comandos
administrativos passam por essas APIs — mas cada comando sensivel valida
`interaction.user.id == OWNER_USER_ID` antes de executar. Ninguem alem do dono
consegue de fato usar esses comandos, mesmo tendo acesso ao servidor.

## Fontes de dados externas

Nenhuma dessas exige API key:

- [Open-Meteo](https://open-meteo.com/) — geocodificacao e previsao do tempo
- [INMET](https://apiprevmet3.inmet.gov.br/) — alertas meteorologicos oficiais (Defesa Civil)
- Feeds RSS publicos (The Verge, Ars Technica, ScienceDaily, Nature, MIT Technology Review,
  VentureBeat, G1, BBC, Al Jazeera)
- DuckDuckGo (busca web para `/pesquisa`)
