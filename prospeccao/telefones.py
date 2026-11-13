"""Normalização, validação de telefones brasileiros e inferência de segmento."""

from __future__ import annotations

import re
import unicodedata


def normalizar_telefone(tel: str) -> str:
    """Remove formatação e garante DDI 55. Ex: (79) 99963-1871 -> 5579999631871.

    Também remove o prefixo de trunk nacional ("0" + DDD), comum nas listagens
    do Google Maps: 079996746397 -> 5579996746397.
    """
    digitos = re.sub(r"\D", "", tel)
    if digitos.startswith("0") and len(digitos) in (11, 12):
        digitos = digitos[1:]  # remove o 0 de acesso (trunk) antes do DDD
    if len(digitos) <= 11 and not digitos.startswith("55"):  # número local
        digitos = "55" + digitos
    return digitos


def validar_telefone(digitos: str) -> bool:
    """Válido = DDI 55 + DDD + número fixo (8) ou celular (9) => 12 ou 13 dígitos."""
    return bool(re.fullmatch(r"55\d{10,11}", digitos))


def eh_celular(digitos: str) -> bool:
    """Celular = 55 + DDD + 9 + 8 dígitos. Fixos raramente têm WhatsApp."""
    return bool(re.fullmatch(r"55\d{2}9\d{8}", digitos))


_SINAIS_ODONTO = (
    "odontolog", "odont", "dentista", "dental", "dente", "ortodont",
    "orthos", "implante dent", "implantodont", "endodont", "periodont",
    "protese", "clareamento", "aparelho", "oral", "siso", "sorriso",
    "sorrir", "sorria", "sorridents",
)
_SINAIS_MEDICO = (
    "medic", "consultori", "consultorio", "hospital", "maternidade",
    "fisioterapia", "dermatolog", "psicolog", "pediatr", "oftalmolog",
    "nutri", "saude", "laborat", "diagnostico", "exame", "endoscop",
    "ultrassom", "ginecolog", "cardio", "onco", "psiquiatr", "ortoped",
    "traumato", "urolog", "neurolog", "gastro", "endocrino",
    "fonoaudiolog", "cirurgia", "ambulatorio", "medicina",
)


def _sem_acentos(texto: str) -> str:
    decomposto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn")


def _escore_sinais(sinais: tuple[str, ...], baixo: str) -> int:
    """Quantos sinais batem; subpalavras de outro sinal casado não contam
    (evita "odont" somar junto de "odontolog")."""
    casados = [s for s in sinais if s in baixo]
    return sum(1 for s in casados
               if not any(s != outro and s in outro for outro in casados))


def inferir_segmento(texto: str) -> str:
    """Classifica o texto (nome da clínica ou termo de busca).

    Conta os sinais de cada segmento e vence o de maior escore (empate:
    odonto). Retorna "" quando não há nenhum sinal.
    """
    baixo = _sem_acentos(texto or "").lower()
    if not baixo:
        return ""
    odonto = _escore_sinais(_SINAIS_ODONTO, baixo)
    medico = _escore_sinais(_SINAIS_MEDICO, baixo)
    if odonto == 0 and medico == 0:
        return ""
    return "odonto" if odonto >= medico else "medico"

