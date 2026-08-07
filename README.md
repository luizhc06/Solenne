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
- `cogs/linksummary.py` — `/resumolink`: abre uma pagina e resume o conteudo real dela.
- `cogs/reminders.py` — `/lembrete`, `/lembretes`, `/cancelarlembrete` e o loop de entrega.
- `cogs/anime.py` — `/anime` e o radar de episodios novos via AniList.

## Funcionalidades

### Chat com IA
- `/ask <pergunta>` ou mencionar o bot: conversa via modelo `nvidia/nemotron-3-super-120b-a12b`
  na API da NVIDIA (trocado do `openai/gpt-oss-120b` em jul/2026 — 12B parametros ativos,
  2.2x a vazao do anterior e superior a ele nos benchmarks da classe; a motivacao foi
  latencia, ja que cada resposta custa varias chamadas sequenciais). O modelo e so a
  env `HERMES_MODEL`, entao trocar de novo nao exige mudanca de codigo.
  Persona propria (ver `SYSTEM_PROMPT` em `cogs/chat.py`) e **raciocinio nativo do
  modelo** (`enable_thinking` + `reasoning_budget`): UMA chamada por resposta, com a
  cadeia de pensamento voltando num campo separado (`reasoning_content`) que nunca chega
  ao chat. Ate ago/2026 o bot mandava `enable_thinking=False` em toda chamada e compensava
  com 3 passadas artesanais (rascunho -> autocritica -> humanizacao) — o que na pratica
  rebaixava um modelo de raciocinio a um modelo de 12B respondendo de primeira, e ainda
  custava ~66s por resposta contra ~20-45s hoje. As passadas antigas continuam disponiveis
  por env (`REFINEMENT_ROUNDS`, `HUMANIZE_PASS`), desligadas por padrao.
- **Ferramentas que ela usa sozinha** (`tools.py`): `pesquisar_web`, `consultar_clima` e
  `resumir_link`. A Solenne DECIDE quando usar, via function calling do proprio modelo —
  antes existia so um regex procurando a palavra "pesquisa" na mensagem, entao ela sabia
  consultar clima mas nunca consultava sozinha quando alguem perguntava se ia chover.
  Os cogs se registram no `tools.py` ao serem carregados; `ai_client` so le o registro
  (o modulo separado existe pra quebrar o ciclo cog -> ai_client -> cog).
  **Os modos de raciocinio das duas chamadas sao diferentes de proposito**: orcamento na
  1a (e dela que sai a decisao E a resposta profunda quando nao ha ferramenta a usar — com
  raciocinio desligado ela chamou busca web pra "diferenca entre TCP e UDP") e `low_effort`
  na 2a (so sintetizar o que a ferramenta trouxe — foi onde o orcamento estourou e devolveu
  resposta vazia numa sonda). Medido: 10/10 decisoes corretas, nenhuma resposta vazia.
  **Custo**: com ferramentas ligadas a resposta ficou em 38-60s (era 20-45s) — as specs e a
  secao de ferramentas no prompt entram em toda mensagem. `AI_REASONING_BUDGET` e o dial.
- **Como falar com ela**: mencionando (`@Solenne`), **respondendo (reply) uma mensagem
  dela** ou **chamando pelo nome** no meio da frase (`solenne, o que voce acha?`). Os dois
  ultimos nao existiam e eram justamente os gestos mais naturais — sem eles a Solenne
  parecia ignorar as pessoas.
- **Modo ambiente**: em canais especificos (`geral`, `comidas`, `bot`, `videojogos-geral`),
  o bot responde perguntas sem precisar ser mencionado, com cooldown de 3 minutos por canal
  para nao estourar o limite de requisicoes da API. Reconhece pergunta pelo `?` **ou** por
  palavra interrogativa (`qual`, `alguem sabe`, `me explica`...) — exigir o `?` literal
  fazia perder a maioria das perguntas reais do Discord.
- **Memoria persistente**: historico de conversa por canal (SQLite, sobrevive a restart) e
  um resumo curto por pessoa, atualizado automaticamente em segundo plano com fatos uteis
  (preferencias, contexto recorrente).
- Fila global de respostas com prioridade: so processa um pedido de IA por vez, e o que
  tem gente esperando (chat, comandos) passa na frente de tarefa de fundo (digest de
  noticias). Com fila FIFO simples, quem mencionava a Solenne durante o digest do meio-dia
  ficava minutos preso atras dele vendo so o "Pensando...".
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
- `/resumolink <url>`: abre a pagina, extrai o texto real e resume em um paragrafo + bullets.
  So aceita http/https apontando pra IP publico — endereco interno (`127.0.0.1`, rede local,
  e principalmente o `169.254.169.254` de metadados da VM na Oracle) e recusado, senao
  qualquer pessoa do servidor poderia extrair credenciais da maquina por ai.

### Lembretes
- `/lembrete <quando> <o que>`: aceita duracao (`30m`, `2h`, `1h30m`, `3 dias`), dia nomeado
  (`amanha as 9h`, `hoje as 18:30`), data (`25/12 10:00`, `25/12/2027 10:00`) e horario (`18:30`).
- Desambiguacao: `9h` sozinho e *duracao* (daqui a 9 horas); `as 9h` e *horario* (9 da manha).
- `/lembretes` lista os pendentes, `/cancelarlembrete <id>` cancela (so os seus).
- O loop verifica a cada 30s e entrega no canal onde o lembrete foi criado, marcando como
  entregue na mesma transacao da leitura (nao reenvia pra sempre se o canal sumir).

### Anime
- `/anime`: proximos episodios das series marcadas como "assistindo" no AniList do dono,
  com contagem regressiva renderizada no fuso de quem le.
- Radar automatico (a cada 30 min): avisa no canal quando sai episodio novo dessas series.
  Na primeira execucao ele so registra o estado atual, sem anunciar — senao despejaria de
  uma vez o ultimo episodio de tudo que esta sendo acompanhado.

### Noticias
- `/noticias`: resumo sob demanda, ou automatico todo dia ao meio-dia (horario de Brasilia)
  em um canal com "noticias" no nome. Divisorias por categoria usam heading (`#`) do Discord.
- Fontes reais via RSS (nunca inventadas): Geek & Anime (Anime News Network, MyAnimeList),
  Tecnologia & Hardware (Tom's Hardware, Wccftech), Ciencia (ScienceDaily, Nature),
  Inteligencia Artificial (MIT Tech Review, TechCrunch AI), Brasil (G1, G1 Economia) e
  Mundo/Geopolitica (BBC, Al Jazeera). G1 Politica saiu em ago/2026: com o G1 geral ja
  cheio de politica, a categoria "Brasil" do dia virava so Brasilia.
- **Feed que "morre calado" e um risco recorrente aqui**: um veiculo aposenta a URL mas ela
  continua respondendo 200 com materia velha, entao nada falha — a categoria so fica pobre.
  Ja aconteceu com `g1.globo.com/dynamo/brasil` (parou em mai/2023) e com
  `venturebeat.com/category/ai` (parou em mai/2026). Se uma categoria empobrecer, cheque
  primeiro a data do item mais recente de cada feed dela.
- **Rodizio entre fontes**: os feeds de cada categoria sao intercalados (1 de cada, por
  recencia) antes do corte de candidatos. Concatenar e cortar no fim, como era antes,
  fazia a SEGUNDA fonte de cada categoria ser descartada inteira antes da IA ve-la.
- **Timeout nos feeds** (12s, via httpx): `feedparser.parse(url)` baixa com socket sem
  timeout, e um feed lento pendurava a thread e travava o digest todo.
- Categoria que falha ou fica vazia e reportada no fim do post em vez de sumir calada,
  e um erro numa categoria nao derruba mais as outras.
- **Abertura com a voz dela + destaque do dia**: a intro so e escrita DEPOIS da curadoria e
  recebe as manchetes selecionadas, entao ela comenta o assunto mais forte do dia em vez de
  soltar uma frase generica. Antes o prompt da intro nao recebia noticia nenhuma — era a
  razao principal do digest soar vazio.
  **A ordem dos campos do JSON importa e e proposital** (`destaque_i`, `eco`, `abertura`):
  com `abertura` primeiro, o modelo escrevia antes de ter escolhido o destaque e chegou a
  INVENTAR noticia que nao estava na lista. Geracao e autoregressiva — a ordem dos campos e
  a ordem em que ele pensa. Depois da inversao: 6/6 aberturas ancoradas em manchetes reais.
- **Curadoria em JSON estrito** (`response_format=json_object`, raciocinio desligado): ate 4
  cards por categoria, titulo de ate 90 caracteres so com o fato principal e resumo de uma
  frase, ambos em PT-BR a partir do texto real do feed, com link da fonte sempre presente.
  O formato anterior era uma linha de texto `INDICE ||| titulo ||| resumo` — o modelo
  trocava o segundo separador pelo `-` que via na lista de ENTRADA e a categoria inteira
  caia pro fallback sem traducao. Medido contra a API de producao, o JSON saiu valido 9/9.
- **Ancora anti-troca-de-link**: junto de cada escolha a IA devolve um `eco` (as 5
  primeiras palavras do titulo original). Se o indice nao bater com o eco, o item e
  recuperado pelo eco em vez de aceito — sem isso um indice trocado gera o pior erro
  possivel aqui: card com o titulo de uma noticia e o LINK e a imagem de outra, parecendo
  perfeitamente correto. Reproduzido contra a API real antes da ancora existir.
- **Quantidade variavel por categoria** (0 a 4, nao cota fixa): dia fraco vira "nada que
  valesse a pena em X" em vez de tres manchetes mornas de enchimento. Falha e dia fraco sao
  reportados em linhas separadas — uma e curadoria funcionando, a outra e coisa pra investigar.
- **Cada categoria declara seu `foco`** e o prompt manda descartar o que nao encaixa. Sem
  isso, "Trump diz que Congresso dos EUA quer regulamentar a IA" entrava na categoria Brasil
  so porque saiu num veiculo brasileiro.
- **Raciocinio fica DESLIGADO na curadoria de proposito**: medido em producao, com
  `low_effort` ligado o modelo devolve JSON valido mas deixa os resumos em ingles; com
  raciocinio desligado, traduz certo.
- **Personalizacao da categoria Geek & Anime**: busca o perfil publico do dono no
  [AniList](https://anilist.co/) (generos favoritos e series bem avaliadas, cache de 12h)
  e usa isso pra priorizar noticias relacionadas na hora de escolher as mais relevantes,
  sem excluir noticias importantes fora do perfil.
- **Deduplicacao em duas camadas**: por link, noticias ja mostradas nos ultimos 2 dias nao
  repetem entre o post automatico e execucoes manuais do `/noticias`; e por conteudo, a
  MESMA materia publicada por duas fontes (links diferentes, fato identico) e reduzida a
  uma antes mesmo da IA ver a lista de candidatos.

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
  recebem automaticamente uma versao corrigida (`fxtwitter.com`, `kkinstagram.com`,
  `vxtiktok.com`) que embeda corretamente no Discord, e a mensagem original tem o embed
  quebrado suprimido (quando o bot tem permissao de Gerenciar Mensagens no canal).
  Esses dominios de embed-fix mudam com frequencia (takedowns) - se o Instagram parar
  de embedar de novo, o dominio provavelmente caiu e precisa ser trocado em `cogs/linkfix.py`.

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
| `HERMES_MODEL` | Modelo usado no NIM (padrao: `nvidia/nemotron-3-super-120b-a12b`) |
| `ALLOWED_GUILD_ID` | ID do unico servidor onde o bot pode ficar |
| `OWNER_USER_ID` | Seu ID de usuario no Discord (dono, recebe DMs de moderacao/seguranca) |
| `AI_REASONING_BUDGET` | Teto de tokens de raciocinio por resposta de chat (padrao `768`; mais = mais profundo e mais lento) |
| `REFINEMENT_ROUNDS` | Passadas extras de autocritica por resposta (padrao `0` — o raciocinio nativo ja faz esse papel) |
| `HUMANIZE_PASS` | `1` liga uma passada final de reescrita "mais humana" (padrao `0`, custa +1 chamada) |
| `ANILIST_USERNAME` | Perfil publico do AniList usado nas noticias geek e no radar de anime (padrao `Rizuw`) |

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
  TechCrunch, G1, BBC, Al Jazeera)
- DuckDuckGo (busca web para `/pesquisa`)
