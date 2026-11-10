#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do núcleo anti-ban: telefones, spintax, limite diário e janela.

Rode com a biblioteca padrão (sem dependências):
    python3 -m unittest test_prospeccao -v

Também são descobertos pelo pytest, caso preferir: pytest test_prospeccao.py
"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import prospeccao_whatsapp as pw


class BasePw(unittest.TestCase):
    """Salva/restaura os globais do módulo tocados pelos testes."""

    def setUp(self):
        self._log_original = pw.ARQUIVO_LOG
        self._csv_original = pw.CAMINHO_CSV_ATIVO
        self._contatos_original = pw.CONTATOS_GLOBAIS
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        pw.ARQUIVO_LOG = self._log_original
        pw.CAMINHO_CSV_ATIVO = self._csv_original
        pw.CONTATOS_GLOBAIS = self._contatos_original
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Telefone: normalização e validação
# ---------------------------------------------------------------------------

class TestTelefone(unittest.TestCase):
    def test_normalizar_adiciona_ddi(self):
        self.assertEqual(pw.normalizar_telefone("(79) 99963-1871"),
                         "5579999631871")

    def test_normalizar_preserva_ddi_ja_presente(self):
        self.assertEqual(pw.normalizar_telefone("55 79 99963-1871"),
                         "5579999631871")

    def test_normalizar_remove_formatacao(self):
        self.assertEqual(pw.normalizar_telefone("+55 (79) 99963-1871"),
                         "5579999631871")

    def test_normalizar_remove_prefixo_trunk_do_maps(self):
        # Google Maps armazena como "079996746397" (0 de acesso + DDD + nº)
        self.assertEqual(pw.normalizar_telefone("079996746397"),
                         "5579996746397")
        self.assertTrue(pw.validar_telefone(
            pw.normalizar_telefone("079996746397")))

    def test_normalizar_trunk_de_fixo(self):
        # 0 + DDD + 8 dígitos (fixo) = 11 dígitos
        self.assertEqual(pw.normalizar_telefone("07932111222"),
                         "557932111222")

    def test_validar_celular_ok(self):
        self.assertTrue(pw.validar_telefone("5579999631871"))  # 13 dígitos

    def test_validar_fixo_ok(self):
        self.assertTrue(pw.validar_telefone("557932111222"))  # 12 dígitos

    def test_validar_curto_rejeitado(self):
        self.assertFalse(pw.validar_telefone("55799996318"))  # incompleto

    def test_validar_sem_ddi_rejeitado(self):
        self.assertFalse(pw.validar_telefone("79999631871"))


# ---------------------------------------------------------------------------
# Carga do CSV: deduplicação e validação
# ---------------------------------------------------------------------------

class TestCargaCsv(BasePw):
    def test_deduplica_e_valida(self):
        csv = self.tmpdir / "prospeccao.csv"
        csv.write_text(
            "clinica,bairro,cidade,telefone,status,resposta,atualizado_em\n"
            "Clínica A,Bairro X,Aracaju,5579991111111,pendente,,\n"
            "Clínica A dup,Bairro X,Aracaju,5579991111111,pendente,,\n"  # duplicado
            "Clínica B,Bairro Y,Aracaju,12345,pendente,,\n"              # inválido
            "Clínica C,Bairro Z,Aracaju,5579992222222,pendente,,\n",
            encoding="utf-8",
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            contatos = pw.carregar_contatos(csv)

        self.assertEqual(len(contatos), 2)
        self.assertEqual(contatos[0]["clinica"], "Clínica A")
        self.assertEqual(contatos[1]["telefone"], "5579992222222")
        self.assertIn("duplicado", buffer.getvalue())
        self.assertIn("inválido", buffer.getvalue())

    def test_csv_antigo_sem_coluna_resposta(self):
        """CSV antigo (sem a coluna 'resposta') continua funcionando."""
        csv = self.tmpdir / "antigo.csv"
        csv.write_text(
            "clinica,bairro,cidade,telefone,status\n"
            "Clínica A,Bairro X,Aracaju,5579991111111,pendente\n",
            encoding="utf-8",
        )
        contatos = pw.carregar_contatos(csv)
        self.assertEqual(contatos[0]["resposta"], "")

    def test_usa_mock_se_arquivo_faltar(self):
        contatos = pw.carregar_contatos(self.tmpdir / "nao_existe.csv")
        self.assertTrue(contatos)
        self.assertTrue(all(c["status"] == "pendente" for c in contatos))


# ---------------------------------------------------------------------------
# Spintax e personalização
# ---------------------------------------------------------------------------

class TestSpintax(unittest.TestCase):
    def test_escolhe_alternativa(self):
        for _ in range(20):
            self.assertIn(pw.resolver_spintax("{a|b}", {}), {"a", "b"})

    def test_substitui_tokens_de_contexto(self):
        saida = pw.resolver_spintax("Clínica {clinica} no {bairro}",
                                    {"clinica": "Ferraz", "bairro": "São José"})
        self.assertEqual(saida, "Clínica Ferraz no São José")

    def test_preserva_token_desconhecido(self):
        self.assertEqual(pw.resolver_spintax("{desconhecido}", {}),
                         "{desconhecido}")


class TestMensagem(unittest.TestCase):
    CONTATO = {"clinica": "Ferraz Odontologia", "bairro": "São José",
               "cidade": "Aracaju"}

    def test_personalizada_sem_chaves_sobrando(self):
        for _ in range(30):
            texto = pw.montar_texto(self.CONTATO)
            self.assertIn("Ferraz Odontologia", texto)
            self.assertIn("São José", texto)
            self.assertIn("Aracaju", texto)
            self.assertNotIn("{", texto)
            self.assertNotIn("}", texto)
            self.assertIn("PROIMAGEM", texto)  # case real sempre presente

    def test_varia_entre_geracoes(self):
        gerados = {pw.montar_texto(self.CONTATO) for _ in range(50)}
        self.assertGreater(len(gerados), 1)  # anti-spam: nada idêntico

    def test_link_codifica_mensagem(self):
        link = pw.montar_link({"telefone": "5579999631871"},
                              "Olá, tudo bem?\nSegunda linha")
        self.assertTrue(link.startswith(
            "https://web.whatsapp.com/send?phone=5579999631871&text="))
        self.assertIn("%0A", link)   # quebra de linha codificada
        self.assertNotIn(" ", link)


# ---------------------------------------------------------------------------
# Controle de volume: limite diário e reenvio no mesmo número
# ---------------------------------------------------------------------------

class TestControleVolume(BasePw):
    def _gravar_log(self, telefones_e_datas):
        log = self.tmpdir / "historico.jsonl"
        linhas = [
            '{"timestamp": "%s", "telefone": "%s"}' % (ts, tel)
            for ts, tel in telefones_e_datas
        ]
        log.write_text("\n".join(linhas) + "\n", encoding="utf-8")
        pw.ARQUIVO_LOG = log
        return log

    def test_envios_hoje_conta_apenas_hoje(self):
        hoje = datetime.now().isoformat(timespec="seconds")
        self._gravar_log([("2000-01-01T10:00:00", "5579991111111"),
                          (hoje, "5579992222222")])
        self.assertEqual(pw.envios_hoje(), 1)

    def test_enviado_hoje_bloqueia_mesmo_numero_no_dia(self):
        hoje = datetime.now().isoformat(timespec="seconds")
        self._gravar_log([(hoje, "5579991111111")])
        self.assertIsNotNone(pw.enviado_hoje("5579991111111"))  # já recebeu
        self.assertIsNone(pw.enviado_hoje("5579999999999"))     # outro número

    def test_limite_diario_bloqueia_acima_do_maximo(self):
        hoje = datetime.now().isoformat(timespec="seconds")
        self._gravar_log([(hoje, f"557999000000{i}") for i in range(3)])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertFalse(pw.verificar_limite_diario(2))
            self.assertTrue(pw.verificar_limite_diario(5))
        self.assertIn("BLOQUEADO", buffer.getvalue())


# ---------------------------------------------------------------------------
# Janela de horário
# ---------------------------------------------------------------------------

class TestJanelaHorario(unittest.TestCase):
    def test_dentro_da_janela(self):
        self.assertTrue(pw.dentro_da_janela(8, 19, datetime(2026, 1, 1, 8, 0)))
        self.assertTrue(pw.dentro_da_janela(8, 19, datetime(2026, 1, 1, 18, 59)))

    def test_fora_da_janela(self):
        # fim é exclusivo: 19h em ponto já está fora
        self.assertFalse(pw.dentro_da_janela(8, 19, datetime(2026, 1, 1, 19, 0)))
        # madrugada é padrão de robô
        self.assertFalse(pw.dentro_da_janela(8, 19, datetime(2026, 1, 1, 3, 0)))


# ---------------------------------------------------------------------------
# Persistência: status + timestamp no CSV
# ---------------------------------------------------------------------------

class TestPersistencia(BasePw):
    def test_marcar_enviado_grava_status_e_timestamp(self):
        csv = self.tmpdir / "prospeccao.csv"
        contato = {"clinica": "Ferraz Odontologia", "bairro": "São José",
                   "cidade": "Aracaju", "telefone": "5579999631871",
                   "status": "pendente", "resposta": "", "atualizado_em": ""}
        pw.CAMINHO_CSV_ATIVO = csv
        pw.CONTATOS_GLOBAIS = [contato]

        pw.marcar_enviado(contato)

        conteudo = csv.read_text(encoding="utf-8")
        self.assertIn("enviado", conteudo)
        self.assertIn("aguardando", conteudo)
        self.assertTrue(contato["atualizado_em"])  # timestamp preenchido


if __name__ == "__main__":
    unittest.main(verbosity=2)
