"""Lógica pura de métricas, filtros e preparação de dados para o Dashboard."""

from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from .calendario import dias_uteis_entre

STATUS_ORDEM = ["pendente", "enviado", "nao_contatar"]
STATUS_CORES = {
    "pendente": "#94a3b8",
    "enviado": "#f59e0b",
    "nao_contatar": "#ef4444",
}
RESPOSTAS_EM_ABERTO = {"", "aguardando", "sem resposta", "resposta automatica"}


def extrair_nota(texto: str) -> float:
    match = re.search(r"(\d+[.,]\d+)", texto or "")
    if not match:
        return float("nan")
    return float(match.group(1).replace(",", "."))


def extrair_qtd_avaliacoes(texto: str) -> float:
    match = re.search(r"\((\d+)\s*avalia", texto or "")
    if not match:
        return float("nan")
    return float(match.group(1))


def link_whatsapp(telefone: str) -> str:
    digitos = re.sub(r"\D", "", telefone or "")
    if not digitos:
        return ""
    if digitos.startswith("55") and len(digitos) > 11:
        return f"https://wa.me/{digitos}"
    if len(digitos) <= 11:
        return f"https://wa.me/55{digitos}"
    return f"https://wa.me/{digitos}"


def preparar_dataframe(df: pd.DataFrame, agora: datetime | None = None) -> pd.DataFrame:
    """Normaliza colunas, tipos, KPIs de resposta e elegibilidade a follow-up."""
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    for coluna in ("clinica", "bairro", "cidade", "telefone", "site",
                   "avaliacoes", "segmento", "status", "resposta", "variante",
                   "etapa", "menu", "versao_textos", "estagio",
                   "ultimo_envio", "atualizado_em"):
        if coluna not in df.columns:
            df[coluna] = ""
        df[coluna] = df[coluna].fillna("").astype(str).str.strip()

    df["nota"] = df["avaliacoes"].map(extrair_nota)
    df["qtd_avaliacoes"] = df["avaliacoes"].map(extrair_qtd_avaliacoes)
    df["ultimo_envio_dt"] = pd.to_datetime(
        df["ultimo_envio"], errors="coerce", format="mixed"
    )
    df["atualizado_em_dt"] = pd.to_datetime(
        df["atualizado_em"], errors="coerce", format="mixed"
    )
    df["resposta_bruta"] = df["resposta"]
    df["resposta"] = df["resposta"].replace({"": "sem resposta"})
    df["status"] = df["status"].replace({"": "desconhecido"})
    df["whatsapp"] = df["telefone"].map(link_whatsapp)
    # Apenas respostas humanas contam como "respondeu"; opt-out é contabilizado à parte.
    df["respondeu"] = df["resposta"] == "respondeu"
    df["opt_out"] = (df["resposta"] == "sem interesse") | (
        df["status"] == "nao_contatar"
    )

    ref = agora or datetime.now()
    hoje = pd.Timestamp(ref.date())
    df["dias_sem_resposta"] = (hoje - df["ultimo_envio_dt"]).dt.days
    df["dias_uteis_sem_resposta"] = df["ultimo_envio_dt"].map(
        lambda dt: dias_uteis_entre(dt.to_pydatetime(), ref) if pd.notna(dt) else 0
    )
    df["followup_pendente"] = (
        (df["status"] == "enviado")
        & df["resposta_bruta"].isin(RESPOSTAS_EM_ABERTO)
        & df["ultimo_envio_dt"].notna()
        & (df["dias_uteis_sem_resposta"] >= 2)
    )
    return df


def aplicar_filtros(df: pd.DataFrame, f: dict) -> pd.DataFrame:
    out = df
    segs_sel = [s for s in f["segmento"] if s != "(sem segmento)"]
    mascara_seg = out["segmento"].isin(segs_sel)
    if "(sem segmento)" in f["segmento"]:
        mascara_seg |= out["segmento"] == ""
    out = out[mascara_seg]
    out = out[out["status"].isin(f["status"])]
    out = out[out["resposta"].isin(f["resposta"])]
    bairros_sel = [b for b in f["bairro"] if b != "(sem bairro)"]
    mascara_bairro = out["bairro"].isin(bairros_sel)
    if "(sem bairro)" in f["bairro"]:
        mascara_bairro |= out["bairro"] == ""
    out = out[mascara_bairro]
    if f["busca"]:
        mascara = (
            out["clinica"].str.contains(f["busca"], case=False, na=False)
            | out["telefone"].str.contains(f["busca"], na=False)
        )
        out = out[mascara]
    if f["nota_min"] > 0:
        out = out[out["nota"] >= f["nota_min"]]
    return out


def calcular_kpis(filtrado: pd.DataFrame) -> dict:
    """Calcula KPIs consistentes (taxa de resposta sobre todos os contatados)."""
    enviados = filtrado[filtrado["status"] == "enviado"]
    contatados = filtrado[filtrado["status"].isin(["enviado", "nao_contatar"])]
    reais = filtrado[filtrado["respondeu"]]
    opt_outs = filtrado[filtrado["opt_out"]]
    fila = filtrado[filtrado["status"] == "pendente"]
    followups = filtrado[filtrado["followup_pendente"]]
    reunioes = filtrado[filtrado["estagio"] == "reuniao_agendada"]
    propostas = filtrado[filtrado["estagio"] == "proposta_enviada"]
    fechados = filtrado[filtrado["estagio"] == "fechado"]
    taxa = (len(reais) / len(contatados) * 100) if len(contatados) else 0.0
    return {
        "enviados": enviados,
        "contatados": contatados,
        "reais": reais,
        "opt_outs": opt_outs,
        "fila": fila,
        "followups": followups,
        "reunioes": reunioes,
        "propostas": propostas,
        "fechados": fechados,
        "taxa": taxa,
    }

