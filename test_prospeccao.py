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
# Segmento: inferência odonto/medico
# ---------------------------------------------------------------------------

class TestSegmento(BasePw):
    def test_inferir_odonto(self):
        for nome in ("Ferraz Odontologia | Dentista em Aracaju",
                     "Dentista 24Horas - Dr Lucas Eduardo",
                     "Clínica OrthoSim",
                     "Lessa Oral Clinic",
                     "dentista em Aracaju SE",
                     "implante dentario em Aracaju SE"):
            self.assertEqual(pw.inferir_segmento(nome), "odonto", nome)

    def test_inferir_medico(self):
        for nome in ("Centro Médico Siqueira Campos",
                     "Grupo Santa Helena - Hospital e Maternidade",
                     "Clínica Bem Viver - Exames - Ultrassom",
                     "clinica medica em Aracaju SE",
                     "clinica de psicologia em Aracaju SE"):
            self.assertEqual(pw.inferir_segmento(nome), "medico", nome)

    def test_sinal_sobreposto_nao_duplica_escore(self):
        # "odont" está dentro de "odontolog": sem dedupe, o empate viraria
        # odonto e CALE (exames+laboratório) seria classificada errado
        self.assertEqual(
            pw.inferir_segmento(
                "Clínica CALE - Consultas | Exames | Laboratório | Odontologia"),
            "medico")

    def test_sem_sinal_retorna_vazio(self):
        self.assertEqual(pw.inferir_segmento("Clínica X"), "")
        self.assertEqual(pw.inferir_segmento(""), "")
        self.assertEqual(pw.inferir_segmento(None), "")

    def test_acentos_e_maiusculas_irrelevantes(self):
        self.assertEqual(pw.inferir_segmento("CLÍNICA SAÚDE EM VIDA"),
                         "medico")
        self.assertEqual(pw.inferir_segmento("Odontologica São José"),
                         "odonto")

    def test_csv_com_segmento_e_filtro(self):
        csv = self.tmpdir / "seg.csv"
        csv.write_text(
            ",".join(pw.COLUNAS) + "\n"
            "Clínica Odonto,X,Aracaju,5579991111111,,,odonto,pendente,,,,,,,,\n"
            "Clínica Médica,Y,Aracaju,5579992222222,,,medico,pendente,,,,,,,,\n"
            "Clínica Sem Seg,Z,Aracaju,5579993333333,,,,pendente,,,,,,,,\n",
            encoding="utf-8",
        )
        contatos = pw.carregar_contatos(csv)
        self.assertEqual([c["segmento"] for c in contatos],
                         ["odonto", "medico", ""])

    def test_csv_antigo_sem_segmento_vira_vazio(self):
        csv = self.tmpdir / "antigo.csv"
        csv.write_text(
            "clinica,bairro,cidade,telefone,status\n"
            "Clínica A,Bairro X,Aracaju,5579991111111,pendente\n",
            encoding="utf-8",
        )
        contatos = pw.carregar_contatos(csv)
        self.assertEqual(contatos[0]["segmento"], "")


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

    def test_nome_para_mensagem_limpa_seo_do_maps(self):
        casos = {
            ("MB Odontologia | Dentista | Urgência Odontológica | Aracaju",
             "Salgado Filho"): "MB Odontologia",
            ("Odonto Center - Dentista", ""): "Odonto Center",
            ("Dra. Mairaira Leão (Periodontista e Implantodontista).",
             "São José"): "Dra. Mairaira Leão",
            ("Sorridents Clínicas Odontológicas Jardins Aracaju",
             "Jardins"): "Sorridents Clínicas Odontológicas",
            ("Clínica São José", "São José"): "Clínica São José",
            ("Clínica Odontológica São José", "São José"):
                "Clínica Odontológica São José",
            ("Odonto Aracaju", ""): "Odonto Aracaju",
            ("Ferraz Odontologia", "São José"): "Ferraz Odontologia",
            ("Centro-Oeste Odonto", ""): "Centro-Oeste Odonto",  # hífen sem espaço
            ("01 - Odonto10x | Clínica Odontológica", ""): "Odonto10x",
            ("Duo Odontologia Consultório Odontológico em Aracaju", ""):
                "Duo Odontologia Consultório Odontológico",
            ("Clínica Bem Viver. Consultas e Exames - Ultrassom", ""):
                "Clínica Bem Viver",
            ("Dr. Daniel Campos (Odontologia 24hrs)", ""): "Dr. Daniel Campos",
            ("Dr. Jose Luiz Sandes, Médico clínico geral", ""): "Dr. Jose Luiz Sandes",
            ("CENTRO MÉDICO DO TRABALHADOR LTDA", ""): "Centro Médico do Trabalhador",
            ("COS - Clínica de Odontologia", ""): "COS",  # sigla curta preservada
        }
        for (nome, bairro), esperado in casos.items():
            self.assertEqual(pw.nome_para_mensagem(nome, bairro, "Aracaju"),
                             esperado, nome)

    def test_concordancia_do_da(self):
        for nome, esperado in [("Dr. Daniel Campos", "do Dr. Daniel Campos"),
                               ("Grupo Santa Helena", "do Grupo Santa Helena"),
                               ("Ferraz Odontologia", "da Ferraz Odontologia"),
                               ("Dra. Mairaira Leão", "da Dra. Mairaira Leão")]:
            contato = {"clinica": nome, "bairro": "Centro", "cidade": "Aracaju"}
            for _ in range(10):
                self.assertIn(nome, pw.montar_texto(contato))
            self.assertEqual(pw._contexto(contato, pw.carregar_mensagens(), None)["da_clinica"],
                             esperado)

    def test_nome_nunca_fica_vazio(self):
        self.assertEqual(pw.nome_para_mensagem("Aracaju", "", "Aracaju"), "Aracaju")
        self.assertEqual(pw.nome_para_mensagem("(Clínica)", ""), "(Clínica)")

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
            self.assertNotIn("PROIMAGEM", texto)  # empregador nunca citado
            self.assertNotIn("prévia", texto)     # CTA é só agendar reunião
            self.assertIn("NF-e", texto)          # case sempre presente

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


class TestTravaEnvio(BasePw):
    """Impede dois processos enviando ao mesmo tempo (causa de duplicatas)."""

    def setUp(self):
        super().setUp()
        self.trava = self.tmpdir / ".envio.lock"

    def test_bloqueia_quando_outro_processo_vivo_tem_a_trava(self):
        import subprocess
        import sys as _sys
        outro = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.trava.write_text(str(outro.pid), encoding="utf-8")
            with self.assertRaises(pw.EnvioEmAndamento):
                with pw.TravaEnvio(self.trava):
                    pass
            self.assertTrue(self.trava.exists())  # trava alheia preservada
        finally:
            outro.kill()
            outro.wait()

    def test_trava_orfa_de_processo_morto_e_descartada(self):
        self.trava.write_text("999999", encoding="utf-8")
        with pw.TravaEnvio(self.trava):
            import os
            self.assertEqual(self.trava.read_text(encoding="utf-8"), str(os.getpid()))
        self.assertFalse(self.trava.exists())  # liberada ao sair

    def test_reentrante_no_mesmo_processo(self):
        with pw.TravaEnvio(self.trava):
            with pw.TravaEnvio(self.trava):
                self.assertTrue(self.trava.exists())
            self.assertTrue(self.trava.exists())  # só libera no nível externo
        self.assertFalse(self.trava.exists())

    def test_sessao_web_recusa_numero_que_ja_recebeu_hoje(self):
        hoje = datetime.now().isoformat(timespec="seconds")
        pw.ARQUIVO_LOG.write_text('{"timestamp": "%s", "telefone": "5579991111111"}\n'
                                  % hoje, encoding="utf-8")
        sessao = pw.SessaoWeb()
        with redirect_stdout(io.StringIO()) as saida:
            ok = sessao.enviar(_contato(telefone="5579991111111"), "oi")
        self.assertFalse(ok)
        self.assertIn("duplicado evitado", saida.getvalue())
        self.assertIsNone(sessao.driver)  # nem abriu o navegador


class TestTextoConfere(unittest.TestCase):
    """Só envia se a caixa do WhatsApp contém EXATAMENTE a mensagem aprovada."""

    MSG = ("Bom dia! Tudo bem?\n\nSou o Nickolas.\n\n"
           "• Triagem no WhatsApp;\n• NF-e em minutos.\n\nQuem seria o contato? 😊")

    def test_mesmo_texto_com_quebras_e_emoji_renderizados_diferente(self):
        caixa = ("Bom dia! Tudo bem?\nSou o Nickolas.\n"
                 "Triagem no WhatsApp;\nNF-e em minutos.\nQuem seria o contato?")
        self.assertTrue(pw.texto_confere(caixa, self.MSG))

    def test_texto_duplicado_e_recusado(self):
        # O bug real: cópia sem quebras de linha misturada ao texto original
        achatado = self.MSG.replace("\n", "")
        caixa = "Bom dia! Tudo bem?\n" + achatado + "\n" + self.MSG.split("\n", 2)[2]
        self.assertFalse(pw.texto_confere(caixa, self.MSG))

    def test_texto_incompleto_ou_vazio_e_recusado(self):
        self.assertFalse(pw.texto_confere("Bom dia! Tudo bem?", self.MSG))
        self.assertFalse(pw.texto_confere("", self.MSG))
        self.assertFalse(pw.texto_confere("", ""))

    def test_rascunho_antigo_somado_e_recusado(self):
        self.assertFalse(pw.texto_confere("oi " + self.MSG, self.MSG))


class TestFeriados(unittest.TestCase):
    def test_moveis_2026(self):
        from datetime import date
        for d in [date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
                  date(2026, 4, 3), date(2026, 6, 4)]:  # Carnaval, Cinzas, Sexta Santa, Corpus
            self.assertTrue(pw.eh_feriado(d), d)

    def test_fixos_e_locais(self):
        from datetime import date
        for d in [date(2026, 11, 20), date(2026, 12, 25), date(2027, 3, 17),
                  date(2026, 7, 8)]:
            self.assertTrue(pw.eh_feriado(d), d)

    def test_dia_util_ignora_feriado(self):
        self.assertFalse(pw.dia_util(datetime(2026, 11, 20, 9)))  # sexta, feriado
        self.assertTrue(pw.dia_util(datetime(2026, 11, 19, 9)))   # quinta comum


class TestValidarMensagem(unittest.TestCase):
    CONTATO = {"clinica": "MB Odontologia | Dentista | Urgência", "bairro": "Centro",
               "cidade": "Aracaju"}

    def test_mensagem_gerada_passa(self):
        for _ in range(30):
            self.assertIsNone(pw.validar_mensagem(pw.montar_texto(self.CONTATO),
                                                  self.CONTATO))

    def test_problemas_sao_detectados(self):
        boa = pw.montar_texto(self.CONTATO)
        casos = {
            boa + " {clinica}": "token",
            boa + " Fale com a PROIMAGEM": "proibido",
            boa + " posso mandar uma prévia": "proibido",
            boa.replace("MB Odontologia", "MB Odontologia | Dentista"): "SEO",
            boa.replace("MB Odontologia", "Outra Clínica"): "ausente",
            "Olá! Conhece a MB Odontologia? Vamos conversar.": "tamanho",
        }
        for msg, trecho in casos.items():
            problema = pw.validar_mensagem(msg, self.CONTATO)
            self.assertIsNotNone(problema, trecho)
            self.assertIn(trecho.lower(), problema.lower())

    def test_nome_generico_e_pulado(self):
        contato = {"clinica": "Implantes Dentários | Dentista | Ortoriso Aracaju",
                   "bairro": "Centro", "cidade": "Aracaju"}
        self.assertIn("genérico", pw.validar_mensagem(pw.montar_texto(contato), contato))

    def test_paragrafo_duplicado(self):
        boa = pw.montar_texto(self.CONTATO)
        paragrafo = [l for l in boa.splitlines() if len(l) > 20][0]
        self.assertIn("repetido", pw.validar_mensagem(boa + "\n" + paragrafo,
                                                      self.CONTATO))


class _SessaoFalsa:
    """Simula o WhatsApp Web: cada chamada consome um resultado do roteiro."""
    roteiro: list = []
    chamadas: list = []
    anexos: list = []

    def __init__(self):
        self.ultimo_erro = None
        self.confirmado = False

    def enviar(self, contato, mensagem, permitir_reenvio=False, confirmar=True):
        assert confirmar is False  # modo automático nunca pergunta
        _SessaoFalsa.chamadas.append(contato["telefone"])
        ok, erro, confirmado = _SessaoFalsa.roteiro.pop(0)
        self.ultimo_erro, self.confirmado = erro, confirmado
        return ok

    def enviar_arquivo(self, caminho):
        _SessaoFalsa.anexos.append(Path(caminho).name)
        return True

    def fechar(self):
        pass


class TestModoAutomatico(BasePw):
    def setUp(self):
        super().setUp()
        self._orig = (pw.SessaoWeb.__dict__.copy(), pw.ARQUIVO_TRAVA, pw.PASTA_LOGS,
                      pw.dia_util, pw.SessaoWeb, pw.notificar_windows,
                      pw.verificar_conversas)
        self.avisos = []
        pw.notificar_windows = lambda titulo, texto: self.avisos.append((titulo, texto))
        pw.verificar_conversas = lambda planilha, sessao, **kw: {
            "conferidas": 0, "nao_verificaveis": 0, "problemas": [], "respostas": [], "optouts": [],
            "automaticas": [], "erro": None}
        pw.ARQUIVO_TRAVA = self.tmpdir / ".envio.lock"
        pw.PASTA_LOGS = self.tmpdir / "logs"
        pw.dia_util = lambda agora=None: True
        falsa = type("SessaoFalsa", (_SessaoFalsa,), {
            k: getattr(pw.SessaoWeb, k) for k in
            ("SEM_WHATSAPP", "DESLOGADO", "TEXTO_NAO_CONFERE", "NAO_CONFIRMADO",
             "TECNICO", "BLOQUEADO", "CANCELADO")})
        pw.SessaoWeb = falsa
        _SessaoFalsa.chamadas = []
        _SessaoFalsa.anexos = []
        self.planilha = pw.Planilha(self.tmpdir / "prospeccao.csv")
        for i, nome in enumerate(["Clínica Alfa", "Clínica Beta", "Clínica Gama",
                                  "Clínica Delta"]):
            self.planilha.adicionar(_contato(clinica=nome, bairro="Centro",
                                             telefone=f"557999000000{i}"))
        self.planilha.adicionar(_contato(clinica="Fixo", telefone="557932000000"))
        import argparse
        self.args = argparse.Namespace(limite_dia=2, hora_inicio=0, hora_fim=24,
                                       intervalo_min=0, intervalo_max=0)

    def tearDown(self):
        (_, pw.ARQUIVO_TRAVA, pw.PASTA_LOGS, pw.dia_util, pw.SessaoWeb,
         pw.notificar_windows, pw.verificar_conversas) = self._orig
        super().tearDown()

    def _rodar(self, roteiro):
        self.planilha.salvar()  # o modo automático relê o CSV do disco
        _SessaoFalsa.roteiro = list(roteiro)
        with redirect_stdout(io.StringIO()) as saida:
            n = pw.rodar_automatico(self.planilha, self.args, pw.carregar_mensagens())
        return n, saida.getvalue()

    def test_respeita_cota_celular_antes_de_fixo_e_sem_whatsapp(self):
        sem = (False, pw.SessaoWeb.SEM_WHATSAPP, False)
        ok = (True, None, True)
        n, log = self._rodar([sem, ok, ok])
        self.assertEqual(n, 2)                                  # cota = 2
        self.assertNotIn("557932000000", _SessaoFalsa.chamadas)  # fixo só depois
        self.assertEqual(self.planilha.contatos[0]["status"], "sem_whatsapp")
        self.assertEqual(pw.envios_hoje(), 2)
        self.assertFalse(pw.ARQUIVO_TRAVA.exists())             # trava liberada

    def test_para_se_desconectado(self):
        n, log = self._rodar([(False, pw.SessaoWeb.DESLOGADO, False)])
        self.assertEqual(n, 0)
        self.assertIn("desconectado", log)
        self.assertEqual(len(_SessaoFalsa.chamadas), 1)

    def test_para_apos_duas_falhas_seguidas(self):
        tec = (False, pw.SessaoWeb.TECNICO, False)
        n, log = self._rodar([tec, tec])
        self.assertEqual(n, 0)
        self.assertIn("2 falhas", log)
        self.assertTrue(self.avisos)  # avisou no Windows
        self.assertEqual(len(_SessaoFalsa.chamadas), 2)

    def test_para_se_envio_nao_foi_conferido(self):
        self.args.limite_dia = 10
        n, log = self._rodar([(True, pw.SessaoWeb.NAO_CONFIRMADO, False)])
        self.assertEqual(n, 1)                      # registrado (ENTER foi dado)
        self.assertIn("[PARADO]", log)
        self.assertEqual(len(_SessaoFalsa.chamadas), 1)  # não seguiu adiante

    def test_retenta_uma_vez_se_texto_nao_conferir(self):
        n, _ = self._rodar([(False, pw.SessaoWeb.TEXTO_NAO_CONFERE, False),
                            (True, None, True), (True, None, True)])
        self.assertEqual(n, 2)
        self.assertEqual(_SessaoFalsa.chamadas[0], _SessaoFalsa.chamadas[1])

    def test_fixo_entra_na_fila_depois_dos_celulares(self):
        self.args.limite_dia = 10
        ok = (True, None, True)
        n, _ = self._rodar([ok, ok, ok, ok, (False, pw.SessaoWeb.SEM_WHATSAPP, False)])
        self.assertEqual(n, 4)
        self.assertEqual(_SessaoFalsa.chamadas[-1], "557932000000")
        self.assertEqual(self.planilha.contatos[-1]["status"], "sem_whatsapp")

    def test_mesma_clinica_com_outro_numero_nao_recebe(self):
        self.args.limite_dia = 10
        self.planilha.adicionar(_contato(clinica="Clínica ALFA - Consultas | Exames",
                                         bairro="Centro", telefone="5579990000099"))
        ok = (True, None, True)
        n, log = self._rodar([ok] * 4 + [(False, pw.SessaoWeb.SEM_WHATSAPP, False)])
        self.assertNotIn("5579990000099", _SessaoFalsa.chamadas)
        self.assertEqual(self.planilha.contatos[-1]["status"], "mesma_clinica")
        self.assertIn("MESMA CLÍNICA", log)

    def test_nome_bloqueado_nunca_recebe(self):
        self.planilha.contatos[0]["clinica"] = "PROIMAGEM Diagnóstico"
        n, log = self._rodar([(True, None, True), (True, None, True)])
        self.assertNotIn("5579990000000", _SessaoFalsa.chamadas)
        self.assertEqual(self.planilha.contatos[0]["status"], "bloqueado")

    def test_resumo_vai_para_o_proprio_numero(self):
        self.args.resumo_para = "79981677954"
        ok = (True, None, True)
        n, log = self._rodar([ok, ok, ok])  # 2 envios + 1 resumo
        self.assertEqual(n, 2)
        self.assertEqual(_SessaoFalsa.chamadas[-1], "5579981677954")
        self.assertEqual(pw.envios_hoje(), 2)  # resumo não conta no limite

    def test_reler_csv_preserva_contatos_gravados_pelo_minerador(self):
        self.planilha.salvar()
        # minerador grava um contato novo DEPOIS de o envio ter carregado o CSV
        outra = pw.carregar_planilha(self.planilha.caminho)
        outra.adicionar(_contato(clinica="Nova do Minerador", telefone="5579990000077"))
        outra.salvar()
        _SessaoFalsa.roteiro = [(True, None, True)] * 2
        with redirect_stdout(io.StringIO()):
            pw.rodar_automatico(self.planilha, self.args, pw.carregar_mensagens())
        self.assertIn("Nova do Minerador",
                      self.planilha.caminho.read_text(encoding="utf-8"))

    def test_nao_faz_nada_em_feriado_ou_fora_da_janela(self):
        pw.dia_util = lambda agora=None: False
        n, log = self._rodar([])
        self.assertEqual((n, _SessaoFalsa.chamadas), (0, []))
        self.assertIn("não é dia útil", log)


class TestLeituraDeRespostas(unittest.TestCase):
    ENVIADA = pw.montar_texto({"clinica": "MB Odontologia", "bairro": "Centro",
                               "cidade": "Aracaju"})
    DESDE = datetime(2026, 11, 10, 8, 25, 31)

    def _bolha(self, hora, quem, texto):
        return pw.interpretar_bolha(f"[{hora}, 10/11/2026] {quem}: ", texto)

    def test_interpreta_cabecalho_da_bolha(self):
        b = self._bolha("08:25", "Nickolas", "oi")
        self.assertEqual((b["remetente"], b["quando"]),
                         ("Nickolas", datetime(2026, 11, 10, 8, 25)))

    def test_robo_imediato_e_resposta_automatica(self):
        bolhas = [self._bolha("08:25", "Nickolas", self.ENVIADA),
                  self._bolha("08:25", "MB Odontologia",
                              "Olá! Me chamo Gabi, sou a secretária. Como posso ajudar?")]
        self.assertEqual(pw.classificar_resposta(bolhas, [self.ENVIADA], self.DESDE),
                         "resposta automatica")

    def test_pessoa_respondendo_depois(self):
        bolhas = [self._bolha("08:25", "Nickolas", self.ENVIADA),
                  self._bolha("08:25", "MB", "Seja bem-vindo! Escolha uma das opções"),
                  self._bolha("10:12", "MB", "Oi Nickolas, pode ser quinta às 14h?")]
        self.assertEqual(pw.classificar_resposta(bolhas, [self.ENVIADA], self.DESDE),
                         "respondeu")

    def test_opt_out(self):
        bolhas = [self._bolha("08:25", "Nickolas", self.ENVIADA),
                  self._bolha("11:00", "MB", "Obrigado, mas não temos interesse.")]
        self.assertEqual(pw.classificar_resposta(bolhas, [self.ENVIADA], self.DESDE),
                         "sem interesse")

    def test_frase_comum_nao_vira_opt_out(self):
        bolhas = [self._bolha("08:25", "Nickolas", self.ENVIADA),
                  self._bolha("11:00", "MB", "Para agendar, me passa seu horário?")]
        self.assertEqual(pw.classificar_resposta(bolhas, [self.ENVIADA], self.DESDE),
                         "respondeu")

    def test_sem_resposta_ou_mensagens_antigas(self):
        bolhas = [self._bolha("07:00", "MB", "mensagem antiga de ontem"),
                  self._bolha("08:25", "Nickolas", self.ENVIADA)]
        self.assertIsNone(pw.classificar_resposta(bolhas, [self.ENVIADA], self.DESDE))

    def test_sem_a_nossa_mensagem_nao_classifica(self):
        bolhas = [self._bolha("11:00", "MB", "oi")]
        self.assertIsNone(pw.classificar_resposta(bolhas, [self.ENVIADA], self.DESDE))


class TestResultadoEntrega(unittest.TestCase):
    MSG = "Bom dia! Mensagem de prospecção completa para a clínica, com texto suficiente."
    ENVIO = datetime(2026, 11, 10, 8, 25, 31)

    def _b(self, hora, quem, texto):
        return pw.interpretar_bolha(f"[{hora}, 10/11/2026] {quem}: ", texto)

    def test_ok_e_duplicada(self):
        um = [self._b("08:25", "Eu", self.MSG)]
        self.assertEqual(pw.resultado_entrega(um, self.MSG, self.ENVIO), "ok")
        self.assertEqual(pw.resultado_entrega(um * 2, self.MSG, self.ENVIO), "duplicada")

    def test_so_a_resposta_do_robo_carregou_e_inconclusivo(self):
        # Caso real (MB): só a última bolha (robô, mesmo minuto) carregou
        bolhas = [self._b("08:25", "MB", "Olá, me chamo Gabi")]
        self.assertEqual(pw.resultado_entrega(bolhas, self.MSG, self.ENVIO), "inconclusivo")
        self.assertEqual(pw.resultado_entrega([], self.MSG, self.ENVIO), "inconclusivo")

    def test_ausente_so_quando_o_historico_cobre_o_envio(self):
        bolhas = [self._b("07:50", "MB", "mensagem anterior"),
                  self._b("08:26", "MB", "outra")]
        self.assertEqual(pw.resultado_entrega(bolhas, self.MSG, self.ENVIO), "ausente")


class TestChaveClinica(unittest.TestCase):
    def test_mesma_clinica_com_nomes_diferentes(self):
        a = {"clinica": "Clínica CALE - Consultas | Exames | Laboratório", "bairro": ""}
        b = {"clinica": "Clinica Cale", "bairro": ""}
        self.assertEqual(pw.chave_clinica(a), pw.chave_clinica(b))

    def test_clinicas_diferentes(self):
        self.assertNotEqual(pw.chave_clinica({"clinica": "Odonto Center", "bairro": ""}),
                            pw.chave_clinica({"clinica": "Odonto House", "bairro": ""}))


class TestResumo(unittest.TestCase):
    def test_resumo_lista_o_essencial(self):
        texto = pw.montar_resumo(
            ["MB Odontologia"], ["Fixo X"], [],
            {"conferidas": 1, "nao_verificaveis": 0, "problemas": ["Y: mensagem aparece 2x"],
             "respostas": ["Odonto Center"], "optouts": [], "automaticas": []},
            pendentes=5, limite_dia=10, parada=None)
        for trecho in ("Enviadas: 1", "MB Odontologia", "ATENÇÃO: Y", "Odonto Center",
                       "Sem WhatsApp", "Fila acabando"):
            self.assertIn(trecho, texto)


class TestTextosAB(unittest.TestCase):
    COM_NOTA = {"clinica": "MB Odontologia", "bairro": "Centro", "cidade": "Aracaju",
                "avaliacoes": "4.9 estrelas (321 avaliações)"}
    SEM_NOTA = {"clinica": "MB Odontologia", "bairro": "Centro", "cidade": "Aracaju",
                "avaliacoes": "3.8 estrelas"}

    def test_nota_google_so_elogia_nota_alta(self):
        self.assertEqual(pw.nota_google(self.COM_NOTA), "4,9")
        self.assertEqual(pw.nota_google(self.SEM_NOTA), "")
        self.assertEqual(pw.nota_google({"avaliacoes": ""}), "")

    def test_sem_nota_nunca_fala_em_estrelas(self):
        for _ in range(40):
            self.assertNotIn("estrelas", pw.montar_texto(self.SEM_NOTA))
            self.assertNotIn("estrelas", pw.montar_abertura(self.SEM_NOTA))

    def test_com_nota_as_vezes_elogia(self):
        textos = {pw.montar_abertura(self.COM_NOTA) for _ in range(60)}
        self.assertTrue(any("4,9 estrelas" in t for t in textos))

    def test_abertura_e_apresentacao_passam_na_validacao(self):
        for contato in (self.COM_NOTA, self.SEM_NOTA):
            for _ in range(30):
                abertura = pw.montar_abertura(contato)
                self.assertIsNone(pw.validar_mensagem(abertura, contato, "curta"), abertura)
                self.assertLess(len(abertura), 300)
                apresentacao = pw.montar_apresentacao(contato)
                self.assertIsNone(pw.validar_mensagem(apresentacao, contato, "apresentacao"))
                self.assertNotIn("{", apresentacao)
                resumo = pw.montar_resumo_completa(contato)
                self.assertIsNone(pw.validar_mensagem(resumo, contato, "resumo"), resumo)
                self.assertNotIn("{", resumo)

    def test_segunda_etapa(self):
        self.assertEqual(pw._segunda_etapa({"variante": "curta", "etapa": "abertura"}),
                         "apresentacao")
        self.assertIsNone(pw._segunda_etapa({"variante": "curta", "etapa": "apresentacao"}))
        self.assertEqual(pw._segunda_etapa({"variante": "completa", "etapa": "apresentacao",
                                            "versao_textos": "2026-11-11"}), "resumo")
        # textos antigos (CTA não prometia resumo): atendimento manual, como antes
        self.assertIsNone(pw._segunda_etapa({"variante": "completa", "etapa": "apresentacao",
                                             "versao_textos": ""}))
        self.assertIsNone(pw._segunda_etapa({"variante": "completa", "etapa": "resumo",
                                             "versao_textos": "2026-11-11"}))

    def test_resposta_negativa_nao_recebe_venda(self):
        for t in ["Não", "não, obrigado", "Já temos sistema", "No momento não",
                  "Obrigada, mas estamos bem"]:
            self.assertTrue(pw.resposta_negativa([t]), t)
        for t in ["Sim, bastante!", "Pode mandar", "Quem fala?", "É sim, o dia todo",
                  "Vou repassar para a gestora"]:
            self.assertFalse(pw.resposta_negativa([t]), t)

    def test_nao_relevante_e_optout(self):
        for t in ["Não é relevante, obrigado", "Não precisamos", "não temos necessidade"]:
            self.assertTrue(any(__import__("re").search(p, t.lower())
                                for p in pw.PADROES_OPTOUT), t)

    def test_janela_do_chrome_fora_da_tela_por_padrao(self):
        self.assertFalse(pw.JANELA_VISIVEL)
        oculta = pw.opcoes_janela(False)
        self.assertIn("--window-position=-32000,-32000", oculta)
        self.assertNotIn("--start-maximized", oculta)
        self.assertEqual(pw.opcoes_janela(True), ["--start-maximized"])
        args = pw._criar_parser().parse_args(["--conectar"])
        self.assertTrue(args.conectar and not args.mostrar_janela)

    def test_despedida_formal_e_valida(self):
        for contato in (self.COM_NOTA, self.SEM_NOTA):
            for _ in range(20):
                texto = pw.montar_despedida(contato)
                self.assertIsNone(pw.validar_mensagem(texto, contato, "despedida"), texto)
                self.assertNotIn("{", texto)

    def test_tom_formal(self):
        informais = ("rapidinh", "curtinh", "te mando", "te conto", "😊", " aí", "vlw", "blz")
        for contato in (self.COM_NOTA, self.SEM_NOTA):
            for _ in range(40):
                for texto in (pw.montar_texto(contato), pw.montar_abertura(contato),
                              pw.montar_texto_followup(contato), pw.montar_apresentacao(contato),
                              pw.montar_resumo_completa(contato)):
                    for termo in informais:
                        self.assertNotIn(termo, texto.lower(), texto)

    def test_followup_nao_atribui_automacoes_a_clinica(self):
        for _ in range(40):
            self.assertNotIn("automações da ", pw.montar_texto_followup(self.COM_NOTA))

    def test_variante_fixa_por_numero_e_dividida(self):
        self.assertEqual(pw.variante_ab("5579999000001"), pw.variante_ab("5579999000001"))
        variantes = {pw.variante_ab(f"55799990000{i:02d}") for i in range(20)}
        self.assertEqual(variantes, {"curta", "completa"})


class TestMenuDeRobo(unittest.TestCase):
    def test_encontra_opcao_de_atendente(self):
        odonto_center = ("Para agilizar seu atendimento, escolha uma das opções abaixo\n"
                         "1. Agendar consulta.\n2. Remarcar horário.\n3. Estou com dor.\n"
                         "4. Endereço e horários\n5. Falar com um atendente.")
        self.assertEqual(pw.opcao_atendente([odonto_center]), "5")
        self.assertEqual(pw.opcao_atendente(["5\ufe0f\u20e3 Falar com atendente"]), "5")
        self.assertEqual(pw.opcao_atendente(["*3* - Conversar com a recepção"]), "3")

    def test_sem_menu(self):
        self.assertIsNone(pw.opcao_atendente(["Olá! Como posso ajudar?",
                                              "Ligue 1234 para falar com a gente"]))


class TestDiasUteis(unittest.TestCase):
    def test_conta_so_dias_uteis(self):
        self.assertEqual(pw.dias_uteis_entre(datetime(2026, 11, 13, 9),   # sexta
                                             datetime(2026, 11, 16, 9)), 1)  # segunda
        self.assertEqual(pw.dias_uteis_entre(datetime(2026, 11, 10, 9),
                                             datetime(2026, 11, 12, 9)), 2)
        self.assertEqual(pw.dias_uteis_entre(datetime(2026, 11, 19, 9),   # quinta
                                             datetime(2026, 11, 23, 9)), 1)  # 20 é feriado


class TestAcoesEAvisos(TestModoAutomatico):
    def _contato_enviado(self, **extra):
        c = self.planilha.contatos[0]
        c.update({"status": "enviado", "resposta": "aguardando",
                  "ultimo_envio": "2026-11-03T10:00:00", **extra})
        return c

    def test_followup_vem_antes_dos_novos(self):
        self.args.limite_dia = 10
        self.args.dias_followup, self.args.max_followups = 2, 1
        c = self._contato_enviado()
        n, log = self._rodar([(True, None, True)] * 6)
        self.assertEqual(_SessaoFalsa.chamadas[0], c["telefone"])
        atual = next(x for x in self.planilha.contatos if x["telefone"] == c["telefone"])
        self.assertEqual(atual["followups"], "1")  # (o CSV é relido no início)
        tipos = [r.get("tipo") for r in pw.ler_historico()]
        self.assertEqual(tipos[0], "followup")

    def test_novo_contato_recebe_variante_e_etapa(self):
        n, _ = self._rodar([(True, None, True)] * 2)
        enviados = [c for c in self.planilha.contatos if c["status"] == "enviado"]
        self.assertEqual(len(enviados), 2)
        for c in enviados:
            self.assertIn(c["variante"], ("curta", "completa"))
            self.assertEqual(c["etapa"],
                             "abertura" if c["variante"] == "curta" else "apresentacao")
            self.assertEqual(c["versao_textos"], pw.carregar_mensagens()["versao"])
        variantes = [r.get("variante") for r in pw.ler_historico()]
        self.assertTrue(all(variantes))

    def test_executa_apresentacao_e_menu(self):
        c1 = self._contato_enviado(variante="curta", etapa="abertura", resposta="respondeu")
        c2 = self.planilha.contatos[1]
        c2.update(status="enviado", resposta="resposta automatica")
        self.planilha.salvar()
        _SessaoFalsa.roteiro = [(True, None, True), (True, None, True)]
        relatorio = {"acoes": [("apresentacao", c1["telefone"], None),
                               ("menu", c2["telefone"], "5")]}
        sessao = pw.SessaoWeb()
        with redirect_stdout(io.StringIO()):
            feitos = pw.executar_acoes(relatorio, self.planilha, sessao,
                                       pw.carregar_mensagens(), self.args)
        self.assertEqual(len(feitos), 2)
        self.assertEqual(c1["etapa"], "apresentacao")
        self.assertEqual(c1["resposta"], "aguardando")      # volta a monitorar
        self.assertEqual(c2["menu"], "respondido")
        self.assertEqual(c2["resposta"], "resposta automatica")  # menu não muda resposta
        self.assertEqual(pw.envios_hoje(), 0)    # nem menu nem apresentação contam no limite
        self.assertEqual(pw.segundas_etapas_hoje(), 1)

    def test_segunda_etapa_sai_mesmo_com_cota_esgotada(self):
        self.args.limite_dia = 1
        c0 = self.planilha.contatos[1]
        pw.atualizar_historico(c0, "texto", "automatico")   # cota de novos esgotada
        self.assertEqual(pw.envios_hoje(), 1)
        c = self._contato_enviado(variante="curta", etapa="abertura", resposta="respondeu")
        self.planilha.salvar()
        _SessaoFalsa.roteiro = [(True, None, True)]
        with redirect_stdout(io.StringIO()):
            feitos = pw.executar_acoes({"acoes": [("apresentacao", c["telefone"], None)]},
                                       self.planilha, pw.SessaoWeb(),
                                       pw.carregar_mensagens(), self.args)
        self.assertEqual(len(feitos), 1)
        self.assertEqual(c["etapa"], "apresentacao")
        # teto próprio: a 2ª apresentação do dia (limite 1) fica adiada
        c2 = self.planilha.contatos[2]
        c2.update(status="enviado", variante="curta", etapa="abertura", resposta="respondeu")
        with redirect_stdout(io.StringIO()):
            feitos = pw.executar_acoes({"acoes": [("apresentacao", c2["telefone"], None)]},
                                       self.planilha, pw.SessaoWeb(),
                                       pw.carregar_mensagens(), self.args)
        self.assertEqual(feitos, [])
        self.assertEqual(c2["etapa"], "abertura")

    def test_aviso_imediato_de_resposta(self):
        self.args.resumo_para = "79981677954"
        _SessaoFalsa.roteiro = [(True, None, True)]
        sessao = pw.SessaoWeb()
        pw.avisar_respostas({"respostas_texto": [("MB Odontologia", "Pode ser quinta?")],
                             "optouts": []}, sessao, self.args.resumo_para)
        self.assertEqual(_SessaoFalsa.chamadas, ["5579981677954"])
        self.assertTrue(self.avisos)

    def test_sem_respostas_nao_avisa(self):
        pw.avisar_respostas({"respostas_texto": [], "optouts": []}, pw.SessaoWeb(), "79981677954")
        self.assertEqual((_SessaoFalsa.chamadas, self.avisos), ([], []))

    def test_estatisticas_ab(self):
        a, b = self.planilha.contatos[0], self.planilha.contatos[1]
        a.update(status="enviado", variante="curta", etapa="apresentacao")
        b.update(status="enviado", variante="completa", resposta="aguardando")
        texto = pw.estatisticas_ab(self.planilha.contatos)
        self.assertIn("curta: 1/1", texto)
        self.assertIn("completa: 0/1", texto)

    def test_estatisticas_ab_por_versao_dos_textos(self):
        a, b, c = self.planilha.contatos[0], self.planilha.contatos[1], self.planilha.contatos[2]
        a.update(status="enviado", variante="completa", etapa="resumo",
                 resposta="aguardando", versao_textos="v2")
        b.update(status="enviado", variante="completa", resposta="respondeu",
                 versao_textos="")                       # textos antigos: fora da conta
        c.update(status="enviado", variante="curta", etapa="abertura",
                 resposta="aguardando", versao_textos="v2")
        texto = pw.estatisticas_ab(self.planilha.contatos, "v2")
        self.assertIn("textos de v2", texto)
        self.assertIn("completa: 1/1", texto)
        self.assertIn("curta: 0/1", texto)

    def test_despedida_para_quem_nao_tem_interesse(self):
        c = self._contato_enviado(status="nao_contatar", resposta="sem interesse")
        self.planilha.salvar()
        _SessaoFalsa.roteiro = [(True, None, True)]
        acoes = {"acoes": [("despedida", c["telefone"], None)]}
        with redirect_stdout(io.StringIO()):
            feitos = pw.executar_acoes(acoes, self.planilha, pw.SessaoWeb(),
                                       pw.carregar_mensagens(), self.args)
        self.assertEqual(len(feitos), 1)
        self.assertEqual(c["etapa"], "despedida")
        self.assertEqual(c["status"], "nao_contatar")     # continua fora da fila
        self.assertEqual(pw.envios_hoje(), 0)              # não conta no limite
        self.assertEqual(pw.ler_historico()[-1]["tipo"], "despedida")
        # nunca duas vezes
        with redirect_stdout(io.StringIO()):
            feitos = pw.executar_acoes(acoes, self.planilha, pw.SessaoWeb(),
                                       pw.carregar_mensagens(), self.args)
        self.assertEqual(feitos, [])

    def test_executa_resumo_da_completa(self):
        c = self._contato_enviado(variante="completa", etapa="apresentacao",
                                  resposta="respondeu", versao_textos="v2")
        self.planilha.salvar()
        _SessaoFalsa.roteiro = [(True, None, True)]
        with redirect_stdout(io.StringIO()):
            feitos = pw.executar_acoes({"acoes": [("resumo", c["telefone"], None)]},
                                       self.planilha, pw.SessaoWeb(),
                                       pw.carregar_mensagens(), self.args)
        self.assertEqual(len(feitos), 1)
        self.assertIn("resumo", feitos[0])
        self.assertEqual(c["etapa"], "resumo")
        self.assertEqual(c["resposta"], "aguardando")
        self.assertEqual(pw.ler_historico()[-1]["tipo"], "resumo")
        esperado = pw.demonstracao_pdf(pw.carregar_mensagens()).name
        self.assertEqual(_SessaoFalsa.anexos, [esperado])

    def test_pdf_anexado_junto_com_a_apresentacao(self):
        c = self._contato_enviado(variante="curta", etapa="abertura",
                                  resposta="respondeu")
        self.planilha.salvar()
        _SessaoFalsa.roteiro = [(True, None, True)]
        with redirect_stdout(io.StringIO()):
            feitos = pw.executar_acoes({"acoes": [("apresentacao", c["telefone"], None)]},
                                       self.planilha, pw.SessaoWeb(),
                                       pw.carregar_mensagens(), self.args)
        self.assertEqual(len(feitos), 1)
        esperado = pw.demonstracao_pdf(pw.carregar_mensagens()).name
        self.assertEqual(_SessaoFalsa.anexos, [esperado])

    def test_pdf_desativado_quando_config_vazia(self):
        c = self._contato_enviado(variante="curta", etapa="abertura",
                                  resposta="respondeu")
        self.planilha.salvar()
        m = dict(pw.carregar_mensagens())
        m["demonstracao"] = {"pdf": "", "html": ""}
        _SessaoFalsa.roteiro = [(True, None, True)]
        with redirect_stdout(io.StringIO()):
            pw.executar_acoes({"acoes": [("apresentacao", c["telefone"], None)]},
                              self.planilha, pw.SessaoWeb(), m, self.args)
        self.assertEqual(_SessaoFalsa.anexos, [])

    def test_despedida_nao_anexa_pdf(self):
        c = self._contato_enviado(status="nao_contatar", resposta="sem interesse")
        self.planilha.salvar()
        _SessaoFalsa.roteiro = [(True, None, True)]
        with redirect_stdout(io.StringIO()):
            pw.executar_acoes({"acoes": [("despedida", c["telefone"], None)]},
                              self.planilha, pw.SessaoWeb(),
                              pw.carregar_mensagens(), self.args)
        self.assertEqual(_SessaoFalsa.anexos, [])


class TestDemonstracao(BasePw):
    """2ª etapa: link da demonstração (html) e caminho do PDF a anexar."""

    CONTATO = {"clinica": "Ferraz Odontologia", "bairro": "São José",
               "cidade": "Aracaju"}

    @staticmethod
    def _mensagens(**demo):
        m = dict(pw.carregar_mensagens())
        base = {"pdf": "demonstracao.pdf", "html": ""}
        base.update(demo)
        m["demonstracao"] = base
        return m

    def test_link_entrna_na_apresentacao_quando_html_configurado(self):
        m = self._mensagens(html="https://exemplo.github.io/demonstracao/")
        msg = pw.montar_apresentacao(self.CONTATO, m)
        self.assertIn("Demonstração interativa: "
                      "https://exemplo.github.io/demonstracao/", msg)
        self.assertIsNone(pw.validar_mensagem(msg, self.CONTATO, "apresentacao"))

    def test_link_entrna_no_resumo_quando_html_configurado(self):
        m = self._mensagens(html="https://exemplo.github.io/demonstracao/")
        msg = pw.montar_resumo_completa(self.CONTATO, m)
        self.assertIn("https://exemplo.github.io/demonstracao/", msg)
        self.assertIsNone(pw.validar_mensagem(msg, self.CONTATO, "resumo"))

    def test_sem_url_o_texto_nao_muda(self):
        for montar, tipo in ((pw.montar_apresentacao, "apresentacao"),
                             (pw.montar_resumo_completa, "resumo")):
            msg = montar(self.CONTATO, self._mensagens())
            self.assertNotIn("Demonstração interativa", msg)
            self.assertIsNone(pw.validar_mensagem(msg, self.CONTATO, tipo))

    def test_pdf_padrao_resolvido_ao_lado_do_script(self):
        pdf = pw.demonstracao_pdf(self._mensagens())
        self.assertIsNotNone(pdf)
        self.assertEqual(pdf.name, "demonstracao.pdf")
        self.assertTrue(pdf.is_file())

    def test_pdf_desativado_ou_inexistente_gera_none(self):
        self.assertIsNone(pw.demonstracao_pdf(self._mensagens(pdf="")))
        self.assertIsNone(pw.demonstracao_pdf(
            self._mensagens(pdf="nao_existe.pdf")))

    def test_sem_chave_demonstracao_nao_quebra(self):
        m = dict(pw.carregar_mensagens())
        m.pop("demonstracao", None)
        self.assertIsNone(pw.demonstracao_pdf(m))
        self.assertNotIn("Demonstração interativa",
                         pw.montar_resumo_completa(self.CONTATO, m))


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
