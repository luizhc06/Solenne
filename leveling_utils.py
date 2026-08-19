"""Formula de nivel a partir de XP - modulo compartilhado entre db.py (que grava o XP
e precisa calcular o nivel novo dentro da mesma transacao) e cogs/leveling.py (que
mostra o progresso em /rank). Funcoes puras de proposito, testaveis sem SQLite nem
Discord (mesmo padrao de parse_when em cogs/reminders.py).

Curva quadratica simples: XP total pro nivel N = LEVEL_XP_BASE * N**2. E so um patamar
inicial (mesmo espirito do comentario sobre AI_CONCURRENCY_LIMIT em config.py) -
ajustavel depois de observar o volume real de mensagens do servidor.
"""

LEVEL_XP_BASE = 100


def xp_for_level(level: int) -> int:
    """XP total acumulado necessario pra alcancar o nivel dado (nivel 0 = 0 XP)."""
    if level <= 0:
        return 0
    return LEVEL_XP_BASE * level ** 2


def level_for_xp(xp: int) -> int:
    """Nivel atual dado o XP total acumulado - inverso de xp_for_level."""
    if xp <= 0:
        return 0
    return int((xp / LEVEL_XP_BASE) ** 0.5)
