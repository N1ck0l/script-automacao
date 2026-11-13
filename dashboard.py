from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from prospeccao.metricas import (
    STATUS_CORES,
    STATUS_ORDEM,
    aplicar_filtros,
    calcular_kpis,
    preparar_dataframe,
)

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "prospeccao.csv"

st.set_page_config(
    page_title="Dashboard de Prospecção",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(show_spinner=False)
def carregar_dados(caminho: str, mtime: float) -> pd.DataFrame:
    df = pd.read_csv(caminho, dtype=str, keep_default_na=False)
    return preparar_dataframe(df)


@st.cache_data(show_spinner=False)
def mtime_csv(caminho: str) -> float:
    return Path(caminho).stat().st_mtime


def kpi(coluna, rotulo: str, valor, ajuda: str | None = None) -> None:
    coluna.metric(rotulo, valor, help=ajuda)


def main() -> None:
    if not CSV_PATH.exists():
        st.error("prospeccao.csv não encontrado.")
        return

    df = carregar_dados(str(CSV_PATH), mtime_csv(str(CSV_PATH)))

    st.title("📊 Dashboard de Prospecção — Clínicas")
    st.caption(
        f"Fonte: `prospeccao.csv` • atualizado em "
        f"{datetime.fromtimestamp(mtime_csv(str(CSV_PATH))):%d/%m/%Y %H:%M} • "
        f"{len(df)} registros"
    )

    with st.sidebar:
        st.header("Filtros")
        busca = st.text_input("Buscar clínica ou telefone", "")
        segmentos = sorted(s for s in df["segmento"].unique() if s)
        opcoes_segmento = segmentos + ["(sem segmento)"]
        segmento_sel = st.multiselect(
            "Segmento", opcoes_segmento, default=opcoes_segmento,
        )
        status_sel = st.multiselect(
            "Status",
            sorted(df["status"].unique()),
            default=sorted(df["status"].unique()),
        )
        resposta_sel = st.multiselect(
            "Resposta",
            sorted(df["resposta"].unique()),
            default=sorted(df["resposta"].unique()),
        )
        bairros = sorted(b for b in df["bairro"].unique() if b)
        opcoes_bairro = bairros + ["(sem bairro)"]
        bairro_sel = st.multiselect(
            "Bairro", opcoes_bairro, default=opcoes_bairro,
        )
        nota_min = st.slider(
            "Nota mínima (Google)", 0.0, 5.0, 0.0, 0.1,
            help="0 = sem filtro. Registros sem nota ficam de fora quando > 0.",
        )
        st.divider()
        if st.button("🔄 Recarregar dados", width="stretch"):
            st.cache_data.clear()
            st.rerun()

    filtrado = aplicar_filtros(
        df,
        {
            "segmento": segmento_sel,
            "status": status_sel,
            "resposta": resposta_sel,
            "bairro": bairro_sel,
            "busca": busca.strip(),
            "nota_min": nota_min,
        },
    )

    if filtrado.empty:
        st.warning("Nenhum registro com os filtros atuais.")
        return

    kpis = calcular_kpis(filtrado)
    enviados = kpis["enviados"]
    reais = kpis["reais"]
    opt_outs = kpis["opt_outs"]
    fila = kpis["fila"]
    followups = kpis["followups"]
    taxa = kpis["taxa"]

    tab_geral, tab_analise, tab_tabela = st.tabs(
        ["📈 Visão geral", "🔍 Análise", "📋 Contatos"]
    )

    with tab_geral:
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        kpi(c1, "Na base", len(filtrado))
        kpi(c2, "Fila (pendente)", len(fila))
        kpi(c3, "Enviados", len(enviados))
        kpi(c4, "Responderam", len(reais))
        kpi(
            c5,
            "Taxa de resposta",
            f"{taxa:.1f}%",
            ajuda="Respostas humanas ÷ enviados (ignora autorespostas)",
        )
        kpi(c6, "Opt-outs", len(opt_outs))

        st.divider()
        g1, g2 = st.columns([1, 1])

        with g1:
            st.subheader("Funil")
            funil = pd.DataFrame(
                {
                    "Etapa": ["Na base", "Enviados", "Responderam", "Opt-out"],
                    "Contatos": [
                        len(filtrado), len(enviados), len(reais), len(opt_outs),
                    ],
                }
            )
            fig_funil = px.funnel(
                funil, x="Contatos", y="Etapa",
                color_discrete_sequence=["#2563eb", "#f59e0b", "#16a34a", "#ef4444"],
            )
            fig_funil.update_layout(
                template="plotly_white", height=380, margin=dict(t=10, b=10),
                showlegend=False,
            )
            st.plotly_chart(fig_funil, width="stretch")

        with g2:
            st.subheader("Envios por dia")
            tmp_dia = enviados.dropna(subset=["ultimo_envio_dt"]).copy()
            if tmp_dia.empty:
                st.info("Sem envios registrados com os filtros atuais.")
            else:
                por_dia = (
                    tmp_dia.groupby(tmp_dia["ultimo_envio_dt"].dt.date)
                    .size()
                    .reset_index(name="envios")
                )
                por_dia.columns = ["data", "envios"]
                fig_dia = px.bar(
                    por_dia, x="data", y="envios",
                    color_discrete_sequence=["#2563eb"],
                )
                fig_dia.update_layout(
                    template="plotly_white", height=380,
                    margin=dict(t=10, b=10), showlegend=False,
                )
                st.plotly_chart(fig_dia, width="stretch")

        st.subheader(f"⏱ Follow-ups pendentes ({len(followups)})")
        st.caption(
            "Sem resposta há 2+ dias úteis — candidatos a retomar a conversa."
        )
        if followups.empty:
            st.success("Nenhum follow-up pendente.")
        else:
            st.dataframe(
                followups[
                    ["clinica", "bairro", "dias_sem_resposta", "telefone",
                     "etapa", "variante"]
                ].sort_values("dias_sem_resposta", ascending=False),
                width="stretch",
                hide_index=True,
                column_config={
                    "dias_sem_resposta": st.column_config.NumberColumn(
                        "Dias sem resposta", format="%d"
                    ),
                },
            )

    with tab_analise:
        r1, r2 = st.columns(2)

        with r1:
            st.subheader("Status")
            cont_status = (
                filtrado["status"]
                .value_counts()
                .reindex(STATUS_ORDEM + [
                    s for s in filtrado["status"].unique()
                    if s not in STATUS_ORDEM
                ])
                .dropna()
                .reset_index()
            )
            cont_status.columns = ["status", "qtd"]
            fig_status = px.pie(
                cont_status, names="status", values="qtd", hole=0.45,
                color="status",
                color_discrete_map=STATUS_CORES,
            )
            fig_status.update_layout(
                template="plotly_white", height=360, margin=dict(t=10, b=10),
            )
            st.plotly_chart(fig_status, width="stretch")

        with r2:
            st.subheader("Respostas (entre enviados)")
            cont_resp = (
                enviados["resposta"].value_counts().reset_index()
            )
            cont_resp.columns = ["resposta", "qtd"]
            if cont_resp.empty:
                st.info("Sem envios com os filtros atuais.")
            else:
                fig_resp = px.bar(
                    cont_resp, x="qtd", y="resposta", orientation="h",
                    color_discrete_sequence=["#2563eb"],
                )
                fig_resp.update_layout(
                    template="plotly_white", height=360,
                    margin=dict(t=10, b=10), showlegend=False,
                    yaxis={"categoryorder": "total ascending"},
                )
                st.plotly_chart(fig_resp, width="stretch")

        r3, r4 = st.columns(2)

        with r3:
            st.subheader("Top bairros")
            bairro_status = (
                filtrado[filtrado["bairro"] != ""]
                .groupby(["bairro", "status"])
                .size()
                .reset_index(name="qtd")
            )
            if bairro_status.empty:
                st.info("Sem bairros para exibir.")
            else:
                top = (
                    filtrado[filtrado["bairro"] != ""]
                    .groupby("bairro").size().nlargest(10).index
                )
                fig_bairro = px.bar(
                    bairro_status[bairro_status["bairro"].isin(top)],
                    x="qtd", y="bairro", color="status", orientation="h",
                    color_discrete_map=STATUS_CORES,
                )
                fig_bairro.update_layout(
                    template="plotly_white", height=420,
                    margin=dict(t=10, b=10),
                    yaxis={"categoryorder": "total ascending"},
                )
                st.plotly_chart(fig_bairro, width="stretch")

        with r4:
            st.subheader("Nota no Google")
            com_nota = filtrado.dropna(subset=["nota"])
            if com_nota.empty:
                st.info("Sem notas disponíveis.")
            else:
                fig_nota = px.histogram(
                    com_nota, x="nota", nbins=20,
                    color_discrete_sequence=["#2563eb"],
                )
                fig_nota.update_layout(
                    template="plotly_white", height=420,
                    margin=dict(t=10, b=10), showlegend=False,
                    xaxis={"dtick": 0.5},
                )
                st.plotly_chart(fig_nota, width="stretch")
                m1, m2, m3 = st.columns(3)
                m1.metric("Nota média", f"{com_nota['nota'].mean():.2f}")
                m2.metric("Mediana", f"{com_nota['nota'].median():.2f}")
                m3.metric("Com nota", f"{len(com_nota)}/{len(filtrado)}")

        r5, r6 = st.columns(2)

        with r5:
            st.subheader("Textos usados (etapa)")
            etapas = enviados["etapa"].replace({"": "não informado"})
            cont_etapa = etapas.value_counts().reset_index()
            cont_etapa.columns = ["etapa", "qtd"]
            fig_etapa = px.bar(
                cont_etapa, x="qtd", y="etapa", orientation="h",
                color_discrete_sequence=["#7c3aed"],
            )
            fig_etapa.update_layout(
                template="plotly_white", height=320,
                margin=dict(t=10, b=10), showlegend=False,
            )
            st.plotly_chart(fig_etapa, width="stretch")

        with r6:
            st.subheader("Variantes (spintax)")
            variantes = enviados["variante"].replace({"": "não informada"})
            cont_var = variantes.value_counts().reset_index()
            cont_var.columns = ["variante", "qtd"]
            fig_var = px.bar(
                cont_var, x="qtd", y="variante", orientation="h",
                color_discrete_sequence=["#0891b2"],
            )
            fig_var.update_layout(
                template="plotly_white", height=320,
                margin=dict(t=10, b=10), showlegend=False,
            )
            st.plotly_chart(fig_var, width="stretch")

        st.subheader("Funil comercial (estágio pós-resposta)")
        estagios = filtrado[filtrado["estagio"] != ""]["estagio"]
        if estagios.empty:
            st.info("Nenhum estágio comercial registrado ainda (use --respostas).")
        else:
            cont_est = estagios.value_counts().reset_index()
            cont_est.columns = ["estagio", "qtd"]
            fig_est = px.bar(
                cont_est, x="estagio", y="qtd",
                color_discrete_sequence=["#16a34a"],
            )
            fig_est.update_layout(
                template="plotly_white", height=300,
                margin=dict(t=10, b=10), showlegend=False,
            )
            st.plotly_chart(fig_est, width="stretch")

    with tab_tabela:
        st.subheader(f"Contatos ({len(filtrado)})")
        st.caption("Clique em WhatsApp/site para abrir direto no navegador.")
        tabela = filtrado.copy()
        tabela["nota_fmt"] = tabela["nota"].map(
            lambda v: f"{v:.1f}" if pd.notna(v) else "—"
        )
        tabela["avaliacoes_qtd"] = tabela["qtd_avaliacoes"].map(
            lambda v: int(v) if pd.notna(v) else None
        )
        exibir = tabela[
            [
                "clinica", "segmento", "bairro", "nota_fmt", "avaliacoes_qtd",
                "whatsapp", "site", "status", "resposta", "estagio", "etapa",
                "variante", "followups", "ultimo_envio",
            ]
        ].rename(
            columns={
                "clinica": "Clínica",
                "segmento": "Segmento",
                "bairro": "Bairro",
                "nota_fmt": "Nota",
                "avaliacoes_qtd": "Nº avaliações",
                "whatsapp": "WhatsApp",
                "site": "Site",
                "status": "Status",
                "resposta": "Resposta",
                "estagio": "Estágio",
                "etapa": "Etapa",
                "variante": "Variante",
                "followups": "Follow-ups",
                "ultimo_envio": "Último envio",
            }
        )
        st.dataframe(
            exibir,
            width="stretch",
            hide_index=True,
            height=560,
            column_config={
                "WhatsApp": st.column_config.LinkColumn(
                    "WhatsApp", display_text="abrir",
                ),
                "Site": st.column_config.LinkColumn(
                    "Site", display_text="abrir",
                ),
                "Nota": st.column_config.TextColumn("Nota"),
            },
        )
        st.download_button(
            "⬇️ Baixar CSV filtrado",
            data=filtrado.to_csv(index=False).encode("utf-8"),
            file_name="prospeccao_filtrado.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
