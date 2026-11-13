"""Regras de horário comercial, feriados (nacionais + locais) e dias úteis."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import carregar_config

FERIADOS_NACIONAIS = {
    (1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20),
    (12, 25),            # nacionais
    (12, 24), (12, 31),  # vésperas de Natal e Ano Novo
}


def _carregar_feriados_fixos() -> set[tuple[int, int]]:
    cfg = carregar_config()
    locais = {
        (int(par[0]), int(par[1]))
        for par in (cfg.get("feriados_locais") or [])
        if isinstance(par, (list, tuple)) and len(par) == 2
    }
    return FERIADOS_NACIONAIS | locais


FERIADOS_FIXOS = _carregar_feriados_fixos()


def dentro_da_janela(inicio: int, fim: int, agora: datetime | None = None) -> bool:
    """True se 'agora' estiver dentro da janela de horário comercial."""
    hora = (agora or datetime.now()).hour
    return inicio <= hora < fim


def _pascoa(ano: int) -> date:
    """Domingo de Páscoa (algoritmo de Meeus/Jones/Butcher)."""
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    mes = (h + l_ - 7 * m + 114) // 31
    dia = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(ano, mes, dia)


def eh_feriado(dia) -> bool:
    """Feriado fixo ou móvel (Carnaval, Quarta de Cinzas, Sexta Santa, Corpus Christi)."""
    dia = dia.date() if isinstance(dia, datetime) else dia
    if (dia.month, dia.day) in FERIADOS_FIXOS:
        return True
    pascoa = _pascoa(dia.year)
    moveis = {pascoa + timedelta(days=n) for n in (-48, -47, -46, -2, 60)}
    return dia in moveis


def dia_util(agora: datetime | None = None) -> bool:
    """Segunda a sexta, exceto feriados. Fora disso, prospecção parece automação."""
    agora = agora or datetime.now()
    return agora.weekday() < 5 and not eh_feriado(agora)


def dias_uteis_entre(inicio: datetime, fim: datetime) -> int:
    """Dias úteis entre `inicio` e `fim` (exclui fim de semana e feriados)."""
    a, b = inicio.date(), fim.date()
    if b <= a:
        return 0
    return sum(1 for n in range(1, (b - a).days + 1)
               if (d := a + timedelta(days=n)).weekday() < 5 and not eh_feriado(d))

