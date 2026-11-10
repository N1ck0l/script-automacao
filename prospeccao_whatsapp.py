#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prospecção B2B semi-supervisionada (human-in-the-loop) via WhatsApp Web
para clínicas odontológicas e médicas locais.

PRINCÍPIOS ANTI-BAN (Meta):
  1. NENHUM envio acontece sem confirmação explícita humana (ENTER/`s`).
  2. Limite diário rígido de mensagens (padrão: 20/dia).
  3. Delay aleatório obrigatório entre envios (45–90s, com contagem regressiva).
  4. Spintax + personalização por clínica/bairro => nenhuma mensagem idêntica.
  5. Todo envio é registrado em `historico_envios.jsonl` para auditoria.

MODOS DE ENVIO (--modo):
  manual (padrão) : abre https://web.whatsapp.com/send?phone=...&text=... no
                    navegador; VOCÊ revisa e clica em enviar no WhatsApp Web.
  web             : automatiza o WhatsApp Web via Selenium, digita a mensagem,
                    PARA e só envia após você digitar "ENVIAR" no terminal.
  preview         : dry-run — apenas gera prévias, nunca abre navegador.

USO:
  python prospeccao_whatsapp.py                 # fila interativa (recomendado)
  python prospeccao_whatsapp.py --listar        # lista os contatos
  python prospeccao_whatsapp.py --preview       # só gera prévias (dry-run)
  python prospeccao_whatsapp.py --modo web      # com automação do WhatsApp Web
  python prospeccao_whatsapp.py --reset         # reabre a fila inteira
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# ---------------------------------------------------------------------------
# 1. FONTES DE DADOS
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ARQUIVO_CSV = BASE_DIR / "prospeccao.csv"
ARQUIVO_LOG = BASE_DIR / "historico_envios.jsonl"
PERFIL_SELENIUM = BASE_DIR / ".perfil_whatsapp_web"

COLUNAS = ["clinica", "bairro", "cidade", "telefone", "site", "avaliacoes",
           "status", "resposta", "atualizado_em"]

# Fallback mockado caso prospeccao.csv não exista
LISTA_MOCKADA = [
    {"clinica": "Ferraz Odontologia", "bairro": "São José", "cidade": "Aracaju",
     "telefone": "5579999631871", "site": "", "avaliacoes": "",
     "status": "pendente", "resposta": "", "atualizado_em": ""},
    {"clinica": "Clínica Vida Nova", "bairro": "Atalaia", "cidade": "Aracaju",
     "telefone": "5579991234567", "site": "", "avaliacoes": "",
     "status": "pendente", "resposta": "", "atualizado_em": ""},
    {"clinica": "Odonto Sorriso", "bairro": "Evangelista", "cidade": "Aracaju",
     "telefone": "5579998765432", "site": "", "avaliacoes": "",
     "status": "pendente", "resposta": "", "atualizado_em": ""},
    {"clinica": "Centro Médico Aracaju", "bairro": "Ponta d'Areia", "cidade": "Aracaju",
     "telefone": "5579994561230", "site": "", "avaliacoes": "",
     "status": "pendente", "resposta": "", "atualizado_em": ""},
    {"clinica": "Instituto São Lucas", "bairro": "Grageru", "cidade": "Aracaju",
     "telefone": "5579993220987", "site": "", "avaliacoes": "",
     "status": "pendente", "resposta": "", "atualizado_em": ""},
]


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


def carregar_contatos(caminho: Path) -> list[dict]:
    """Carrega o CSV; valida telefones e remove duplicatas (anti reenvio)."""
    if not caminho.exists():
        print(f"[AVISO] '{caminho.name}' não encontrado — usando lista mockada.\n")
        return [dict(c) for c in LISTA_MOCKADA]

    contatos: list[dict] = []
    vistos: set[str] = set()
    with caminho.open(newline="", encoding="utf-8-sig") as f:
        for i, linha in enumerate(csv.DictReader(f), start=2):
            contato = {c: (linha.get(c) or "").strip() for c in COLUNAS}
            if not contato["telefone"]:
                print(f"[AVISO] Linha {i}: telefone vazio — ignorada.")
                continue
            contato["telefone"] = normalizar_telefone(contato["telefone"])
            if not validar_telefone(contato["telefone"]):
                print(f"[AVISO] Linha {i}: telefone inválido "
                      f"('{contato['telefone']}') — ignorado.")
                continue
            if contato["telefone"] in vistos:
                print(f"[AVISO] Linha {i}: telefone duplicado "
                      f"({contato['telefone']}) — ignorado (anti reenvio).")
                continue
            vistos.add(contato["telefone"])
            contato["status"] = contato["status"].lower() or "pendente"
            contato["resposta"] = contato["resposta"].lower()
            contato.setdefault("cidade", "Aracaju")
            contatos.append(contato)

    if not contatos:
        print("[AVISO] CSV vazio — usando lista mockada.\n")
        return [dict(c) for c in LISTA_MOCKADA]
    return contatos


def salvar_contatos(caminho: Path, contatos: list[dict]) -> None:
    """Grava o CSV completo (status + timestamp) de forma atômica."""
    temporario = caminho.with_suffix(".csv.tmp")
    with temporario.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUNAS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(contatos)
    temporario.replace(caminho)  # evita corromper o CSV se o processo cair no meio


# ---------------------------------------------------------------------------
# 2. SPINTAX — variação de mensagens (anti-spam)
# ---------------------------------------------------------------------------

SAUDACOES = [
    "Olá, tudo bem?",
    "Boa tarde! Tudo bem?",
    "Olá! Como vai?",
]

GANCHOS = [
    "Acompanho o trabalho da {clinica} no {bairro} e sei que manter a recepção "
    "ágil com alto volume de pacientes é um desafio diário.",
    "Tenho visto o crescimento da {clinica} aqui no {bairro} e a referência "
    "que vocês são em atendimento na região.",
]

AUTORIDADES = [
    "Meu nome é Nickolas, atuo com automações de tecnologia para a área da "
    "saúde aqui em {cidade}.",
    "Sou Nickolas e atuo com automações de tecnologia para a área da saúde "
    "aqui em {cidade}.",
]

CASES = [
    "Recentemente desenvolvi para a clínica PROIMAGEM duas automações de alto "
    "impacto: 1) Um bot de pré-atendimento no WhatsApp que colhe todas as "
    "informações da queixa do paciente e entrega para a recepcionista uma ficha "
    "resumida pronta; 2) Uma automação fiscal que reduziu o tempo de emissão de "
    "NF-e para poucos minutos com validação em 1 clique.",
    "Recentemente desenvolvi para a clínica PROIMAGEM duas automações de alto "
    "impacto: 1) um bot de pré-atendimento no WhatsApp que colhe a queixa do "
    "paciente e entrega à recepcionista uma ficha resumida pronta; e 2) uma "
    "automação fiscal que deixou a emissão de NF-e em poucos minutos, com "
    "validação em 1 clique.",
]

CTAS = [
    "Gostaria de enviar uma prévia rápida de 2 minutos ou agendar 10 minutos "
    "para mostrar ao gestor/responsável da clínica como isso rodaria aí. Quem "
    "seria o contato indicado para alinharmos?",
    "Posso te mandar uma prévia rápida de 2 minutos, ou agendar 10 minutos "
    "para mostrar ao gestor/responsável da clínica como isso rodaria aí? Quem "
    "seria o contato indicado para alinharmos?",
]

# A ordem/quebra de linha da mensagem também varia
ESTRUTURAS = [
    "{saudacao}\n\n{gancho}\n\n{autoridade}\n\n{case}\n\n{cta}",
    "{saudacao} {gancho}\n\n{autoridade}\n\n{case}\n\n{cta}",
    "{saudacao}\n\n{gancho}\n\n{autoridade} {case}\n\n{cta}",
    "{saudacao}\n\n{gancho}\n\n{autoridade}\n\n{case}\n\n{cta} 😊",
]


def resolver_spintax(texto: str, contexto: dict) -> str:
    """Resolve `{a|b}` (escolha aleatória) e tokens `{clinica}`/`{bairro}`/`{cidade}`.

    Tokens desconhecidos são preservados usando um placeholder temporário —
    sem ele, substituir pelo próprio texto causaria loop infinito.
    """
    padrao = re.compile(r"\{([^{}]*)\}")
    preservados: dict[str, str] = {}

    def substituir(m: re.Match) -> str:
        conteudo = m.group(1)
        if "|" in conteudo:
            return random.choice(conteudo.split("|"))
        if conteudo in contexto:
            return str(contexto[conteudo])
        placeholder = f"\x00{len(preservados)}\x00"  # sem chaves: não rematacha
        preservados[placeholder] = m.group(0)
        return placeholder

    while padrao.search(texto):
        texto = padrao.sub(substituir, texto, count=1)

    for placeholder, original in preservados.items():
        texto = texto.replace(placeholder, original)
    return texto


def montar_texto(contato: dict) -> str:
    """Monta uma mensagem única e personalizada para o contato."""
    contexto = {
        "clinica": contato["clinica"],
        "bairro": contato["bairro"],
        "cidade": contato.get("cidade") or "Aracaju",
    }
    rascunho = random.choice(ESTRUTURAS).format(
        saudacao=random.choice(SAUDACOES),
        gancho=random.choice(GANCHOS),
        autoridade=random.choice(AUTORIDADES),
        case=random.choice(CASES),
        cta=random.choice(CTAS),
    )
    return resolver_spintax(rascunho, contexto)


def montar_link(contato: dict, mensagem: str) -> str:
    """Codifica a mensagem e monta o link do WhatsApp Web."""
    codificada = quote(mensagem, safe="")
    return (f"https://web.whatsapp.com/send?phone={contato['telefone']}"
            f"&text={codificada}")


# ---------------------------------------------------------------------------
# 3. CONTROLE DE VOLUME (anti-ban) E HISTÓRICO
# ---------------------------------------------------------------------------

def ler_historico() -> list[dict]:
    if not ARQUIVO_LOG.exists():
        return []
    registros = []
    with ARQUIVO_LOG.open(encoding="utf-8") as f:
        for linha in f:
            try:
                registros.append(json.loads(linha))
            except json.JSONDecodeError:
                continue
    return registros


def envios_hoje() -> int:
    hoje = datetime.now().date().isoformat()
    return sum(1 for r in ler_historico()
               if str(r.get("timestamp", "")).startswith(hoje))


def enviado_hoje(telefone: str) -> str | None:
    """Retorna o timestamp do envio de HOJE para este número, se houver."""
    hoje = datetime.now().date().isoformat()
    for r in ler_historico():
        if (r.get("telefone") == telefone
                and str(r.get("timestamp", "")).startswith(hoje)):
            return str(r.get("timestamp"))
    return None


def dentro_da_janela(inicio: int, fim: int, agora: datetime | None = None) -> bool:
    """True se 'agora' estiver dentro da janela de horário comercial."""
    hora = (agora or datetime.now()).hour
    return inicio <= hora < fim


def atualizar_historico(contato: dict, mensagem: str, modo: str) -> None:
    """Anexa o envio ao log JSONL (auditoria) e devolve o timestamp."""
    registro = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "clinica": contato["clinica"],
        "bairro": contato["bairro"],
        "telefone": contato["telefone"],
        "modo": modo,
        "mensagem": mensagem,
    }
    with ARQUIVO_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def marcar_enviado(contato: dict) -> None:
    """Atualiza status no CSV para 'enviado' com timestamp."""
    contato["status"] = "enviado"
    contato["resposta"] = contato.get("resposta") or "aguardando"
    contato["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    salvar_contatos(CAMINHO_CSV_ATIVO, CONTATOS_GLOBAIS)


def verificar_limite_diario(limite: int) -> bool:
    usados = envios_hoje()
    print(f"[LIMITE DIÁRIO] {usados}/{limite} mensagens enviadas hoje.")
    if usados >= limite:
        print("[BLOQUEADO] Limite diário atingido. Retome amanhã — "
              "volume alto é o principal gatilho de banimento pela Meta.")
        return False
    return True


def contagem_regressiva(segundos: int) -> None:
    """Contagem regressiva amigável exibida entre os envios."""
    print(f"\n[COOLDOWN] Próxima abertura liberada em {segundos}s "
          f"(delay humano obrigatório, anti-ban):")
    for restante in range(segundos, 0, -1):
        minutos, segs = divmod(restante, 60)
        relogio = f"{minutos:02d}:{segs:02d}"
        print(f"\r  ⏳ {relogio} — Ctrl+C para interromper (o progresso é salvo)  ",
              end="", flush=True)
        time.sleep(1)
    print("\r  ✅ Cooldown concluído. Pode seguir para o próximo contato.       ")


def delay_obrigatorio(intervalo: tuple[int, int]) -> None:
    """Aplica time.sleep(random.randint(45, 90)) com contagem regressiva."""
    espera = random.randint(*intervalo)
    contagem_regressiva(espera)


# ---------------------------------------------------------------------------
# 4. MODOS DE ENVIO (sempre com confirmação humana)
# ---------------------------------------------------------------------------

def enviar_modo_manual(contato: dict, mensagem: str) -> bool:
    """Abre o link do WhatsApp Web no navegador padrão e pausa para o humano.

    A prévia do texto já foi exibida pelo orquestrador (prévia única);
    aqui ficam apenas o cabeçalho do alvo e o link codificado.
    """
    link = montar_link(contato, mensagem)

    print("\n" + "=" * 64)
    print(f"[ALVO] {contato['clinica']} — {contato['bairro']}, "
          f"{contato.get('cidade', 'Aracaju')}")
    print(f"[TEL ] +{contato['telefone']}")
    print(f"[LINK] {link}")
    print("=" * 64)

    if input("\nAbrir no WhatsApp Web? [s/N]: ").strip().lower() != "s":
        print("[SKIP] Envio cancelado pelo operador.")
        return False

    try:
        aberto = webbrowser.open(link)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERRO] Não foi possível abrir o navegador: {exc}")
        return False
    if not aberto:
        print("[ERRO] Nenhum navegador padrão encontrado para abrir o link.")
        print(f"       Copie e abra manualmente:\n       {link}")
        return False

    # Pausa obrigatória: humano revisa e envia no WhatsApp Web
    input("Pressione ENTER após revisar e enviar a mensagem no WhatsApp Web... ")

    if input("A mensagem foi enviada? [s/N]: ").strip().lower() != "s":
        print("[INFO] Não registrado como enviado — o contato segue pendente.")
        return False
    return True


def enviar_modo_web(contato: dict, mensagem: str) -> bool:
    """Automação do WhatsApp Web (Selenium) COM confirmação humana final."""
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        print("[ERRO] Selenium não instalado. Rode: pip install -r requirements.txt")
        return False

    print("\n" + "=" * 64)
    print(f"[ALVO] {contato['clinica']} — {contato['bairro']}, "
          f"{contato.get('cidade', 'Aracaju')}")
    print(f"[TEL ] +{contato['telefone']}")
    print("=" * 64)
    print("\n[PRÉ-VIA DA MENSAGEM]")
    print("-" * 64)
    print(mensagem)
    print("-" * 64)

    if input("\nPreparar o envio no WhatsApp Web (Selenium)? [s/N]: ").strip().lower() != "s":
        print("[SKIP] Envio cancelado pelo operador.")
        return False

    opcoes = webdriver.ChromeOptions()
    opcoes.add_argument("--start-maximized")
    opcoes.add_argument(f"--user-data-dir={PERFIL_SELENIUM}")  # mantém a sessão aberta
    driver = webdriver.Chrome(options=opcoes)
    wait = WebDriverWait(driver, 45)

    try:
        link = montar_link(contato, mensagem)
        driver.get(link)
        print("\n[AGUARDE] Escaneie o QR Code se necessário (o perfil salvo "
              "acelera nas próximas vezes)...")

        # Espera a conversa abrir (indica login concluído)
        caixa_msg = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR,
            'footer div[contenteditable="true"], '
            'div[contenteditable="true"][data-tab="10"], '
            'div[contenteditable="true"][data-tab="6"]'
        )))
        time.sleep(2)
        caixa_msg.click()
        for parte in mensagem.split("\n"):
            caixa_msg.send_keys(parte)
            caixa_msg.send_keys(Keys.SHIFT + Keys.ENTER)
        caixa_msg.send_keys(Keys.BACKSPACE)  # remove quebra extra no fim

        print("\n" + "=" * 64)
        print("MENSAGEM DIGITADA NA CONVERSA, MAS *NÃO* ENVIADA.")
        print("Confira no navegador. Só envie se você revisar o texto.")
        print("=" * 64)
        if input("ENVIAR AGORA? Digite 'ENVIAR' (ou qualquer outra coisa "
                 "para abortar): ").strip() != "ENVIAR":
            print("[SKIP] Envio abortado pelo operador (nada foi enviado).")
            return False

        caixa_msg.send_keys(Keys.ENTER)
        time.sleep(2)
        print("[OK] Mensagem enviada via WhatsApp Web.")
        return True

    except Exception as exc:  # noqa: BLE001 - seletores da Meta mudam com frequência
        print(f"[ERRO] Falha na automação do WhatsApp Web: {exc}")
        print("       Alternativa: use --modo manual")
        return False
    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# 5. ORQUESTRAÇÃO — FLUXO SEMI-SUPERVISIONADO
# ---------------------------------------------------------------------------

CONTATOS_GLOBAIS: list[dict] = []
CAMINHO_CSV_ATIVO: Path = ARQUIVO_CSV


def exibir_contatos(contatos: list[dict]) -> None:
    print(f"\n{'#':>3} | {'CLÍNICA':<28} | {'BAIRRO':<14} | {'TELEFONE':<15} | "
          f"{'STATUS':<10} | RESPOSTA")
    print("-" * 100)
    for i, c in enumerate(contatos, start=1):
        print(f"{i:>3} | {c['clinica'][:28]:<28} | {c['bairro'][:14]:<14} | "
              f"{c['telefone']:<15} | {c['status']:<10} | "
              f"{c.get('resposta') or '-'}")


def gerenciar_respostas(contatos: list[dict]) -> None:
    """Fecha o ciclo human-in-the-loop: registra quem respondeu ao abordagem."""
    enviados = [c for c in contatos if c["status"] == "enviado"]
    if not enviados:
        print("\nNenhum contato com status 'enviado' para acompanhar.")
        return

    print(f"\n[RESPOSTAS] {len(enviados)} contato(s) aguardando acompanhamento.")
    alterados = 0
    for c in enviados:
        print("\n" + "-" * 64)
        print(f"{c['clinica']} — {c['bairro']} | +{c['telefone']} | "
              f"enviado em {c.get('atualizado_em') or '?'} | "
              f"resposta atual: {c.get('resposta') or '-'}")
        acao = input("Respondeu? [s]im / [n]ão / [ENTER] manter: ").strip().lower()
        if acao == "s":
            c["resposta"] = "respondeu"
            alterados += 1
        elif acao == "n":
            c["resposta"] = "sem resposta"
            alterados += 1

    if alterados:
        salvar_contatos(CAMINHO_CSV_ATIVO, CONTATOS_GLOBAIS)
        print(f"\n[OK] {alterados} resposta(s) atualizada(s) em "
              f"{CAMINHO_CSV_ATIVO.name}.")
    else:
        print("\n[INFO] Nenhuma resposta alterada.")


def rodar_fila(contatos: list[dict], args: argparse.Namespace) -> None:
    """Fila: monta texto -> link -> prévia -> confirmação -> delay -> próximo."""
    modo = args.modo
    # Requisito: iterar APENAS sobre contatos com status == 'pendente'
    pendentes = [c for c in contatos if c["status"] == "pendente"]

    if not pendentes:
        print("\nNenhum contato pendente. Use --reset para reabrir a fila.")
        return

    # Janela de horário comercial (disparar de madrugada = padrão de robô)
    if modo != "preview" and not dentro_da_janela(args.hora_inicio, args.hora_fim):
        hora = datetime.now().strftime("%H:%M")
        print(f"\n[BLOQUEADO] Fora da janela de horário ({hora}; "
              f"permitido: {args.hora_inicio:02d}h–{args.hora_fim}h). "
              "Prospecção fora do comercial parece automação — retome dentro "
              "da janela ou use --preview para apenas ver prévias.")
        return

    # Salto opcional para um contato específico (1-based)
    inicio = 0
    if args.contato:
        if not 1 <= args.contato <= len(pendentes):
            print(f"\n[ERRO] --contato {args.contato} fora do intervalo "
                  f"(1..{len(pendentes)} pendentes).")
            return
        inicio = args.contato - 1
        print(f"\n[SELEÇÃO] Iniciando pelo contato #{args.contato} da fila.")

    print(f"\n[FILA] {len(pendentes) - inicio} contato(s) pendente(s). "
          f"Modo: {modo.upper()} (sempre com sua confirmação).")

    indice = inicio
    while indice < len(pendentes):
        contato = pendentes[indice]

        if not verificar_limite_diario(args.limite_dia):
            break

        # Anti reenvio: este número já recebeu mensagem hoje?
        if modo != "preview":
            ts = enviado_hoje(contato["telefone"])
            if ts:
                print(f"\n[ALERTA] {contato['clinica']} (+{contato['telefone']}) "
                      f"JÁ recebeu mensagem hoje às "
                      f"{ts[11:16] if len(ts) >= 16 else ts}.")
                escolha = input("Enviar novamente mesmo assim? [s/N]: ").strip().lower()
                if escolha != "s":
                    print("[SKIP] Reenvio evitado (anti-ban). Contato segue "
                          "pendente para os próximos dias.")
                    indice += 1
                    continue

        # Delay aleatório obrigatório ANTES de liberar a próxima abertura
        # (no primeiro envio do dia não há espera, pois nunca houve envio)
        if envios_hoje() > 0:
            delay_obrigatorio((args.intervalo_min, args.intervalo_max))

        print("\n" + "=" * 64)
        print(f"[PRÓXIMO] {contato['clinica']} — {contato['bairro']}, "
              f"{contato.get('cidade', 'Aracaju')} | +{contato['telefone']}")
        print("=" * 64)

        mensagem = montar_texto(contato)

        if modo == "preview":
            print("\n[PRÉ-VIA — dry-run, nada será enviado]")
            print("-" * 64)
            print(mensagem)
            print("-" * 64)
            acao = input("\n[ENTER] nova variação | [p]ular | [q] sair: ").strip().lower()
            if acao == "q":
                break
            if acao == "p":
                indice += 1
            continue  # ENTER => gera nova variação de spintax

        # Prévia sempre aparece antes de qualquer envio
        print("\n[PRÉ-VIA]")
        print("-" * 64)
        print(mensagem)
        print("-" * 64)
        acao = input("\n[A]provar envio | [v] nova variação | [p]ular | "
                     "[e]ditar texto | [q] sair: ").strip().lower()

        if acao == "q":
            break
        if acao == "p":
            indice += 1
            continue
        if acao == "v":
            continue  # refaz o loop => nova variação
        if acao == "e":
            print("Cole/edite a mensagem final (linha vazia para encerrar):")
            linhas = []
            while True:
                linha = input()
                if not linha:
                    break
                linhas.append(linha)
            if linhas:
                mensagem = "\n".join(linhas)
            if input("\nEnviar esta versão editada? [s/N]: ").strip().lower() != "s":
                continue
        elif acao != "a":
            print("[INFO] Ação não reconhecida — nada foi enviado.")
            continue

        if not verificar_limite_diario(args.limite_dia):
            break

        if modo == "web":
            sucesso = enviar_modo_web(contato, mensagem)
        else:
            sucesso = enviar_modo_manual(contato, mensagem)

        if sucesso:
            atualizar_historico(contato, mensagem, modo)
            marcar_enviado(contato)  # status='enviado' + timestamp no CSV
            print(f"[OK] {contato['clinica']} marcado como 'enviado' "
                  f"({contato['atualizado_em']}).")
            indice += 1
        else:
            print("[RETIDO] Contato continua 'pendente'.")
            if input("Sair da fila? [s/N]: ").strip().lower() == "s":
                break

    print(f"\n[FIM] Sessão encerrada. Progresso salvo em "
          f"{CAMINHO_CSV_ATIVO.name} e {ARQUIVO_LOG.name}.")


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------

def main() -> None:
    global CONTATOS_GLOBAIS, CAMINHO_CSV_ATIVO

    parser = argparse.ArgumentParser(
        description="Prospecção B2B human-in-the-loop para clínicas (WhatsApp)."
    )
    parser.add_argument("--arquivo", default=str(ARQUIVO_CSV),
                        help="Caminho do CSV de prospecção (padrão: prospeccao.csv)")
    parser.add_argument("--modo", choices=["manual", "web", "preview"],
                        default="manual",
                        help="manual=link WhatsApp Web (padrão) | web=Selenium | preview=dry-run")
    parser.add_argument("--limite-dia", type=int, default=20,
                        help="Máximo de mensagens por dia (padrão: 20, anti-ban)")
    parser.add_argument("--intervalo-min", type=int, default=45,
                        help="Delay mínimo entre envios em segundos (padrão: 45)")
    parser.add_argument("--intervalo-max", type=int, default=90,
                        help="Delay máximo entre envios em segundos (padrão: 90)")
    parser.add_argument("--listar", action="store_true", help="Apenas lista os contatos")
    parser.add_argument("--preview", action="store_true",
                        help="Gera prévias sem enviar (dry-run)")
    parser.add_argument("--respostas", action="store_true",
                        help="Acompanha quem respondeu (fecha o ciclo de prospecção)")
    parser.add_argument("--contato", type=int, default=None,
                        help="Inicia a fila a partir do N-ésimo contato pendente (1-based)")
    parser.add_argument("--hora-inicio", type=int, default=8,
                        help="Início da janela de envio, hora inteira (padrão: 8)")
    parser.add_argument("--hora-fim", type=int, default=19,
                        help="Fim da janela de envio, hora inteira (padrão: 19)")
    parser.add_argument("--status", default=None,
                        help="Filtra a fila pela clínica (substring, ex.: 'Ferraz')")
    parser.add_argument("--reset", action="store_true",
                        help="Marca todos os contatos como 'pendente'")
    args = parser.parse_args()

    if args.preview:
        args.modo = "preview"

    caminho = Path(args.arquivo)
    CAMINHO_CSV_ATIVO = caminho
    contatos = carregar_contatos(caminho)
    CONTATOS_GLOBAIS = contatos

    if args.reset:
        for c in contatos:
            c["status"] = "pendente"
            c["resposta"] = ""
            c["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
        salvar_contatos(caminho, contatos)
        print(f"[RESET] {len(contatos)} contato(s) marcado(s) como 'pendente'.")
        return

    if args.status:
        filtro = args.status.lower()
        contatos = [c for c in contatos if filtro in c["clinica"].lower()]
        if not contatos:
            print(f"[ERRO] Nenhum contato corresponde a '{args.status}'.")
            sys.exit(1)

    if args.respostas:
        try:
            gerenciar_respostas(contatos)
        except (KeyboardInterrupt, EOFError):
            salvar_contatos(CAMINHO_CSV_ATIVO, CONTATOS_GLOBAIS)
            print("\n[INTERRUPÇÃO] Progresso salvo.")
            sys.exit(130)
        return

    if args.listar:
        exibir_contatos(contatos)
        return

    print("=" * 64)
    print(" PROSPECÇÃO B2B — WHATSAPP (semi-supervisionada)")
    print(" Nenhum envio ocorre sem sua confirmação explícita.")
    print("=" * 64)
    exibir_contatos(contatos)

    try:
        rodar_fila(contatos, args)
    except (KeyboardInterrupt, EOFError):
        # Salva o progresso caso o usuário queira parar com Ctrl+C
        salvar_contatos(CAMINHO_CSV_ATIVO, CONTATOS_GLOBAIS)
        print("\n\n[INTERRUPÇÃO] Sessão interrompida. Progresso salvo em "
              f"{CAMINHO_CSV_ATIVO.name}. Rode o script novamente para continuar "
              "de onde parou (só 'pendentes' entram na fila).")
        sys.exit(130)


if __name__ == "__main__":
    main()
