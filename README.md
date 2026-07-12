# Solenne

IA pessoal do Rizu, rodando como bot de Discord. Usa a API da NVIDIA (NIM) como
motor de inferencia e discord.py para integracao com o Discord.

## Stack

- Python 3.12 + [discord.py](https://github.com/Rapptz/discord.py) (bot estruturado em Cogs/extensions)
- [openai](https://github.com/openai/openai-python) `AsyncOpenAI` (SDK usado apontando para o endpoint OpenAI-compatible da NVIDIA, chamado de forma assincrona nativa)
- [feedparser](https://github.com/kurtmckee/feedparser) para os feeds RSS de noticias
- [httpx](https://github.com/encode/httpx) para as APIs de clima, alertas e busca web
- SQLite (memoria persistente, sem dependencia externa)
- Docker / Docker Compose para deploy
- VM Oracle Cloud (Always Free, VM.Standard.E2.1.Micro)

## Estrutura do codigo

O bot e modularizado em Cogs (extensions do discord.py), com a infraestrutura
compartilhada em modulos de nivel superior:

- `bot.py` — entrypoint: cria o `HermesBot` (`commands.Bot`), carrega os cogs e sincroniza os slash commands.
- `config.py` — variaveis de ambiente, logging e constantes globais.
- `db.py` — todo o acesso a SQLite (historico, perfis, dedup de noticias, backup).
- `ai_client.py` — cliente `AsyncOpenAI`, o lock global de uma resposta por vez e o pipeline de raciocinio em multiplas passadas.
- `user_profile.py` — atualizacao do resumo de perfil por pessoa.
- `views.py` / `utils.py` / `notify.py` — UI compartilhada (botoes de feedback), helpers (embed de "pensando", deteccao de pergunta) e notificacao por DM ao dono.
- `cogs/chat.py` — persona (`SYSTEM_PROMPT`), `/ask`, `/help` e o gatilho de conversa por mencao/modo ambiente.
- `cogs/search.py` — busca web e `/pesquisa`.
- `cogs/weather.py` — clima/alertas e `/clima` (com cache em memoria, ver abaixo).
- `cogs/news.py` — resumo diario de noticias e `/noticias`.
- `cogs/moderation.py` — anti-flood/automod.
- `cogs/admin.py` — comandos restritos ao dono (`/kick`, `/perturbar`, `/clear`, etc.).
- `cogs/core.py` — trava de servidor, status rotativo, backup diario e `/status`.
- `cogs/linkfix.py` — conversor de links quebrados (Twitter/Instagram/TikTok).

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
- Sabe a data/hora atual (Brasilia) em todo pedido, pra nao "alucinar" datas/dias da semana.
- Botoes de feedback (👍/👎) em respostas de chat, pesquisa e noticias, que alimentam o
  resumo de perfil da pessoa que clicou.
- `/resumo [mensagens]`: resume as ultimas mensagens do canal atual (10-100, padrao 50) em
  tom informal/fofoqueiro, usando so o historico real salvo no SQLite.

### Pesquisa
- `/pesquisa <termo>`: busca real na web (minimo 5 fontes via DuckDuckGo), resume com a IA
  citando `[1] [2]` etc. e sempre anexa os links reais das fontes usadas.
- Gatilho automatico em chat livre: falar "pesquise"/"pesquisa" mencionando o bot ou no
  modo ambiente dispara essa mesma busca em vez de responder de memoria. Restrito de
  proposito a variacoes de "pesquis-" (nao "buscar"/"procurar", de uso comum do dia a dia)
  pra nao disparar buscas sem querer.

### Noticias
- `/noticias`: resumo sob demanda, ou automatico todo dia ao meio-dia (horario de Brasilia)
  em um canal com "noticias" no nome. Divisorias por categoria usam heading (`#`) do Discord.
- Fontes reais via RSS (nunca inventadas): Geek/Tecnologia, Ciencia, Inteligencia Artificial,
  Brasil (G1) e Mundo/Geopolitica (BBC, Al Jazeera).
- Titulo e resumo traduzidos/resumidos para PT-BR pela IA a partir do texto real do feed,
  com link da fonte original sempre presente.
- **Deduplicacao**: noticias ja mostradas nos ultimos 2 dias nao repetem entre o post
  automatico e execucoes manuais do `/noticias`.

### Clima
- `/clima <cidade>`: temperatura atual, sensacao termica, umidade e previsao dos proximos
  7 dias via Open-Meteo (sem precisar de API key).
- Alertas oficiais ativos do INMET (que alimentam a Defesa Civil) para a regiao da cidade,
  quando existentes: severidade, periodo de validade, riscos e instrucoes.
- **Cache em memoria (~20 min)** para geocodificacao, previsao e alertas INMET, pra nao
  bater repetidamente nas APIs externas quando varias pessoas perguntam a mesma cidade
  em um curto periodo.

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
- `/status`: uptime, latencia com o Discord, saude da API do Open-Meteo (ping) e contagem
  de mensagens/perfis salvos no banco.

### Qualidade de vida
- **Conversor de links**: mensagens com links do Twitter/X, Instagram (post/reel) ou TikTok
  recebem automaticamente uma versao corrigida (`fxtwitter.com`, `ddinstagram.com`,
  `vxtiktok.com`) que embeda corretamente no Discord, e a mensagem original tem o embed
  quebrado suprimido (quando o bot tem permissao de Gerenciar Mensagens no canal).

### Infraestrutura e confiabilidade
- **Backup diario** do banco SQLite (3h da manha, horario de Brasilia), com retencao de
  7 dias e limpeza automatica dos backups mais antigos (nao enche o disco com o tempo).
- **Limite de memoria no Docker** (700MB) pra nao arriscar travar a VM inteira (1GB total)
  se algum processo vazar memoria.

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

## Testes

Testes unitarios cobrem so as funcoes puras/deterministicas (cache TTL do clima,
normalizacao de texto, deteccao de gatilhos de busca, conversor de links etc.):

```bash
pip install -r requirements-dev.txt
pytest
```

## Fontes de dados externas

Nenhuma dessas exige API key:

- [Open-Meteo](https://open-meteo.com/) — geocodificacao e previsao do tempo
- [INMET](https://apiprevmet3.inmet.gov.br/) — alertas meteorologicos oficiais (Defesa Civil)
- Feeds RSS publicos (The Verge, Ars Technica, ScienceDaily, Nature, MIT Technology Review,
  VentureBeat, G1, BBC, Al Jazeera)
- DuckDuckGo (busca web para `/pesquisa`)
