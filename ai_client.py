import re
import json
import asyncio
import logging
import contextlib

import tools

from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

import httpx

from config import (
    NVIDIA_API_KEY,
    MODEL,
    REFINEMENT_ROUNDS,
    REASONING_BUDGET,
    HUMANIZE_PASS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    AI_CONCURRENCY_LIMIT,
)

log = logging.getLogger("hermes-bot")

# Timeout explicito e mais curto (achado do conselho, 18/08/2026): sem isto, o SDK da
# OpenAI usa o padrao dele por baixo (read=600s, ate 2 retries proprios do SDK) -
# empilhado sobre os proprios retries de _request() abaixo (is_transient_ai_error), uma
# unica chamada lenta podia segurar o ai_gate por minutos. max_retries=0 no cliente
# porque o retry de verdade ja e feito por _request(), que sabe distinguir erro
# transitorio de definitivo (o SDK sozinho nao sabe disso).
client_ai = AsyncOpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=NVIDIA_API_KEY,
    timeout=httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=5.0),
    max_retries=0,
)


# --------------------------------------------------------------------------------------
# Modos de raciocinio
#
# O Nemotron 3 Super e um modelo DE RACIOCINIO: ele gera uma cadeia de pensamento e so
# depois a resposta final. Ate ago/2026 o bot mandava enable_thinking=False em TODA
# chamada - a correcao de um bug real (noticias saindo em ingles/cruas porque o
# raciocinio estourava o max_tokens antes da linha formatada), mas aplicada no lugar
# errado: desligar o raciocinio transforma um modelo 120B/12B-ativos num modelo de 12B
# que responde de primeira. Era essa a causa da Solenne parecer "bobinha" comparada ao
# gpt-oss-120b, que raciocina por padrao.
#
# A API devolve o raciocinio num campo SEPARADO (reasoning_content), nunca misturado com
# o content - confirmado contra a API de producao. Ou seja: da pra ter raciocinio sem
# sujar nada que seja parseado depois, desde que o max_tokens comporte os dois.
#
# OFF    - sem raciocinio. Para saida estruturada (JSON) e tarefas mecanicas, onde
#          raciocinar chega a atrapalhar: medido em producao, low_effort deixou os
#          resumos das noticias EM INGLES enquanto OFF traduzia certo.
# LOW    - raciocinio de baixo esforco (trace de ~1 linha). Barato, para tarefas de
#          julgamento simples.
# BUDGET - raciocinio completo com teto de tokens (AI_REASONING_BUDGET). E o modo do
#          chat: e o que devolve a Solenne com opiniao formada em vez de resposta rasa.
# --------------------------------------------------------------------------------------
THINK_OFF = "off"
THINK_LOW = "low"
THINK_BUDGET = "budget"


def thinking_kwargs(mode: str, budget: int = REASONING_BUDGET) -> dict:
    """Monta o chat_template_kwargs do modo pedido. Funcao pura, testavel sem rede."""
    if mode == THINK_LOW:
        return {"chat_template_kwargs": {"enable_thinking": True, "low_effort": True}}
    if mode == THINK_BUDGET:
        return {"chat_template_kwargs": {"enable_thinking": True, "reasoning_budget": budget}}
    return {"chat_template_kwargs": {"enable_thinking": False}}


class PriorityGate:
    """Limita quantas chamadas de IA rodam ao mesmo tempo, mas deixa quem tem gente
    esperando (interativo) passar na frente de tarefa de fundo.

    Ate 18/08/2026 era um asyncio.Lock unico e justo (FIFO) - 1 chamada de IA por vez,
    sempre, compartilhado entre chat e tarefas de fundo (digest de noticias, atualizacao
    de perfil). Achado do conselho de agentes daquele dia: isso desperdicava capacidade
    real da API (o teto pratico e ~40 requisicoes/minuto por conta no tier gratuito da
    NVIDIA, bem acima do que 1 chamada serial de ~30-45s por vez consegue emitir) e
    criava fila de ATE ~15 MINUTOS pra 20 pessoas conversando ao mesmo tempo - perto do
    limite de 15min do token de interacao do Discord. Virou um asyncio.Semaphore(N)
    (concurrency, ver AI_CONCURRENCY_LIMIT em config.py), mantendo a MESMA regra de
    prioridade de antes: tarefa de fundo so pega um slot quando nao ha ninguem
    interativo esperando por um. Concurrency=1 (padrao do construtor, nao da instancia
    de producao abaixo) preserva o comportamento antigo exato - e o que os testes que
    validam a ordem de prioridade sob disputa real (FIFO saturado) usam de proposito.
    """

    def __init__(self, concurrency: int = 1):
        self._sem = asyncio.Semaphore(concurrency)
        self._waiting_interactive = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @contextlib.asynccontextmanager
    async def interactive(self):
        self._waiting_interactive += 1
        self._idle.clear()
        try:
            await self._sem.acquire()
        finally:
            self._waiting_interactive -= 1
            if self._waiting_interactive == 0:
                self._idle.set()
        try:
            yield
        finally:
            self._sem.release()

    @contextlib.asynccontextmanager
    async def background(self):
        while True:
            await self._idle.wait()
            await self._sem.acquire()
            # Um interativo pode ter chegado entre o wait e o acquire: devolve a vez.
            if self._waiting_interactive == 0:
                break
            self._sem.release()
            await asyncio.sleep(0)
        try:
            yield
        finally:
            self._sem.release()


ai_gate = PriorityGate(concurrency=AI_CONCURRENCY_LIMIT)


CRITIQUE_PROMPT = (
    "Releia sua resposta anterior com espirito critico, como se fosse outra pessoa "
    "revisando. Aponte pra si mesma: falhas de logica, coisas incertas apresentadas "
    "com confianca demais, floreio ou enrolacao desnecessaria, partes genericas demais. "
    "Depois reescreva uma versao melhor: mais precisa, mais direta, cortando o que "
    "sobrou. Responda somente com a nova versao da resposta, sem comentar o processo "
    "nem citar a critica."
)

HUMANIZE_PROMPT = (
    "Reescreva essa resposta final para soar como uma pessoa de verdade conversando "
    "no Discord, nao como um assistente robotico: cadencia natural, sem parecer "
    "checklist nem relatorio. NAO invente, NAO remova ressalvas e NAO transforme "
    "incerteza em certeza - mantenha exatamente os mesmos fatos e as mesmas duvidas, "
    "so muda o jeito de falar e corta o que sobrou. Responda somente com o texto "
    "final, pronto para enviar."
)


# A API da NVIDIA da 504/timeout com alguma frequencia sob carga. Uma falha dessas
# derrubava a resposta inteira e a pessoa via so "deu ruim". Retry curto com backoff
# resolve a maioria; erro definitivo (chave invalida, modelo inexistente) nao e
# retentado, porque tentar de novo so faz esperar mais pelo mesmo erro.
AI_MAX_ATTEMPTS = 3
AI_RETRY_BASE_DELAY_SECONDS = 2


class EmptyAIResponse(Exception):
    """Modelo respondeu sem texto util (tipicamente o raciocinio comeu o max_tokens).

    Existe como erro proprio porque antes isso virava uma string vazia que descia ate o
    Discord, onde editar/enviar mensagem vazia levanta HTTPException DEPOIS do try/except
    do cog - resultado: a Solenne simplesmente nao respondia, sem erro visivel nem log.
    """


class TruncatedAIResponse(Exception):
    """A geracao bateu no teto de max_tokens e parou no meio.

    Nunca deve descer pro Discord. Com raciocinio ligado, o corte no meio do trace e o
    que produz o vazamento: o modelo ainda estava PENSANDO quando o orcamento acabou,
    entao o que sobrou em content e raciocinio cru - as vezes sem nem a tag <think> pra
    clean_reply se agarrar, porque o parser do NIM so separa o trace quando consegue ver
    o fechamento. Tratar como erro deixa o chamador refazer sem raciocinar, que cabe
    folgado no orcamento e sai em pt-BR.
    """


def is_transient_ai_error(exc: Exception) -> bool:
    """Se vale a pena tentar de novo. Funcao pura pra poder ser testada."""
    if isinstance(exc, AuthenticationError):
        return False
    # Resposta vazia nao e sorte: e o raciocinio estourando o max_tokens. Repetir a
    # MESMA chamada da o mesmo resultado, so que 3x mais devagar - quem trata isso e o
    # chamador, refazendo com outros parametros.
    if isinstance(exc, EmptyAIResponse):
        return False
    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        # 404 num POST pra /chat/completions com modelo que existe nao e "nao
        # encontrado": e soluco do roteamento do NIM. Visto em producao em 06/08/2026 -
        # duas tentativas seguidas da mesma categoria de noticias tomaram 404 enquanto
        # as outras cinco do MESMO digest voltaram 200, e a reproducao minutos depois
        # passou 6/6 com prompt e parametros identicos. Como nao era retentado, o soluco
        # derrubava a categoria inteira pro fallback sem traducao.
        #
        # Se HERMES_MODEL estiver de fato errado, isso vira 3 tentativas antes de
        # desistir - alguns segundos a mais num erro que ja seria fatal de qualquer
        # forma, e o corpo da resposta vai pro log pra distinguir os dois casos.
        return exc.status_code >= 500 or exc.status_code in (404, 429)
    return False


def friendly_ai_error(exc: Exception) -> str:
    """Mensagem pro Discord que diz o que houve, sem vazar traceback nem chave.

    Antes era sempre "Deu ruim aqui consultando o modelo", pra qualquer causa -
    chave invalida, 504 e timeout ficavam indistinguiveis pra quem estava no canal
    e pro dono lendo depois.
    """
    if isinstance(exc, AuthenticationError):
        return "Minha chave da API foi recusada. Isso e coisa pro Rizu resolver, nao adianta tentar de novo."
    if isinstance(exc, RateLimitError):
        return "Bati no limite de requisicoes da API agora. Me da uns minutos e pergunta de novo."
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return "A API demorou demais pra responder e desisti. Tenta de novo em instantes."
    if isinstance(exc, EmptyAIResponse):
        return "Me embananei toda pensando e acabei nao respondendo nada. Pergunta de novo?"
    if isinstance(exc, APIStatusError):
        if exc.status_code >= 500:
            return f"A API da NVIDIA respondeu {exc.status_code} (problema do lado deles). Tenta de novo em instantes."
        return f"A API recusou o pedido ({exc.status_code}). Se persistir, e configuracao minha."
    return "Deu ruim aqui consultando o modelo, tenta de novo em instantes."


# Sobras que o modelo as vezes deixa no texto final e que nao devem chegar ao Discord:
# a tag de raciocinio (quando o backend nao separa em reasoning_content) e o proprio
# nome dela como prefixo de fala ("Solenne: ...", "Solenne aqui:"), visto na sonda
# contra a API real depois de religar o raciocinio.
THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# Trace TRUNCADO: o modelo abriu <think> e o max_tokens cortou a geracao antes do
# fechamento. Como THINK_TAG_RE exige o par completo, sem isso o raciocinio cru inteiro
# passa batido e vai pro Discord - foi o vazamento visto em 06/09/2026, em que a Solenne
# despejou varios paragrafos de "So the user is asking... Wait, maybe they want..." em
# ingles, cortados no meio da frase.
THINK_OPEN_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
# Fechamento ORFAO: backend que separa o trace em reasoning_content mas ainda emite o
# </think> no content. Tudo que vem antes dele e raciocinio, nao resposta.
THINK_CLOSE_RE = re.compile(r"\A.*?</think>", re.DOTALL | re.IGNORECASE)
NAME_PREFIX_RE = re.compile(r"^\s*(solenne|hermes)\s*(aqui)?\s*[:\-–]\s*", re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def clean_reply(text: str) -> str:
    """Tira as sobras de formatacao antes de mandar pro Discord.

    A ordem importa: tira os pares completos primeiro, depois o fechamento orfao (que
    so aparece se o par ja nao existia) e por ultimo a abertura sem fechamento. Fazer
    o inverso deixaria THINK_OPEN_RE comer a resposta boa que vem depois de um par.
    """
    text = THINK_TAG_RE.sub("", text or "")
    text = THINK_CLOSE_RE.sub("", text)
    text = THINK_OPEN_RE.sub("", text)
    text = NAME_PREFIX_RE.sub("", text.strip())
    return text.strip()


async def _request(
    messages: list[dict],
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = 800,
    thinking: str = THINK_OFF,
    json_mode: bool = False,
    tools: list[dict] | None = None,
):
    """Uma chamada ao modelo, com retry, devolvendo a mensagem crua.

    Separada de `_complete` porque com ferramentas o normal e content VAZIO e
    tool_calls preenchido - a validacao de "veio vazio" so vale pra quem esperava texto.
    """
    extra_kwargs = {}
    if json_mode:
        extra_kwargs["response_format"] = {"type": "json_object"}
    if tools:
        extra_kwargs["tools"] = tools
        extra_kwargs["tool_choice"] = "auto"

    last_error: Exception | None = None
    for attempt in range(AI_MAX_ATTEMPTS):
        try:
            completion = await client_ai.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=temperature,
                top_p=DEFAULT_TOP_P,
                max_tokens=max_tokens,
                extra_body=thinking_kwargs(thinking),
                **extra_kwargs,
            )
            return completion.choices[0]
        except Exception as exc:
            if not is_transient_ai_error(exc):
                raise
            last_error = exc
            # 404 e ambiguo (soluco do NIM x HERMES_MODEL errado): loga o corpo da
            # resposta pra dar pra distinguir os dois casos so pelo log.
            if isinstance(exc, APIStatusError) and exc.status_code == 404:
                corpo = getattr(getattr(exc, "response", None), "text", "")
                log.warning("404 da API. Corpo: %s", (corpo or "(vazio)")[:300])
            if attempt < AI_MAX_ATTEMPTS - 1:
                delay = AI_RETRY_BASE_DELAY_SECONDS * (2**attempt)
                log.warning(
                    "Erro transitorio da API (%s), tentativa %s/%s, nova tentativa em %ss",
                    type(exc).__name__, attempt + 1, AI_MAX_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)

    raise last_error


async def _complete(
    messages: list[dict],
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = 800,
    thinking: str = THINK_OFF,
    json_mode: bool = False,
) -> str:
    """Uma chamada que TEM que voltar com texto."""
    choice = await _request(
        messages, temperature=temperature, max_tokens=max_tokens,
        thinking=thinking, json_mode=json_mode,
    )
    # Truncada e pior que vazia: vem com texto, parece resposta e passa por qualquer
    # checagem de "veio vazio". Barra ANTES de limpar - o que sobrou nao e resposta.
    if choice.finish_reason == "length":
        raise TruncatedAIResponse(
            f"geracao truncada (max_tokens={max_tokens}, thinking={thinking})"
        )
    # A API as vezes retorna content=None (sem levantar erro) em vez de string vazia.
    content = clean_reply(choice.message.content or "")
    if not content:
        # Com raciocinio ligado isso quase sempre e max_tokens curto demais: o
        # trace consumiu o orcamento inteiro e sobrou zero pra resposta.
        raise EmptyAIResponse(
            f"resposta vazia (finish_reason={choice.finish_reason}, "
            f"max_tokens={max_tokens}, thinking={thinking})"
        )
    return content


async def complete_json(
    prompt: str,
    max_tokens: int = 1800,
    temperature: float = DEFAULT_TEMPERATURE,
) -> dict:
    """Chamada que devolve JSON ja parseado.

    Usa response_format=json_object com raciocinio DESLIGADO de proposito: medido contra
    a API de producao, e a combinacao que respeita o formato e o idioma pedidos. Com
    raciocinio ligado o modelo entrega JSON valido mas as vezes deixa os campos no idioma
    original em vez de traduzir.
    """
    raw = await _complete(
        [{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
        thinking=THINK_OFF,
        json_mode=True,
    )
    return parse_json_payload(raw)


def parse_json_payload(raw: str) -> dict:
    """Parseia o JSON tolerando cerca de codigo e texto solto em volta.

    response_format ja garante JSON na quase totalidade das respostas; isso aqui e o
    cinto de seguranca pra nao perder a categoria inteira por causa de um ```json.
    """
    text = CODE_FENCE_RE.sub("", (raw or "").strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    inicio, fim = text.find("{"), text.rfind("}")
    if inicio == -1 or fim <= inicio:
        raise ValueError("resposta sem nenhum objeto JSON reconhecivel")
    return json.loads(text[inicio:fim + 1])


# Quantas vezes ela pode chamar ferramenta antes de ser obrigada a responder. 2 cobre
# "pesquisa e responde" e "pesquisa, ficou faltando algo, pesquisa de novo"; mais que
# isso vira espera longa demais pra quem esta olhando o "Pensando...".
MAX_TOOL_ROUNDS = 2


async def answer_with_tools(base_messages: list[dict]) -> tuple[str, list]:
    """Deixa a Solenne escolher ferramenta antes de responder.

    Modos de raciocinio medidos contra a API de producao, com resultado bem diferente:

    - 1a chamada (decidir + responder): ORCAMENTO. E dela que sai tanto a decisao quanto
      a resposta profunda quando nao ha ferramenta a usar - rebaixar aqui desfaz a
      correcao que tirou a Solenne de "bobinha". Com raciocinio desligado ela chamou
      busca web pra "diferenca entre TCP e UDP", conhecimento estavel que nao precisa.
    - 2a chamada (sintetizar o que a ferramenta trouxe): LOW_EFFORT. E so juntar dado
      que ja chegou, e foi exatamente onde o orcamento estourou e devolveu resposta
      vazia numa das sondas.

    Nessa combinacao: 10/10 decisoes corretas e nenhuma resposta vazia.
    """
    especificacoes = tools.tool_specs()
    if not especificacoes:
        return await _think_and_answer(base_messages), []

    mensagens = list(base_messages)
    embeds = []
    for rodada in range(MAX_TOOL_ROUNDS):
        primeira = rodada == 0
        choice = await _request(
            mensagens,
            max_tokens=REASONING_BUDGET + 1600 if primeira else 1600,
            thinking=THINK_BUDGET if primeira else THINK_LOW,
            tools=especificacoes,
        )
        chamadas = choice.message.tool_calls or []
        if not chamadas:
            # Truncada no meio do raciocinio: o que esta em content e trace, nao
            # resposta. Cai pro fechamento sem ferramentas em vez de entregar isso.
            if choice.finish_reason == "length":
                log.warning("Rodada %s truncada no max_tokens, refazendo sem ferramentas", rodada)
                break
            texto = clean_reply(choice.message.content or "")
            if texto:
                return texto, embeds
            break

        log.info("Solenne pediu: %s", [c.function.name for c in chamadas])
        mensagens.append(choice.message.model_dump(exclude_none=True))
        for chamada in chamadas:
            resultado = await tools.execute_tool(chamada.function.name, chamada.function.arguments)
            if resultado.embed is not None:
                embeds.append(resultado.embed)
            mensagens.append({
                "role": "tool",
                "tool_call_id": chamada.id,
                "content": resultado.content,
            })

    # Estourou as rodadas (ou veio vazio): fecha SEM ferramentas, pra ela ser obrigada a
    # responder com o que ja tem em vez de pedir mais uma busca pra sempre.
    log.info("Fechando a resposta sem ferramentas apos %s rodada(s)", MAX_TOOL_ROUNDS)
    try:
        texto = await _complete(mensagens, max_tokens=1600, thinking=THINK_LOW)
    except (EmptyAIResponse, TruncatedAIResponse) as exc:
        # Ultima linha de defesa: sem raciocinio o orcamento inteiro e da resposta.
        log.warning("Fechamento falhou (%s), refazendo sem raciocinio", exc)
        texto = await _complete(mensagens, max_tokens=1600, thinking=THINK_OFF)
    return texto, embeds


async def _think_and_answer(base_messages: list[dict]) -> str:
    """Resposta de chat: UMA chamada com raciocinio de verdade.

    O pipeline antigo era rascunho -> N criticas -> humanizacao (3+ chamadas, medidas em
    ~66s por resposta), uma imitacao artesanal de raciocinio feita quando o modelo nao
    raciocinava. Com o raciocinio nativo ligado, a mesma pergunta sai em ~20-45s E com
    resposta melhor. As passadas extras continuam disponiveis por env (REFINEMENT_ROUNDS
    e HUMANIZE_PASS) pra quem quiser trocar tempo por polimento.
    """
    # max_tokens precisa caber raciocinio + resposta: o orcamento de raciocinio e um
    # teto flexivel (o modelo fecha o trace no proximo fim de linha), entao sobra folga.
    max_tokens = REASONING_BUDGET + 1400
    try:
        draft = await _complete(
            base_messages, max_tokens=max_tokens, thinking=THINK_BUDGET
        )
    except (EmptyAIResponse, TruncatedAIResponse):
        # Raciocinio comeu o orcamento inteiro mesmo com a folga: responde sem raciocinar
        # em vez de deixar a pessoa sem resposta nenhuma (ou pior, de vazar o trace).
        log.warning("Raciocinio estourou o orcamento, refazendo a resposta sem raciocinio")
        draft = await _complete(base_messages, max_tokens=1000, thinking=THINK_OFF)

    for _ in range(REFINEMENT_ROUNDS):
        refine_messages = base_messages + [
            {"role": "assistant", "content": draft},
            {"role": "user", "content": CRITIQUE_PROMPT},
        ]
        try:
            draft = await _complete(refine_messages, max_tokens=max_tokens, thinking=THINK_BUDGET)
        except (EmptyAIResponse, TruncatedAIResponse) as exc:
            # O refino e polimento, nao a resposta. Se ele falhar, o rascunho anterior ja
            # servia - trocar por trace truncado seria piorar de proposito.
            log.warning("Rodada de refino descartada (%s), mantendo o rascunho", exc)
            break

    if not HUMANIZE_PASS:
        return draft

    final_messages = base_messages + [
        {"role": "assistant", "content": draft},
        {"role": "user", "content": HUMANIZE_PROMPT},
    ]
    try:
        return await _complete(final_messages, max_tokens=1200, thinking=THINK_OFF)
    except (EmptyAIResponse, TruncatedAIResponse) as exc:
        log.warning("Passada de humanizacao descartada (%s), mantendo o rascunho", exc)
        return draft
