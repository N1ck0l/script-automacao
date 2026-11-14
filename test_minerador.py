#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do minerador Google Maps: parser de endereço, avaliações,
preparação de registros e integração ao CSV (dedup por telefone).

Rode com a biblioteca padrão (sem dependências):
    python3 -m unittest test_minerador -v
"""

import io
import tempfile
from contextlib import redirect_stdout
import unittest
from pathlib import Path

import minerador_maps as mm
import prospeccao_whatsapp as pw


class TestUrlBusca(unittest.TestCase):
    def test_url_com_codificacao_e_pt_br(self):
        url = mm.montar_url_busca("clinica odontologica em Aracaju SE")
        self.assertTrue(url.startswith(
            "https://www.google.com/maps/search/clinica%20odontologica"))
        self.assertIn("hl=pt-BR", url)

    def test_url_com_acentos_e_simbolos(self):
        url = mm.montar_url_busca("clínica médica em Aracaju/SE")
        self.assertIn("%C3%A9", url)  # é codificado
        self.assertNotIn(" ", url)


class TestExtrairBairro(unittest.TestCase):
    CIDADE = "Aracaju"

    def test_formato_padrao_com_cep_e_brasil(self):
        end = ("Av. Marechal Rondon, 1470 - Centro - Aracaju - SE, "
               "49010-000 - Brasil")
        self.assertEqual(mm.extrair_bairro(end, self.CIDADE), "Centro")

    def test_bairro_com_acento(self):
        end = ("Rua Santo Amaro, 35 - São José - Aracaju - SE, "
               "49010-000 - Brasil")
        self.assertEqual(mm.extrair_bairro(end, self.CIDADE), "São José")

    def test_endereco_sem_travessao_antes_do_bairro(self):
        end = "Ponta d'Areia, Aracaju - SE, 49030-000 - Brasil"
        self.assertEqual(mm.extrair_bairro(end, self.CIDADE), "Ponta d'Areia")

    def test_bairro_virgula_antes_da_cidade(self):
        end = ("Rua das Flores, 123 - Bairro Centro, Aracaju - SE, "
               "49000-000 - Brasil")
        self.assertEqual(mm.extrair_bairro(end, self.CIDADE), "Bairro Centro")

    def test_endereco_vazio_retorna_vazio(self):
        self.assertEqual(mm.extrair_bairro(""), "")
        self.assertEqual(mm.extrair_bairro(None), "")


class TestExtrairAvaliacoes(unittest.TestCase):
    def test_nota_e_quantidade(self):
        self.assertEqual(mm.extrair_avaliacoes("4,8\n(1.234)"),
                         "4.8 estrelas (1234 avaliações)")

    def test_apenas_quantidade(self):
        self.assertEqual(mm.extrair_avaliacoes("127 avaliações"),
                         "127 avaliações")

    def test_apenas_nota_com_estrelas(self):
        self.assertEqual(mm.extrair_avaliacoes("4,9 estrelas de 5"),
                         "4.9 estrelas")

    def test_sem_dados(self):
        self.assertEqual(mm.extrair_avaliacoes(""), "")
        self.assertEqual(mm.extrair_avaliacoes("Segunda a sábado, 8h às 18h"),
                         "")

    def test_preco_nao_vira_nota(self):
        # Preço solto no texto não deve virar "nota"
        self.assertEqual(mm.extrair_avaliacoes("Consulta R$ 89,90"), "")


class TestPrepararRegistro(unittest.TestCase):
    def test_registro_completo_com_status_pendente(self):
        reg = mm.preparar_novo_registro({
            "clinica": "Ferraz Odontologia",
            "bairro": "São José",
            "telefone": "(79) 99963-1871",
            "site": "https://ferrazodonto.com.br",
            "avaliacoes": "4.8 estrelas (1234 avaliações)",
        })
        self.assertEqual(reg["telefone"], "5579999631871")  # higienizado +55
        self.assertEqual(reg["status"], "pendente")
        self.assertEqual(reg["cidade"], "Aracaju")
        self.assertEqual(list(reg.keys()), pw.COLUNAS)  # esquema do envio
        self.assertEqual(reg["segmento"], "odonto")  # inferido do nome

    def test_segmento_vem_do_termo_de_busca(self):
        reg = mm.preparar_novo_registro(
            {"clinica": "Clínica Genérica", "telefone": "(79) 99963-1871"},
            segmento=pw.inferir_segmento("clinica medica em Aracaju SE"))
        self.assertEqual(reg["segmento"], "medico")

    def test_segmento_sem_sinal_fica_vazio(self):
        reg = mm.preparar_novo_registro(
            {"clinica": "Clínica X", "telefone": "(79) 99963-1871"})
        self.assertEqual(reg["segmento"], "")

    def test_rejeita_telefone_invalido(self):
        reg = mm.preparar_novo_registro({"clinica": "Clínica X",
                                         "telefone": "12345"})
        self.assertIsNone(reg)

    def test_rejeita_sem_telefone(self):
        self.assertIsNone(mm.preparar_novo_registro({"clinica": "Clínica X"}))

    def test_rejeita_sem_nome(self):
        self.assertIsNone(mm.preparar_novo_registro({"clinica": "  ",
                                                     "telefone": "79999631871"}))


class TestMesclarRegistros(unittest.TestCase):
    def test_dedup_por_telefone(self):
        existentes = [{"clinica": "Já existente", "telefone": "5579991111111"}]
        novos = [
            {"clinica": "Nova", "telefone": "5579992222222"},
            {"clinica": "Duplicada", "telefone": "5579991111111"},  # já existe
        ]
        inseridos, pulados = mm.mesclar_registros(existentes, novos)
        self.assertEqual(len(inseridos), 1)
        self.assertEqual(inseridos[0]["clinica"], "Nova")
        self.assertEqual(pulados, 1)

    def test_registro_existente_prevalece(self):
        existentes = [{"clinica": "Original", "telefone": "5579991111111",
                       "status": "enviado"}]
        novos = [{"clinica": "Nova versão", "telefone": "5579991111111"}]
        inseridos, pulados = mm.mesclar_registros(existentes, novos)
        self.assertEqual(inseridos, [])
        self.assertEqual(pulados, 1)
        self.assertEqual(existentes[0]["status"], "enviado")  # nada sobrescrito


class TestIntegrarCsv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self._tmp.name) / "prospeccao.csv"

    def tearDown(self):
        self._tmp.cleanup()

    def _novo(self, tel, nome="Clínica"):
        return {"clinica": nome, "bairro": "Centro", "telefone": tel,
                "site": "", "avaliacoes": ""}

    def test_cria_csv_novo_com_cabecalho_do_envio(self):
        inseridos, pulados = mm.integrar_csv([self._novo("79999631871")],
                                             self.csv)
        self.assertEqual((inseridos, pulados), (1, 0))
        cabecalho = self.csv.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(cabecalho, ",".join(pw.COLUNAS))

    def test_nao_sobrescreve_duplicados_por_telefone(self):
        # Primeira mineração
        mm.integrar_csv([self._novo("79999631871", "Original")], self.csv)
        # Segunda mineração traz o mesmo número com outro nome
        inseridos, pulados = mm.integrar_csv(
            [self._novo("79999631871", "Renomeada"),
             self._novo("79991234567", "Nova")], self.csv)

        self.assertEqual((inseridos, pulados), (1, 1))
        conteudo = self.csv.read_text(encoding="utf-8")
        self.assertIn("Original", conteudo)     # registro prévio preservado
        self.assertNotIn("Renomeada", conteudo)  # duplicata não entra
        self.assertIn("Nova", conteudo)

    def test_integra_com_csv_do_envio_sem_quebrar_colunas(self):
        # CSV do script de envio já existe (mesmo esquema)
        pw.salvar_contatos(self.csv, [{
            "clinica": "Ferraz Odontologia", "bairro": "São José",
            "cidade": "Aracaju", "telefone": "5579999631871", "site": "",
            "avaliacoes": "", "status": "enviado", "resposta": "respondeu",
            "atualizado_em": "2026-11-09T10:00:00",
        }])
        inseridos, pulados = mm.integrar_csv(
            [self._novo("79999631871"),   # duplicata da Ferraz
             self._novo("79999888777")], self.csv)

        self.assertEqual((inseridos, pulados), (1, 1))
        linhas = self.csv.read_text(encoding="utf-8").splitlines()
        self.assertEqual(linhas[0], ",".join(pw.COLUNAS))
        self.assertEqual(len(linhas), 3)  # cabeçalho + Ferraz + nova
        self.assertIn("respondeu", linhas[1])  # dado de envio intacto

    def test_csv_so_com_cabecalho_nao_injeta_contatos_ficticios(self):
        self.csv.write_text(",".join(pw.COLUNAS) + "\n", encoding="utf-8")
        inseridos, _ = mm.integrar_csv([self._novo("79999631871", "Real")],
                                       self.csv)
        linhas = self.csv.read_text(encoding="utf-8").splitlines()
        self.assertEqual(inseridos, 1)
        self.assertEqual(len(linhas), 2)  # cabeçalho + a nova, nada mais

    def test_preserva_linhas_invalidas_existentes(self):
        self.csv.write_text(
            ",".join(pw.COLUNAS) + "\n"
            "Sem Telefone,Centro,Aracaju,,,,pendente,,,,\n",
            encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            mm.integrar_csv([self._novo("79999631871", "Nova")], self.csv)
        conteudo = self.csv.read_text(encoding="utf-8")
        self.assertIn("Sem Telefone", conteudo)
        self.assertIn("Nova", conteudo)


class TestRodizio(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.termos = self.dir / "termos.txt"
        self.estado = self.dir / ".rodizio"

    def tearDown(self):
        self._tmp.cleanup()

    def test_le_termos_ignorando_comentarios(self):
        self.termos.write_text("# comentário\nA\n\n  B  \n# outro\n", encoding="utf-8")
        self.assertEqual(mm.ler_termos(self.termos), ["A", "B"])

    def test_rodizio_avanca_e_volta_ao_inicio(self):
        termos = ["A", "B", "C"]
        vistos = []
        for _ in range(4):
            termo, i = mm.proximo_termo(termos, self.estado)
            vistos.append(termo)
            mm.avancar_rodizio(i, self.estado)
        self.assertEqual(vistos, ["A", "B", "C", "A"])

    def test_termos_reais_nao_buscam_concorrentes_de_imagem(self):
        for termo in mm.ler_termos():
            for proibido in ("imagem", "diagnost", "radiolog", "ultrass", "tomograf",
                             "resson", "proimagem"):
                self.assertNotIn(proibido, termo.lower(), termo)


class TestExtrairHorarios(unittest.TestCase):
    def test_extrai_dias_e_sabado(self):
        bruto = (
            "segunda-feira, 08:00–18:00; terça-feira, 08:00–18:00; "
            "sábado, 08:00–12:00; domingo, Fechado"
        )
        res = mm.extrair_horarios(bruto)
        self.assertIn("Seg: 08:00–18:00", res)
        self.assertIn("Sáb: 08:00–12:00", res)
        self.assertIn("Dom: Fechado", res)

    def test_sem_horarios_retorna_vazio(self):
        self.assertEqual(mm.extrair_horarios("Rua X, 123"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
