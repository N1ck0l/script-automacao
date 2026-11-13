#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes para métricas do dashboard, configuração, trava multi-caminho e cache."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

import prospeccao_whatsapp as pw
from prospeccao.config import carregar_config
from prospeccao.metricas import (
    aplicar_filtros,
    calcular_kpis,
    extrair_nota,
    extrair_qtd_avaliacoes,
    link_whatsapp,
    preparar_dataframe,
)
from prospeccao.trava import TravaEnvio


class TestMetricasDashboard(unittest.TestCase):
    def test_extratores_e_link(self):
        self.assertEqual(extrair_nota("4.8 estrelas (120 avaliações)"), 4.8)
        self.assertTrue(pd.isna(extrair_nota("sem nota")))
        self.assertEqual(extrair_qtd_avaliacoes("4.8 estrelas (120 avaliações)"), 120.0)
        self.assertTrue(pd.isna(extrair_qtd_avaliacoes("4.8 estrelas")))
        self.assertEqual(link_whatsapp("(79) 99963-1871"), "https://wa.me/5579999631871")
        self.assertEqual(link_whatsapp("5579999631871"), "https://wa.me/5579999631871")
        self.assertEqual(link_whatsapp(""), "")

    def test_taxa_nao_mistura_optout_e_nao_passa_de_100(self):
        df = pd.DataFrame([
            {"clinica": "A", "status": "enviado", "resposta": "respondeu",
             "ultimo_envio": "2026-11-10T10:00:00", "telefone": "5579999990001"},
            {"clinica": "B", "status": "nao_contatar", "resposta": "sem interesse",
             "ultimo_envio": "2026-11-10T10:05:00", "telefone": "5579999990002"},
            {"clinica": "C", "status": "nao_contatar", "resposta": "sem interesse",
             "ultimo_envio": "2026-11-10T10:10:00", "telefone": "5579999990003"},
            {"clinica": "D", "status": "pendente", "resposta": "",
             "ultimo_envio": "", "telefone": "5579999990004"},
        ])
        prep = preparar_dataframe(df, agora=datetime(2026, 11, 13, 12, 0))
        kpis = calcular_kpis(prep)
        self.assertEqual(len(kpis["reais"]), 1)
        self.assertEqual(len(kpis["opt_outs"]), 2)
        self.assertEqual(len(kpis["contatados"]), 3)
        self.assertAlmostEqual(kpis["taxa"], 1 / 3 * 100, places=2)

    def test_followup_pendente_usa_dias_uteis(self):
        # 2026-11-10 (terça) -> 2026-11-12 (quinta) = 2 dias úteis -> elegível
        df = pd.DataFrame([
            {"clinica": "A", "status": "enviado", "resposta": "aguardando",
             "ultimo_envio": "2026-11-10T10:00:00", "telefone": "5579999990001"},
            {"clinica": "B", "status": "enviado", "resposta": "resposta automatica",
             "ultimo_envio": "2026-11-11T10:00:00", "telefone": "5579999990002"},
            {"clinica": "C", "status": "nao_contatar", "resposta": "aguardando",
             "ultimo_envio": "2026-11-05T10:00:00", "telefone": "5579999990003"},
        ])
        prep = preparar_dataframe(df, agora=datetime(2026, 11, 12, 14, 0))
        self.assertEqual(list(prep["followup_pendente"]), [True, False, False])

    def test_aplicar_filtros_basico(self):
        df = preparar_dataframe(pd.DataFrame([
            {"clinica": "Odonto Prime", "segmento": "odonto", "status": "pendente",
             "resposta": "", "bairro": "Jardins", "avaliacoes": "4.9 estrelas",
             "telefone": "5579999990001"},
            {"clinica": "Med Center", "segmento": "medico", "status": "enviado",
             "resposta": "respondeu", "bairro": "", "avaliacoes": "",
             "telefone": "5579999990002"},
        ]))
        res = aplicar_filtros(df, {
            "segmento": ["odonto"],
            "status": ["pendente"],
            "resposta": ["sem resposta"],
            "bairro": ["Jardins"],
            "busca": "prime",
            "nota_min": 4.5,
        })
        self.assertEqual(len(res), 1)
        self.assertEqual(res.iloc[0]["clinica"], "Odonto Prime")


class TestMelhoriasNucleo(unittest.TestCase):
    def test_trava_caminhos_diferentes_independentes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "a.lock"
            p2 = Path(tmp) / "b.lock"
            with TravaEnvio(p1):
                with TravaEnvio(p2):
                    self.assertTrue(p1.exists())
                    self.assertTrue(p2.exists())
                self.assertTrue(p1.exists())
                self.assertFalse(p2.exists())
            self.assertFalse(p1.exists())

    def test_carregar_config_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.local.json").write_text(
                json.dumps({"cidade_padrao": "Salvador"}), encoding="utf-8"
            )
            cfg = carregar_config(base)
            self.assertEqual(cfg["cidade_padrao"], "Salvador")

    def test_cache_historico_invalida_ao_gravar(self):
        orig = pw.ARQUIVO_LOG
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pw.ARQUIVO_LOG = Path(tmp) / "hist.jsonl"
                self.assertEqual(pw.ler_historico(), [])
                contato = {"clinica": "C1", "bairro": "Centro", "telefone": "5579999990001"}
                pw.atualizar_historico(contato, "Oi", "manual")
                self.assertEqual(len(pw.ler_historico()), 1)
                pw.atualizar_historico(contato, "Oi 2", "manual")
                self.assertEqual(len(pw.ler_historico()), 2)
        finally:
            pw.ARQUIVO_LOG = orig

    def test_demonstracao_html_personaliza_clinica_e_segmento(self):
        msgs = {"demonstracao": {"html": "https://n1ck0l.github.io/script-automacao/"}}
        contato = {
            "clinica": "Clínica Vida - Cardiologia",
            "bairro": "Centro",
            "cidade": "Aracaju",
            "segmento": "medico",
        }
        url = pw.demonstracao_html(msgs, contato)
        self.assertIn("https://n1ck0l.github.io/script-automacao/?", url)
        self.assertIn("clinica=Cl%C3%ADnica+Vida", url)
        self.assertIn("seg=medico", url)
        self.assertEqual(pw.demonstracao_html(msgs), "https://n1ck0l.github.io/script-automacao/")

    def test_score_lead_prioriza_celular_e_volume_avaliacoes(self):
        fixo_movimentado = {
            "telefone": "557932145678",
            "avaliacoes": "4.9 estrelas (300 avaliações)",
            "site": "https://a.com",
        }
        cel_parado = {
            "telefone": "5579999990001",
            "avaliacoes": "4.2 estrelas (5 avaliações)",
            "site": "",
        }
        cel_movimentado = {
            "telefone": "5579999990002",
            "avaliacoes": "4.8 estrelas (200 avaliações)",
            "site": "https://b.com",
        }
        self.assertGreater(pw.score_lead(cel_parado), pw.score_lead(fixo_movimentado))
        self.assertGreater(pw.score_lead(cel_movimentado), pw.score_lead(cel_parado))

    def test_kpis_contabilizam_estagio_comercial(self):
        df = pd.DataFrame([
            {"clinica": "A", "status": "enviado", "resposta": "respondeu",
             "estagio": "reuniao_agendada", "telefone": "5579999990001"},
            {"clinica": "B", "status": "enviado", "resposta": "respondeu",
             "estagio": "fechado", "telefone": "5579999990002"},
        ])
        kpis = calcular_kpis(preparar_dataframe(df))
        self.assertEqual(len(kpis["reunioes"]), 1)
        self.assertEqual(len(kpis["propostas"]), 0)
        self.assertEqual(len(kpis["fechados"]), 1)

    def test_classificador_inteligente_para_outros_robos_e_acentos(self):
        # Menu com colchetes / fallback administrativo / sem acento
        self.assertEqual(pw.opcao_atendente(["[4] Outros assuntos"]), "4")
        self.assertEqual(pw.opcao_atendente(["Opcao 6 - Falar na recepcao"]), "6")
        # Robô pedindo nome ou assunto
        self.assertEqual(
            pw.resposta_para_robo(["Ola! Qual o seu nome para iniciarmos?"]),
            "Nickolas",
        )
        self.assertIn(
            "administrativo",
            pw.resposta_para_robo(["Como posso ajudar voce hoje?"]).lower(),
        )
        # Negativa e opt-out sem acento
        self.assertTrue(pw.resposta_negativa(["Agora nao, ja contratamos outra empresa"]))
        bolhas = [
            {"remetente": "Eu", "quando": datetime(2026, 11, 13, 10, 0), "texto": "Oi"},
            {"remetente": "Clinica", "quando": datetime(2026, 11, 13, 10, 10),
             "texto": "Nao nos interessa, pare de mandar"},
        ]
        self.assertEqual(
            pw.classificar_resposta(bolhas, ["Oi"], datetime(2026, 11, 13, 10, 0)),
            "sem interesse",
        )


if __name__ == "__main__":
    unittest.main()

