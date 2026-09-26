# Comandos da Solenne

Referencia completa de todo slash command que o bot expoe — 32 no total, extraidos
direto do `app_commands` registrado em cada cog, nao escritos a mao. Pra entender
**como** cada funcionalidade se comporta por tras (regras de parsing de lembrete,
moderacao automatica, cache de clima etc.), veja o [`README`](README.md#funcionalidades);
aqui e so "o que existe e quais parametros aceita".

**Legenda:** `Dono` = so executa se `interaction.user.id == OWNER_USER_ID` (ver
`owner_only()` em `cogs/admin.py`) — o Discord ainda mostra o comando pra qualquer
membro no autocomplete de "/", so a execucao e restrita. `Todos` = qualquer pessoa
no servidor pode usar. Um `?` depois do nome do parametro significa que ele e
opcional.

## Indice

- [Moderacao e admin](#moderacao-e-admin-cogsadminpy)
- [Emojis e figurinhas](#emojis-e-figurinhas-cogsadminpy)
- [Conversa com IA](#conversa-com-ia-cogschatpy)
- [Pesquisa](#pesquisa-cogssearchpy)
- [Links](#links-cogslinksummarypy)
- [Noticias](#noticias-cogsnewspy)
- [Clima](#clima-cogsweatherpy)
- [Lembretes](#lembretes-cogsreminderspy)
- [Anime](#anime-cogsanimepy)
- [Enquetes](#enquetes-cogspollspy)
- [Nivel & XP](#nivel--xp-cogslevelingpy)
- [Video](#video-cogsvideotoolspy)
- [Status](#status-cogscorepy)

## Moderacao e admin (`cogs/admin.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/kick` | `usuario` `motivo?` | Dono | Expulsa um membro do servidor |
| `/addrole` | `usuario` `cargo` | Dono | Adiciona um cargo a um membro |
| `/removerole` | `usuario` `cargo` | Dono | Remove um cargo de um membro |
| `/criarcanal` | `nome` `tipo` | Dono | Cria um canal de texto ou voz (`tipo` e um dropdown: Texto/Voz) |
| `/apagarcanal` | `canal` | Dono | Apaga um canal |
| `/lock` | `canal?` | Dono | Tranca um canal — ninguem consegue mandar mensagem (padrao: canal atual) |
| `/unlock` | `canal?` | Dono | Destranca um canal (padrao: canal atual) |
| `/clear` | `quantidade` | Dono | Apaga as ultimas N mensagens do canal (1-100) |
| `/perturbar` | `usuario` `vezes?` `intervalo?` | Dono | A Solenne perturba um usuario no canal, de brincadeira, por um tempinho |

## Emojis e figurinhas (`cogs/admin.py`)

Precisam da permissao **Gerenciar Emojis e Figurinhas** no servidor, alem de serem
restritos ao dono.

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/addemoji` | `nome` `imagem` | Dono | Sobe um emoji customizado (PNG/JPG/GIF/WEBP, ate 256KB) |
| `/removeemoji` | `emoji` (autocomplete) | Dono | Remove um emoji — aceita colar o emoji ou digitar o nome |
| `/addfigurinha` | `nome` `descricao` `emoji_relacionado` `imagem` | Dono | Sobe uma figurinha (PNG/APNG, ate 512KB) |
| `/removefigurinha` | `figurinha` (autocomplete) | Dono | Remove uma figurinha por nome |

## Conversa com IA (`cogs/chat.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/ask` | `pergunta` | Todos | Pergunta algo a Solenne (mesmo raciocinio do chat livre, como comando explicito) |
| `/resumo` | `mensagens?` | Todos | Resume o que rolou de conversa recente no canal (10-100 mensagens, padrao 50) |
| `/help` | — | Todos | Mostra os comandos da Solenne, organizados por categoria num embed |

## Pesquisa (`cogs/search.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/pesquisa` | `termo` | Todos | Pesquisa real na web e resume com links das fontes |

## Links (`cogs/linksummary.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/resumolink` | `url` | Todos | Abre um link e resume o conteudo real da pagina |

## Noticias (`cogs/news.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/noticias` | — | Todos | Manda um resumo de noticias agora, fora do horario do digest automatico |

## Clima (`cogs/weather.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/clima` | `cidade` | Todos | Clima atual e previsao da semana, com alerta oficial de Defesa Civil/INMET quando for no Brasil |

## Lembretes (`cogs/reminders.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/lembrete` | `quando` `oque` | Todos | Marca um lembrete pra depois (aceita duracao, dia nomeado, data ou horario) |
| `/lembretes` | — | Todos | Lista seus lembretes pendentes |
| `/cancelarlembrete` | `id` | Todos | Cancela um lembrete pendente seu (o `id` aparece em `/lembretes`) |

## Anime (`cogs/anime.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/anime` | — | Todos | Proximos episodios das series que o Rizu acompanha no AniList (consulta a conta dele na hora) |

## Enquetes (`cogs/polls.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/enquete` | `pergunta` `opcao1` `opcao2` `opcao3?` `opcao4?` `opcao5?` `duracao_minutos?` `anonimo?` | Todos | Cria uma enquete com botoes (ate 5 opcoes) |
| `/encerrarenquete` | `id` | Todos* | Encerra uma enquete e mostra o resultado final |
| `/enquetes` | — | Todos | Lista as enquetes abertas no momento |

\* `/encerrarenquete` e visivel e executavel por qualquer pessoa, mas so fecha de
verdade se quem chamou for o autor da enquete ou o dono do bot — a checagem e por
enquete, nao por comando.

## Nivel & XP (`cogs/leveling.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/rank` | `usuario?` | Todos | Mostra nivel e XP seu ou de outra pessoa |
| `/leaderboard` | — | Todos | Mostra o top 10 do servidor por XP |

## Video (`cogs/videotools.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/paragif` | `video` `duracao?` `fps?` `largura?` | Todos | Converte um video anexado em GIF (via ffmpeg) |
| `/extrairaudio` | `video` `formato?` | Todos | Extrai o audio de um video anexado (mp3/wav/m4a) |

## Status (`cogs/core.py`)

| Comando | Parametros | Quem usa | O que faz |
|---|---|---|---|
| `/status` | — | Todos | Uptime, latencia com o Discord, saude da API de clima e contagem de mensagens/perfis no banco |
