#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do núcleo anti-ban: telefones, spintax, limite diário, janela,
intervalo entre envios, follow-up e opt-out.

Rode com a biblioteca padrão (sem dependências):
    python3 -m unittest test_prospeccao -v

Também são descobertos pelo pytest, caso preferir: pytest test_prospeccao.py
"""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

import prospeccao_whatsapp as pw


class BasePw(unittest.TestCase):
    """Isola o log de envios em um diretório temporário."""

    def setUp(self):
        self._log_original = pw.ARQUIVO_LOG
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        pw.ARQUIVO_LOG = self.tmpdir / "historico.jsonl"

    def tearDown(self):
        pw.ARQUIVO_LOG = self._log_original
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

    def test_eh_celular(self):
        self.assertTrue(pw.eh_celular("5579999631871"))
        self.assertFalse(pw.eh_celular("557932111222"))   # fixo
        self.assertFalse(pw.eh_celular("5579899631871"))  # não começa com 9


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

    def test_arquivo_ausente_nao_usa_mock(self):
        with redirect_stdout(io.StringIO()):
            contatos = pw.carregar_contatos(self.tmpdir / "nao_existe.csv")
        self.assertEqual(contatos, [])

    def test_csv_so_com_cabecalho_nao_usa_mock(self):
        csv = self.tmpdir / "vazio.csv"
        csv.write_text(",".join(pw.COLUNAS) + "\n", encoding="utf-8")
        self.assertEqual(pw.carregar_contatos(csv), [])

    def test_salvar_preserva_linhas_invalidas_e_duplicadas(self):
        csv = self.tmpdir / "prospeccao.csv"
        csv.write_text(
            "clinica,bairro,cidade,telefone,status,minha_coluna\n"
            "Clínica A,X,Aracaju,5579991111111,pendente,nota A\n"
            "Clínica A dup,X,Aracaju,5579991111111,pendente,\n"
            "Clínica B,Y,Aracaju,12345,pendente,\n"
            "Clínica C,Z,,(79) 99222-2222,,\n",
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            planilha = pw.carregar_planilha(csv)
        self.assertEqual(len(planilha.contatos), 2)
        self.assertEqual(planilha.contatos[1]["cidade"], "Aracaju")  # padrão aplicado

        planilha.salvar()
        conteudo = csv.read_text(encoding="utf-8")
        self.assertIn("Clínica A dup", conteudo)  # duplicada continua no arquivo
        self.assertIn("Clínica B", conteudo)      # inválida continua no arquivo
        self.assertIn(",12345,", conteudo)        # telefone original intacto
        self.assertIn("nota A", conteudo)         # coluna extra preservada
        self.assertIn("5579992222222", conteudo)  # válido normalizado


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

    def test_mensagens_json_valido(self):
        m = pw.carregar_mensagens()
        for chave in pw.CHAVES_MENSAGENS:
            self.assertTrue(m[chave], chave)

    def test_saudacao_coerente_com_horario(self):
        m = pw.carregar_mensagens()
        for _ in range(30):
            manha = pw.montar_texto(self.CONTATO, m, datetime(2026, 1, 5, 9, 0))
            tarde = pw.montar_texto(self.CONTATO, m, datetime(2026, 1, 5, 15, 0))
            self.assertNotIn("Boa tarde", manha)
            self.assertNotIn("Bom dia", tarde)

    def test_periodo_do_dia(self):
        self.assertEqual(pw.periodo_do_dia(8), "manha")
        self.assertEqual(pw.periodo_do_dia(12), "tarde")
        self.assertEqual(pw.periodo_do_dia(18), "noite")

    def test_sem_bairro_usa_cidade(self):
        contato = {"clinica": "Clínica X", "bairro": "", "cidade": "Aracaju"}
        for _ in range(30):
            texto = pw.montar_texto(contato)
            self.assertNotIn("no  ", texto)
            self.assertNotIn("no e ", texto)
            self.assertIn("em Aracaju", texto)

    def test_followup_personalizado(self):
        for _ in range(20):
            texto = pw.montar_texto_followup(self.CONTATO)
            self.assertIn("Ferraz Odontologia", texto)
            self.assertNotIn("{", texto)

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

class TestIntervaloEntreEnvios(unittest.TestCase):
    AGORA = datetime(2026, 1, 5, 10, 0, 0)

    def test_sem_envio_anterior_nao_espera(self):
        self.assertEqual(pw.segundos_de_espera(None, 60, self.AGORA), 0)

    def test_espera_so_o_que_falta(self):
        ultimo = self.AGORA - timedelta(seconds=20)
        self.assertEqual(pw.segundos_de_espera(ultimo, 60, self.AGORA), 40)

    def test_envio_antigo_nao_espera(self):
        ultimo = self.AGORA - timedelta(hours=3)
        self.assertEqual(pw.segundos_de_espera(ultimo, 90, self.AGORA), 0)


class TestUltimoEnvio(BasePw):
    def test_pega_o_mais_recente(self):
        pw.ARQUIVO_LOG.write_text(
            '{"timestamp": "2026-01-05T10:00:00"}\n'
            '{"timestamp": "2026-01-05T11:30:00"}\n'
            'linha corrompida\n', encoding="utf-8")
        self.assertEqual(pw.ultimo_envio(), datetime(2026, 1, 5, 11, 30))

    def test_sem_historico(self):
        self.assertIsNone(pw.ultimo_envio())


class TestEdicaoMultilinha(unittest.TestCase):
    def test_mantem_linhas_vazias_ate_o_terminador(self):
        entradas = iter(["Olá!", "", "Segundo parágrafo.", "", "Fim.", "."])
        texto = pw.ler_texto_multilinha(lambda: next(entradas))
        self.assertEqual(texto, "Olá!\n\nSegundo parágrafo.\n\nFim.")


class TestJanelaHorario(unittest.TestCase):
    def test_dentro_da_janela(self):
        self.assertTrue(pw.dentro_da_janela(8, 19, datetime(2026, 1, 1, 8, 0)))
        self.assertTrue(pw.dentro_da_janela(8, 19, datetime(2026, 1, 1, 18, 59)))

    def test_fora_da_janela(self):
        # fim é exclusivo: 19h em ponto já está fora
        self.assertFalse(pw.dentro_da_janela(8, 19, datetime(2026, 1, 1, 19, 0)))
        # madrugada é padrão de robô
        self.assertFalse(pw.dentro_da_janela(8, 19, datetime(2026, 1, 1, 3, 0)))

    def test_dia_util(self):
        self.assertTrue(pw.dia_util(datetime(2026, 11, 9)))    # segunda
        self.assertFalse(pw.dia_util(datetime(2026, 11, 14)))  # sábado
        self.assertFalse(pw.dia_util(datetime(2026, 11, 15)))  # domingo


# ---------------------------------------------------------------------------
# Persistência: status + timestamp no CSV
# ---------------------------------------------------------------------------

def _contato(**extra):
    base = {"clinica": "Ferraz Odontologia", "bairro": "São José",
            "cidade": "Aracaju", "telefone": "5579999631871",
            "status": "pendente", "resposta": "", "atualizado_em": "",
            "ultimo_envio": "", "followups": ""}
    base.update(extra)
    return base


class TestPersistencia(BasePw):
    def test_marcar_enviado_grava_status_e_timestamp(self):
        contato = _contato()
        planilha = pw.Planilha(self.tmpdir / "prospeccao.csv")
        planilha.adicionar(contato)

        pw.marcar_enviado(contato, planilha)

        conteudo = planilha.caminho.read_text(encoding="utf-8")
        self.assertIn("enviado", conteudo)
        self.assertIn("aguardando", conteudo)
        self.assertTrue(contato["atualizado_em"])  # timestamp preenchido
        self.assertEqual(contato["ultimo_envio"], contato["atualizado_em"])

    def test_followup_incrementa_contador(self):
        contato = _contato(status="enviado", resposta="sem resposta")
        planilha = pw.Planilha(self.tmpdir / "prospeccao.csv")
        planilha.adicionar(contato)
        pw.marcar_enviado(contato, planilha, pw.FOLLOWUP)
        self.assertEqual(contato["followups"], "1")
        self.assertEqual(contato["resposta"], "aguardando")


class TestFollowup(unittest.TestCase):
    AGORA = datetime(2026, 1, 10, 10, 0)

    def test_elegivel_apos_n_dias_sem_resposta(self):
        c = _contato(status="enviado", resposta="aguardando",
                     ultimo_envio="2026-01-06T10:00:00")
        self.assertTrue(pw.precisa_followup(c, 3, 1, self.AGORA))

    def test_cedo_demais(self):
        c = _contato(status="enviado", ultimo_envio="2026-01-09T10:00:00")
        self.assertFalse(pw.precisa_followup(c, 3, 1, self.AGORA))

    def test_respondeu_ou_opt_out_nao_recebe(self):
        respondeu = _contato(status="enviado", resposta="respondeu",
                             ultimo_envio="2026-01-01T10:00:00")
        opt_out = _contato(status="nao_contatar", resposta="sem interesse",
                           ultimo_envio="2026-01-01T10:00:00")
        self.assertFalse(pw.precisa_followup(respondeu, 3, 1, self.AGORA))
        self.assertFalse(pw.precisa_followup(opt_out, 3, 1, self.AGORA))

    def test_respeita_maximo_de_followups(self):
        c = _contato(status="enviado", resposta="aguardando", followups="1",
                     ultimo_envio="2026-01-01T10:00:00")
        self.assertFalse(pw.precisa_followup(c, 3, 1, self.AGORA))
        self.assertTrue(pw.precisa_followup(c, 3, 2, self.AGORA))

    def test_csv_antigo_usa_atualizado_em(self):
        c = _contato(status="enviado", atualizado_em="2026-01-01T10:00:00")
        self.assertTrue(pw.precisa_followup(c, 3, 1, self.AGORA))


class TestReset(unittest.TestCase):
    def test_reabre_so_quem_nao_engajou(self):
        contatos = [
            _contato(clinica="A", status="enviado", resposta="sem resposta",
                     followups="1"),
            _contato(clinica="B", status="enviado", resposta="respondeu"),
            _contato(clinica="C", status="nao_contatar", resposta="sem interesse"),
            _contato(clinica="D", status="pendente"),
        ]
        self.assertEqual(pw.reabrir_fila(contatos), 1)
        self.assertEqual([c["status"] for c in contatos],
                         ["pendente", "enviado", "nao_contatar", "pendente"])
        self.assertEqual(contatos[0]["followups"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
