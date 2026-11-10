#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prospecção B2B semi-supervisionada (human-in-the-loop) via WhatsApp Web
para clínicas odontológicas e médicas locais.

PRINCÍPIOS ANTI-BAN (Meta):
  1. NENHUM envio acontece sem confirmação explícita humana (ENTER/`s`).
  2. Limite diário rígido de mensagens (padrão: 20/dia).
  3. Intervalo aleatório mínimo entre envios (45–90s, com contagem regressiva).
  4. Janela de horário comercial e bloqueio de fim de semana.
  5. Spintax + personalização por clínica/bairro => nenhuma mensagem idêntica.
  6. Opt-out: quem pediu para não ser contatado (`nao_contatar`) nunca volta à fila.
  7. Todo envio é registrado em `historico_envios.jsonl` para auditoria.

MODOS DE ENVIO (--modo):
  manual (padrão) : abre https://web.whatsapp.com/send?phone=...&text=... no
                    navegador; VOCÊ revisa e clica em enviar no WhatsApp Web.
  web             : abre o WhatsApp Web via Selenium com o texto já na caixa,
                    PARA e só envia após você digitar "ENVIAR" no terminal.
  preview         : dry-run — apenas gera prévias, nunca abre navegador.

USO:
  python prospeccao_whatsapp.py                 # fila interativa (recomendado)
  python prospeccao_whatsapp.py --listar        # lista os contatos
  python prospeccao_whatsapp.py --preview       # só gera prévias (dry-run)
  python prospeccao_whatsapp.py --followup      # retoma quem não respondeu
  python prospeccao_whatsapp.py --modo web      # com automação do WhatsApp Web
  python prospeccao_whatsapp.py --reset         # reabre quem não respondeu

Os textos ficam em `mensagens.json` (spintax + tokens).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# ---------------------------------------------------------------------------
# 1. FONTES DE DADOS
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ARQUIVO_CSV = BASE_DIR / "prospeccao.csv"
ARQUIVO_LOG = BASE_DIR / "historico_envios.jsonl"
ARQUIVO_MENSAGENS = BASE_DIR / "mensagens.json"
PERFIL_SELENIUM = BASE_DIR / ".perfil_whatsapp_web"

COLUNAS = ["clinica", "bairro", "cidade", "telefone", "site", "avaliacoes",
           "status", "resposta", "atualizado_em", "ultimo_envio", "followups"]

CIDADE_PADRAO = "Aracaju"

# Status do contato
PENDENTE = "pendente"
ENVIADO = "enviado"
NAO_CONTATAR = "nao_contatar"  # opt-out: nunca volta à fila (nem com --reset)

# Respostas que ainda admitem follow-up
RESPOSTAS_EM_ABERTO = {"", "aguardando", "sem resposta"}

# Tipos de envio
INICIAL = "inicial"
FOLLOWUP = "followup"

# Exemplos para --preview quando não há CSV. Números fictícios (prefixo
# 5579900000...) — nunca usados para envio real.
LISTA_EXEMPLO = [
    {"clinica": "Clínica Exemplo Sorriso", "bairro": "Jardins", "cidade": "Aracaju",
     "telefone": "5579900000001", "status": PENDENTE},
    {"clinica": "Centro Médico Exemplo", "bairro": "", "cidade": "Aracaju",
     "telefone": "5579900000002", "status": PENDENTE},
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


def eh_celular(digitos: str) -> bool:
    """Celular = 55 + DDD + 9 + 8 dígitos. Fixos raramente têm WhatsApp."""
    return bool(re.fullmatch(r"55\d{2}9\d{8}", digitos))


def _inteiro(valor) -> int:
    try:
        return int(str(valor).strip() or 0)
    except ValueError:
        return 0


def _data(valor) -> datetime | None:
    try:
        return datetime.fromisoformat(str(valor).strip())
    except ValueError:
        return None


@dataclass
class Planilha:
    """CSV de prospecção em memória.

    `linhas` guarda TODAS as linhas do arquivo (inclusive inválidas e
    duplicadas), para que salvar nunca apague dados. `contatos` é o subconjunto
    válido que entra nas filas — são os mesmos objetos, então alterar um
    contato altera a linha que será gravada.
    """
    caminho: Path
    linhas: list[dict] = field(default_factory=list)
    contatos: list[dict] = field(default_factory=list)
    colunas: list[str] = field(default_factory=lambda: list(COLUNAS))

    def adicionar(self, registro: dict) -> None:
        self.linhas.append(registro)
        self.contatos.append(registro)

    def salvar(self) -> None:
        salvar_contatos(self.caminho, self.linhas, self.colunas)


def carregar_planilha(caminho: Path) -> Planilha:
    """Carrega o CSV. Linhas com telefone vazio, inválido ou duplicado ficam
    fora da fila (com aviso), mas continuam no arquivo."""
    caminho = Path(caminho)
    planilha = Planilha(caminho)
    if not caminho.exists():
        print(f"[AVISO] '{caminho.name}' não encontrado.")
        return planilha

    vistos: set[str] = set()
    with caminho.open(newline="", encoding="utf-8-sig") as f:
        leitor = csv.DictReader(f)
        extras = [c for c in (leitor.fieldnames or []) if c and c not in COLUNAS]
        planilha.colunas = COLUNAS + extras  # colunas extras do usuário são preservadas

        for i, linha in enumerate(leitor, start=2):
            registro = {c: (linha.get(c) or "").strip() for c in planilha.colunas}
            planilha.linhas.append(registro)

            bruto = registro["telefone"]
            telefone = normalizar_telefone(bruto)
            if not bruto:
                motivo = "telefone vazio"
            elif not validar_telefone(telefone):
                motivo = f"telefone inválido ('{telefone}')"
            elif telefone in vistos:
                motivo = f"telefone duplicado ({telefone}) — anti reenvio"
            else:
                motivo = ""
            if motivo:
                print(f"[AVISO] Linha {i}: {motivo} — fora da fila (mantida no CSV).")
                continue

            vistos.add(telefone)
            registro["telefone"] = telefone
            registro["status"] = registro["status"].lower() or PENDENTE
            registro["resposta"] = registro["resposta"].lower()
            registro["cidade"] = registro["cidade"] or CIDADE_PADRAO
            planilha.contatos.append(registro)

    return planilha


def carregar_contatos(caminho: Path) -> list[dict]:
    """Atalho: apenas os contatos válidos do CSV."""
    return carregar_planilha(caminho).contatos


def salvar_contatos(caminho: Path, contatos: list[dict],
                    colunas: list[str] | None = None) -> None:
    """Grava o CSV completo de forma atômica."""
    caminho = Path(caminho)
    temporario = caminho.with_suffix(".csv.tmp")
    with temporario.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=colunas or COLUNAS,
                                extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(contatos)
    temporario.replace(caminho)  # evita corromper o CSV se o processo cair no meio


# ---------------------------------------------------------------------------
# 2. MENSAGENS — spintax + personalização (textos em mensagens.json)
# ---------------------------------------------------------------------------

CHAVES_MENSAGENS = ["saudacoes", "ganchos", "autoridades", "cases", "ctas",
                    "estruturas", "followups"]
_CACHE_MENSAGENS: dict[Path, dict] = {}


def carregar_mensagens(caminho: Path = ARQUIVO_MENSAGENS) -> dict:
    """Lê e valida o mensagens.json (com cache por caminho)."""
    caminho = Path(caminho).resolve()
    if caminho not in _CACHE_MENSAGENS:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        faltando = [c for c in CHAVES_MENSAGENS if not dados.get(c)]
        if faltando:
            raise ValueError(f"{caminho.name}: chaves ausentes ou vazias: "
                             f"{', '.join(faltando)}")
        if not isinstance(dados["saudacoes"], dict):
            raise ValueError(f"{caminho.name}: 'saudacoes' deve ser um objeto "
                             "com listas 'manha', 'tarde', 'noite' e/ou 'qualquer'")
        _CACHE_MENSAGENS[caminho] = dados
    return _CACHE_MENSAGENS[caminho]


def resolver_spintax(texto: str, contexto: dict) -> str:
    """Resolve `{a|b}` (escolha aleatória) e tokens `{clinica}`/`{bairro}`/...

    Valores do contexto podem conter outros tokens ou spintax — são resolvidos
    nas iterações seguintes. Tokens desconhecidos são preservados usando um
    placeholder temporário (sem ele, o loop nunca terminaria).
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


def periodo_do_dia(hora: int) -> str:
    if 5 <= hora < 12:
        return "manha"
    if 12 <= hora < 18:
        return "tarde"
    return "noite"


def escolher_saudacao(mensagens: dict, agora: datetime | None = None) -> str:
    """Saudação coerente com o horário ("Bom dia" só de manhã etc.)."""
    saudacoes = mensagens["saudacoes"]
    periodo = periodo_do_dia((agora or datetime.now()).hour)
    opcoes = list(saudacoes.get(periodo, [])) + list(saudacoes.get("qualquer", []))
    return random.choice(opcoes) if opcoes else "Olá, tudo bem?"


def nome_para_mensagem(clinica: str, bairro: str = "", cidade: str = "") -> str:
    """Limpa o nome vindo do Google Maps para soar humano na mensagem.

    "MB Odontologia | Dentista | Urgência" -> "MB Odontologia"
    "Odonto Center - Dentista"             -> "Odonto Center"
    "Dra. Fulana (Periodontista)."         -> "Dra. Fulana"
    "Sorridents Clínicas Jardins Aracaju"  -> "Sorridents Clínicas" (SEO "<bairro> <cidade>")
    "Clínica São José" (bairro São José)   -> inalterado (o bairro faz parte do nome)
    """
    original = (clinica or "").strip()
    partes = re.split(r"\s+[|–—-]\s+|\s*\|\s*", original)
    # Primeiro trecho com pelo menos 2 letras ("01 - Odonto10x" -> "Odonto10x")
    nome = next((p for p in partes if len(re.findall(r"[^\W\d_]", p)) >= 2),
                partes[0])
    nome = re.sub(r"\s*\([^)]*\)?", "", nome)
    # "Clínica Bem Viver. Consultas e Exames" -> corta no ponto final de frase
    # (mas não em abreviações curtas como "Dra. " / "Dr. ")
    nome = re.split(r"(?<=[^\W\d_]{4})\.\s+", nome)[0]
    nome = nome.split(", ")[0]  # "Dr. Fulano, Médico clínico geral"
    nome = re.sub(r"\s+(?:ltda|me|eireli|s/?a)\.?$", "", nome, flags=re.IGNORECASE)
    nome = nome.strip(" .,;:-")
    if nome.isupper() and len(nome) > 4:  # "CENTRO MEDICO JARDINS"
        pequenas = {"de", "do", "da", "dos", "das", "e", "em"}
        nome = " ".join(p.lower() if i and p.lower() in pequenas else p.capitalize()
                        for i, p in enumerate(nome.split()))
    if cidade:  # "... Consultório Odontológico em Aracaju" -> sem "em Aracaju"
        nome = re.sub(rf"\s+(?:em|de|-)\s+{re.escape(cidade)}$", "", nome,
                      flags=re.IGNORECASE)

    def sem_final(texto: str, final: str) -> str | None:
        """Remove `final` do fim se sobrarem 2+ palavras; senão None."""
        final = (final or "").strip()
        if not final or not texto.lower().endswith(" " + final.lower()):
            return None
        resto = texto[: -len(final)].strip(" .,;:-")
        return resto if len(resto.split()) >= 2 else None

    # Só trata como SEO quando a cidade está no fim; o bairro sai apenas se
    # vier logo antes dela ("... Jardins Aracaju").
    sem_cidade = sem_final(nome, cidade)
    if sem_cidade:
        nome = sem_final(sem_cidade, bairro) or sem_cidade
    return nome or original


INICIOS_MASCULINOS = ("dr ", "dr. ", "grupo ", "centro ", "instituto ",
                      "consultório ", "consultorio ", "hospital ", "espaço ",
                      "espaco ", "núcleo ", "nucleo ", "complexo ",
                      "laboratório ", "laboratorio ", "dentista ")


def artigo(nome: str) -> str:
    """'o' para nomes masculinos (Dr., Grupo, Centro...); 'a' (clínica) no resto."""
    return "o" if nome.lower().startswith(INICIOS_MASCULINOS) else "a"


def _contexto(contato: dict, mensagens: dict, agora: datetime | None) -> dict:
    bairro = (contato.get("bairro") or "").strip()
    cidade = (contato.get("cidade") or "").strip() or CIDADE_PADRAO
    nome = nome_para_mensagem(contato["clinica"], bairro, cidade)
    art = artigo(nome)
    return {
        "clinica": nome,
        "a_clinica": f"{art} {nome}",                       # "a Ferraz" / "o Dr. X"
        "da_clinica": f"{'do' if art == 'o' else 'da'} {nome}",
        "bairro": bairro,
        "cidade": cidade,
        # Sem bairro, "no {bairro}" viraria "no  e sei..." — cai para a cidade.
        "local": f"no {bairro}" if bairro else f"em {cidade}",
        "saudacao": escolher_saudacao(mensagens, agora),
    }


def montar_texto(contato: dict, mensagens: dict | None = None,
                 agora: datetime | None = None) -> str:
    """Monta uma mensagem inicial única e personalizada para o contato."""
    m = mensagens or carregar_mensagens()
    contexto = _contexto(contato, m, agora)
    contexto.update(
        gancho=random.choice(m["ganchos"]),
        autoridade=random.choice(m["autoridades"]),
        case=random.choice(m["cases"]),
        cta=random.choice(m["ctas"]),
    )
    return resolver_spintax(random.choice(m["estruturas"]), contexto)


def montar_texto_followup(contato: dict, mensagens: dict | None = None,
                          agora: datetime | None = None) -> str:
    """Mensagem curta de retomada para quem não respondeu."""
    m = mensagens or carregar_mensagens()
    return resolver_spintax(random.choice(m["followups"]),
                            _contexto(contato, m, agora))


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


def ultimo_envio() -> datetime | None:
    """Momento do envio mais recente registrado no histórico."""
    datas = [d for d in (_data(r.get("timestamp", "")) for r in ler_historico()) if d]
    return max(datas) if datas else None


def segundos_de_espera(ultimo: datetime | None, intervalo_alvo: int,
                       agora: datetime) -> int:
    """Quanto falta para cumprir o intervalo mínimo desde o último envio.

    O tempo gasto revisando, pulando contatos ou gerando variações já conta —
    a espera não reinicia a cada volta da fila.
    """
    if ultimo is None:
        return 0
    decorrido = (agora - ultimo).total_seconds()
    return max(0, math.ceil(intervalo_alvo - decorrido))


def dentro_da_janela(inicio: int, fim: int, agora: datetime | None = None) -> bool:
    """True se 'agora' estiver dentro da janela de horário comercial."""
    hora = (agora or datetime.now()).hour
    return inicio <= hora < fim


def dia_util(agora: datetime | None = None) -> bool:
    """Segunda a sexta. Prospecção no fim de semana parece automação."""
    return (agora or datetime.now()).weekday() < 5


def atualizar_historico(contato: dict, mensagem: str, modo: str,
                        tipo: str = INICIAL) -> None:
    """Anexa o envio ao log JSONL (auditoria)."""
    registro = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "clinica": contato["clinica"],
        "bairro": contato["bairro"],
        "telefone": contato["telefone"],
        "modo": modo,
        "tipo": tipo,
        "mensagem": mensagem,
    }
    with ARQUIVO_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def marcar_enviado(contato: dict, planilha: Planilha, tipo: str = INICIAL) -> None:
    """Atualiza status/timestamps do contato e grava o CSV."""
    agora = datetime.now().isoformat(timespec="seconds")
    contato["status"] = ENVIADO
    contato["resposta"] = "aguardando"
    contato["atualizado_em"] = agora
    contato["ultimo_envio"] = agora
    if tipo == FOLLOWUP:
        contato["followups"] = str(_inteiro(contato.get("followups")) + 1)
    planilha.salvar()


def precisa_followup(contato: dict, dias: int, max_followups: int,
                     agora: datetime | None = None) -> bool:
    """Enviado, sem resposta, abaixo do limite de follow-ups e há >= `dias`."""
    if contato.get("status") != ENVIADO:
        return False
    if (contato.get("resposta") or "") not in RESPOSTAS_EM_ABERTO:
        return False
    if _inteiro(contato.get("followups")) >= max_followups:
        return False
    referencia = _data(contato.get("ultimo_envio") or contato.get("atualizado_em") or "")
    if referencia is None:
        return False
    return ((agora or datetime.now()) - referencia).days >= dias


def reabrir_fila(contatos: list[dict]) -> int:
    """--reset: volta para 'pendente' quem recebeu e não engajou.

    Nunca reabre opt-out (`nao_contatar`) nem quem respondeu — esses já estão
    em conversa ou pediram para não serem contatados.
    """
    agora = datetime.now().isoformat(timespec="seconds")
    reabertos = 0
    for c in contatos:
        if c["status"] != ENVIADO or c.get("resposta") == "respondeu":
            continue
        c["status"] = PENDENTE
        c["resposta"] = ""
        c["followups"] = ""
        c["atualizado_em"] = agora
        reabertos += 1
    return reabertos


ARQUIVO_TRAVA = BASE_DIR / ".envio.lock"
_TRAVA_PROFUNDIDADE = 0  # reentrância dentro do mesmo processo


def _processo_vivo(pid: int) -> bool:
    """True se o PID existe. (No Windows, os.kill(pid, 0) MATARIA o processo.)"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            codigo = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo))
            return codigo.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class EnvioEmAndamento(RuntimeError):
    """Outro processo já está enviando (ou usando o perfil do WhatsApp Web)."""


class TravaEnvio:
    """Garante UM único processo enviando por vez (arquivo .envio.lock com o PID).

    Reentrante no mesmo processo; trava de processo morto é descartada.
    """

    def __init__(self, caminho: Path | None = None) -> None:
        self.caminho = Path(caminho or ARQUIVO_TRAVA)

    def __enter__(self) -> "TravaEnvio":
        import os
        global _TRAVA_PROFUNDIDADE
        if _TRAVA_PROFUNDIDADE:
            _TRAVA_PROFUNDIDADE += 1
            return self
        for _ in range(2):
            try:
                fd = os.open(self.caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    dono = int(self.caminho.read_text(encoding="utf-8").strip() or 0)
                except (OSError, ValueError):
                    dono = 0
                if dono and dono != os.getpid() and _processo_vivo(dono):
                    raise EnvioEmAndamento(
                        f"Outro envio está em andamento (processo {dono}). Aguarde "
                        f"ele terminar ou feche-o; se tiver certeza de que não há "
                        f"nenhum, apague {self.caminho.name}.")
                self.caminho.unlink(missing_ok=True)  # trava órfã
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            _TRAVA_PROFUNDIDADE = 1
            return self
        raise EnvioEmAndamento(f"Não foi possível criar {self.caminho.name}.")

    def __exit__(self, *_exc) -> None:
        global _TRAVA_PROFUNDIDADE
        _TRAVA_PROFUNDIDADE -= 1
        if _TRAVA_PROFUNDIDADE <= 0:
            _TRAVA_PROFUNDIDADE = 0
            self.caminho.unlink(missing_ok=True)


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
          f"(intervalo mínimo desde o último envio, anti-ban):")
    for restante in range(segundos, 0, -1):
        minutos, segs = divmod(restante, 60)
        print(f"\r  ⏳ {minutos:02d}:{segs:02d} — Ctrl+C para interromper "
              "(o progresso é salvo)  ", end="", flush=True)
        time.sleep(1)
    print("\r  ✅ Cooldown concluído. Pode seguir para o próximo contato.       ")


def ler_texto_multilinha(ler=input, terminador: str = ".") -> str:
    """Lê várias linhas (inclusive vazias) até uma linha só com o terminador."""
    linhas: list[str] = []
    while True:
        linha = ler()
        if linha.strip() == terminador:
            break
        linhas.append(linha)
    return "\n".join(linhas).strip("\n")


# ---------------------------------------------------------------------------
# 4. MODOS DE ENVIO (sempre com confirmação humana)
# ---------------------------------------------------------------------------

def _cabecalho_alvo(contato: dict) -> None:
    print("\n" + "=" * 64)
    print(f"[ALVO] {contato['clinica']} — {contato['bairro'] or '?'}, "
          f"{contato.get('cidade') or CIDADE_PADRAO}")
    print(f"[TEL ] +{contato['telefone']}")


def enviar_modo_manual(contato: dict, mensagem: str) -> bool:
    """Abre o link do WhatsApp Web no navegador padrão e pausa para o humano.

    A prévia do texto já foi exibida pelo orquestrador (prévia única);
    aqui ficam apenas o cabeçalho do alvo e o link codificado.
    """
    link = montar_link(contato, mensagem)
    _cabecalho_alvo(contato)
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


def texto_confere(texto_na_caixa: str, mensagem: str) -> bool:
    """A caixa contém exatamente a mensagem (nem a mais, nem a menos)?

    Compara só letras e números: o WhatsApp Web renderiza emoji como imagem
    e pode mexer em espaços/quebras, mas texto duplicado ou faltando aparece.
    """
    def nucleo(s: str) -> str:
        return re.sub(r"[\W_]+", "", s or "").lower()
    return bool(nucleo(mensagem)) and nucleo(texto_na_caixa) == nucleo(mensagem)


class SessaoWeb:
    """WhatsApp Web via Selenium, com UM navegador reaproveitado na sessão.

    O texto entra na conversa pelo próprio link `send?text=` (que preenche a
    caixa de mensagem). Não usamos `send_keys` para digitar: o ChromeDriver
    não aceita emoji (fora do BMP) e digitar por cima do texto pré-preenchido
    duplicaria a mensagem.
    """

    SELETOR_CAIXA = ('footer div[contenteditable="true"], '
                     'div[contenteditable="true"][data-tab="10"], '
                     'div[contenteditable="true"][data-tab="6"]')

    def __init__(self) -> None:
        self.driver = None
        self._trava: TravaEnvio | None = None

    def _garantir_driver(self):
        if self.driver is None:
            # O perfil do WhatsApp Web só pode ser usado por um processo por vez.
            if self._trava is None:
                trava = TravaEnvio()
                trava.__enter__()  # levanta EnvioEmAndamento se já houver outro
                self._trava = trava
            from selenium import webdriver
            opcoes = webdriver.ChromeOptions()
            opcoes.add_argument("--start-maximized")
            opcoes.add_argument(f"--user-data-dir={PERFIL_SELENIUM}")  # mantém o login
            self.driver = webdriver.Chrome(options=opcoes)
            print("\n[AGUARDE] Escaneie o QR Code se necessário (o perfil salvo "
                  "acelera nas próximas vezes)...")
        return self.driver

    def enviar(self, contato: dict, mensagem: str,
               permitir_reenvio: bool = False) -> bool:
        """Prepara e envia UMA mensagem. Sem `permitir_reenvio`, recusa números
        que já receberam hoje — conferido de novo no último instante."""
        if not permitir_reenvio and enviado_hoje(contato["telefone"]):
            print(f"[BLOQUEADO] +{contato['telefone']} já recebeu mensagem hoje "
                  "— envio duplicado evitado.")
            return False
        _cabecalho_alvo(contato)
        print("=" * 64)
        if input("\nPreparar o envio no WhatsApp Web (Selenium)? [s/N]: "
                 ).strip().lower() != "s":
            print("[SKIP] Envio cancelado pelo operador.")
            return False

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
            driver = self._garantir_driver()
        except ImportError:
            print("[ERRO] Selenium não instalado. Rode: pip install -r requirements.txt")
            return False
        except EnvioEmAndamento as exc:
            print(f"[BLOQUEADO] {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            print(f"[ERRO] Não foi possível abrir o Chrome: {exc}")
            self.fechar()
            return False

        try:
            driver.get(montar_link(contato, mensagem))
            caixa = WebDriverWait(driver, 60).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.SELETOR_CAIXA)))
            # Espera o WhatsApp terminar de preencher a caixa com o texto do link.
            # NUNCA digitamos/colamos por cima: isso já duplicou mensagens
            # (cópia sem quebras de linha misturada ao texto original).
            confere = False
            for _ in range(40):  # até ~20s
                time.sleep(0.5)
                if texto_confere(caixa.get_attribute("innerText") or "", mensagem):
                    confere = True
                    break
            if not confere:
                print("[ERRO] O texto na caixa não confere com a mensagem aprovada "
                      "(faltando ou duplicado) — NADA foi enviado.")
                self._limpar_caixa(caixa)
                return False

            print("\n" + "=" * 64)
            print("MENSAGEM NA CAIXA DA CONVERSA, MAS *NÃO* ENVIADA.")
            print("Confira no navegador. Só envie se você revisar o texto.")
            print("=" * 64)
            if input("ENVIAR AGORA? Digite 'ENVIAR' (ou qualquer outra coisa "
                     "para abortar): ").strip() != "ENVIAR":
                print("[SKIP] Envio abortado pelo operador (nada foi enviado).")
                self._limpar_caixa(caixa)
                return False

            # Última conferência: alguém pode ter enviado enquanto revisávamos.
            if not permitir_reenvio and enviado_hoje(contato["telefone"]):
                print("[BLOQUEADO] Este número recebeu mensagem durante a revisão "
                      "— envio duplicado evitado.")
                self._limpar_caixa(caixa)
                return False
            # ...e o texto ainda é exatamente o aprovado?
            if not texto_confere(caixa.get_attribute("innerText") or "", mensagem):
                print("[ERRO] O texto na caixa mudou desde a revisão — NADA foi enviado.")
                self._limpar_caixa(caixa)
                return False

            caixa.send_keys(Keys.ENTER)
            time.sleep(2)
            print("[OK] Mensagem enviada via WhatsApp Web.")
            return True

        except Exception as exc:  # noqa: BLE001 - seletores da Meta mudam com frequência
            print(f"[ERRO] Falha na automação do WhatsApp Web: {exc}")
            print("       Alternativa: use --modo manual")
            self.fechar()  # navegador pode ter sido fechado; reabre no próximo
            return False

    @staticmethod
    def _limpar_caixa(caixa) -> None:
        """Apaga o rascunho para não sobrar texto na conversa (nem misturar
        com o próximo envio para o mesmo número)."""
        try:
            from selenium.webdriver.common.keys import Keys
            caixa.click()
            caixa.send_keys(Keys.CONTROL, "a")
            caixa.send_keys(Keys.DELETE)
        except Exception:  # noqa: BLE001
            print("[AVISO] Não consegui limpar o rascunho — apague-o no WhatsApp Web.")

    def fechar(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:  # noqa: BLE001
                pass
            self.driver = None
        if self._trava is not None:
            self._trava.__exit__(None, None, None)
            self._trava = None


# ---------------------------------------------------------------------------
# 5. ORQUESTRAÇÃO — FLUXO SEMI-SUPERVISIONADO
# ---------------------------------------------------------------------------

def exibir_contatos(contatos: list[dict]) -> None:
    print(f"\n{'#':>3} | {'CLÍNICA':<28} | {'BAIRRO':<14} | {'TELEFONE':<15} | "
          f"{'STATUS':<12} | RESPOSTA")
    print("-" * 104)
    for i, c in enumerate(contatos, start=1):
        fixo = "" if eh_celular(c["telefone"]) else " (fixo)"
        print(f"{i:>3} | {c['clinica'][:28]:<28} | {c['bairro'][:14]:<14} | "
              f"{c['telefone']:<15} | {c['status']:<12} | "
              f"{c.get('resposta') or '-'}{fixo}")


def gerenciar_respostas(contatos: list[dict], planilha: Planilha) -> None:
    """Fecha o ciclo human-in-the-loop: registra quem respondeu à abordagem."""
    enviados = [c for c in contatos if c["status"] == ENVIADO]
    if not enviados:
        print("\nNenhum contato com status 'enviado' para acompanhar.")
        return

    print(f"\n[RESPOSTAS] {len(enviados)} contato(s) aguardando acompanhamento.")
    alterados = 0
    for c in enviados:
        print("\n" + "-" * 64)
        print(f"{c['clinica']} — {c['bairro']} | +{c['telefone']} | "
              f"último envio {c.get('ultimo_envio') or c.get('atualizado_em') or '?'} | "
              f"resposta atual: {c.get('resposta') or '-'}")
        acao = input("Respondeu? [s]im / [n]ão / [x] não contatar mais (opt-out) "
                     "/ [ENTER] manter: ").strip().lower()
        if acao == "s":
            c["resposta"] = "respondeu"
        elif acao == "n":
            c["resposta"] = "sem resposta"
        elif acao == "x":
            c["status"] = NAO_CONTATAR
            c["resposta"] = "sem interesse"
        else:
            continue
        c["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
        alterados += 1
        planilha.salvar()  # salva a cada resposta: Ctrl+C não perde nada

    if alterados:
        print(f"\n[OK] {alterados} resposta(s) atualizada(s) em "
              f"{planilha.caminho.name}.")
    else:
        print("\n[INFO] Nenhuma resposta alterada.")


def _exibir_previa(mensagem: str, titulo: str) -> None:
    print(f"\n[{titulo}]")
    print("-" * 64)
    print(mensagem)
    print("-" * 64)


def rodar_fila(fila: list[dict], planilha: Planilha, args: argparse.Namespace,
               mensagens: dict, tipo: str = INICIAL) -> None:
    """Fila: monta texto -> prévia -> confirmação -> envio -> intervalo -> próximo."""
    modo = args.modo
    rotulo = "follow-up" if tipo == FOLLOWUP else "pendente"

    if not fila:
        if tipo == FOLLOWUP:
            print(f"\nNenhum contato elegível para follow-up (enviados há "
                  f"{args.dias_followup}+ dias, sem resposta, até "
                  f"{args.max_followups} follow-up(s)).")
        else:
            print("\nNenhum contato pendente. Use --reset para reabrir quem "
                  "não respondeu, ou --followup para retomá-los.")
        return

    if modo != "preview":
        agora = datetime.now()
        if not args.fim_de_semana and not dia_util(agora):
            print("\n[BLOQUEADO] Hoje é fim de semana — prospecção fora de dia útil "
                  "parece automação. Use --fim-de-semana para liberar ou "
                  "--preview para apenas ver prévias.")
            return
        if not dentro_da_janela(args.hora_inicio, args.hora_fim, agora):
            print(f"\n[BLOQUEADO] Fora da janela de horário ({agora:%H:%M}; "
                  f"permitido: {args.hora_inicio:02d}h–{args.hora_fim:02d}h). "
                  "Prospecção fora do comercial parece automação — retome dentro "
                  "da janela ou use --preview para apenas ver prévias.")
            return

    # Salto opcional para um contato específico (1-based)
    inicio = 0
    if args.contato:
        if not 1 <= args.contato <= len(fila):
            print(f"\n[ERRO] --contato {args.contato} fora do intervalo "
                  f"(1..{len(fila)} na fila).")
            return
        inicio = args.contato - 1
        print(f"\n[SELEÇÃO] Iniciando pelo contato #{args.contato} da fila.")

    print(f"\n[FILA] {len(fila) - inicio} contato(s) {rotulo}(s). "
          f"Modo: {modo.upper()} (sempre com sua confirmação).")

    gerar = montar_texto_followup if tipo == FOLLOWUP else montar_texto
    intervalo_alvo = random.randint(args.intervalo_min, args.intervalo_max)
    # Um único processo enviando por vez (evita mensagens duplicadas).
    trava = TravaEnvio() if modo != "preview" else None
    if trava is not None:
        try:
            trava.__enter__()
        except EnvioEmAndamento as exc:
            print(f"\n[BLOQUEADO] {exc}")
            return
    sessao_web = SessaoWeb() if modo == "web" else None
    reenvio_autorizado: set[str] = set()

    try:
        indice = inicio
        while indice < len(fila):
            contato = fila[indice]

            if modo != "preview":
                if not verificar_limite_diario(args.limite_dia):
                    break

                # Anti reenvio: este número já recebeu mensagem hoje?
                ts = enviado_hoje(contato["telefone"])
                if ts:
                    print(f"\n[ALERTA] {contato['clinica']} (+{contato['telefone']}) "
                          f"JÁ recebeu mensagem hoje às "
                          f"{ts[11:16] if len(ts) >= 16 else ts}.")
                    escolha = input("Enviar novamente mesmo assim? [s/N]: ").strip().lower()
                    if escolha != "s":
                        print("[SKIP] Reenvio evitado (anti-ban). Contato segue "
                              "na fila para os próximos dias.")
                        indice += 1
                        continue
                    reenvio_autorizado.add(contato["telefone"])

                # Intervalo mínimo desde o ÚLTIMO ENVIO real (pular ou pedir
                # nova variação não reinicia a espera).
                restante = segundos_de_espera(ultimo_envio(), intervalo_alvo,
                                              datetime.now())
                if restante:
                    contagem_regressiva(restante)

            print("\n" + "=" * 64)
            print(f"[PRÓXIMO] {contato['clinica']} — {contato['bairro'] or '?'}, "
                  f"{contato.get('cidade') or CIDADE_PADRAO} | +{contato['telefone']}"
                  f"{' | FOLLOW-UP' if tipo == FOLLOWUP else ''}")
            if not eh_celular(contato["telefone"]):
                print("[AVISO] Número fixo — pode não ter WhatsApp.")
            print("=" * 64)

            mensagem = gerar(contato, mensagens)

            if modo == "preview":
                _exibir_previa(mensagem, "PRÉVIA — dry-run, nada será enviado")
                acao = input("\n[ENTER] nova variação | [p]ular | [q] sair: ").strip().lower()
                if acao == "q":
                    break
                if acao == "p":
                    indice += 1
                continue  # ENTER => gera nova variação de spintax

            # Prévia sempre aparece antes de qualquer envio
            _exibir_previa(mensagem, "PRÉVIA")
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
                print("Cole/edite a mensagem final. Linhas vazias são mantidas; "
                      "termine com uma linha contendo apenas '.':")
                editada = ler_texto_multilinha()
                if editada:
                    mensagem = editada
                _exibir_previa(mensagem, "VERSÃO FINAL")
                if input("\nEnviar esta versão? [s/N]: ").strip().lower() != "s":
                    continue
            elif acao != "a":
                print("[INFO] Ação não reconhecida — nada foi enviado.")
                continue

            if not verificar_limite_diario(args.limite_dia):
                break

            if sessao_web is not None:
                sucesso = sessao_web.enviar(
                    contato, mensagem,
                    permitir_reenvio=contato["telefone"] in reenvio_autorizado)
            else:
                sucesso = enviar_modo_manual(contato, mensagem)

            if sucesso:
                atualizar_historico(contato, mensagem, modo, tipo)
                marcar_enviado(contato, planilha, tipo)
                print(f"[OK] {contato['clinica']} marcado como 'enviado' "
                      f"({contato['atualizado_em']}).")
                intervalo_alvo = random.randint(args.intervalo_min, args.intervalo_max)
                indice += 1
            else:
                print(f"[RETIDO] Contato continua na fila ({rotulo}).")
                if input("Sair da fila? [s/N]: ").strip().lower() == "s":
                    break
    finally:
        if sessao_web is not None:
            sessao_web.fechar()
        if trava is not None:
            trava.__exit__(None, None, None)

    print(f"\n[FIM] Sessão encerrada. Progresso salvo em "
          f"{planilha.caminho.name} e {ARQUIVO_LOG.name}.")


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------

def _criar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prospecção B2B human-in-the-loop para clínicas (WhatsApp)."
    )
    parser.add_argument("--arquivo", default=str(ARQUIVO_CSV),
                        help="Caminho do CSV de prospecção (padrão: prospeccao.csv)")
    parser.add_argument("--mensagens", default=str(ARQUIVO_MENSAGENS),
                        help="JSON com os textos/spintax (padrão: mensagens.json)")
    parser.add_argument("--modo", choices=["manual", "web", "preview"],
                        default="manual",
                        help="manual=link WhatsApp Web (padrão) | web=Selenium | preview=dry-run")
    parser.add_argument("--limite-dia", type=int, default=20,
                        help="Máximo de mensagens por dia (padrão: 20, anti-ban)")
    parser.add_argument("--intervalo-min", type=int, default=45,
                        help="Intervalo mínimo entre envios em segundos (padrão: 45)")
    parser.add_argument("--intervalo-max", type=int, default=90,
                        help="Intervalo máximo entre envios em segundos (padrão: 90)")
    parser.add_argument("--listar", action="store_true", help="Apenas lista os contatos")
    parser.add_argument("--preview", action="store_true",
                        help="Gera prévias sem enviar (dry-run)")
    parser.add_argument("--respostas", action="store_true",
                        help="Acompanha quem respondeu / opt-out (fecha o ciclo)")
    parser.add_argument("--followup", action="store_true",
                        help="Fila de follow-up: enviados sem resposta há N dias")
    parser.add_argument("--dias-followup", type=int, default=3,
                        help="Dias desde o último envio para o follow-up (padrão: 3)")
    parser.add_argument("--max-followups", type=int, default=1,
                        help="Máximo de follow-ups por contato (padrão: 1)")
    parser.add_argument("--contato", type=int, default=None,
                        help="Inicia a fila a partir do N-ésimo contato (1-based)")
    parser.add_argument("--hora-inicio", type=int, default=8,
                        help="Início da janela de envio, hora inteira (padrão: 8)")
    parser.add_argument("--hora-fim", type=int, default=19,
                        help="Fim da janela de envio, hora inteira (padrão: 19)")
    parser.add_argument("--fim-de-semana", action="store_true",
                        help="Permite envios no sábado e domingo (bloqueado por padrão)")
    parser.add_argument("--clinica", "--status", dest="clinica", default=None,
                        help="Filtra pelo nome da clínica (substring, ex.: 'Ferraz'). "
                             "--status é um alias antigo.")
    parser.add_argument("--reset", action="store_true",
                        help="Reabre como 'pendente' quem recebeu e não respondeu "
                             "(nunca reabre opt-out nem quem respondeu)")
    return parser


def main() -> None:
    parser = _criar_parser()
    args = parser.parse_args()

    if args.preview:
        args.modo = "preview"
    if args.intervalo_min > args.intervalo_max:
        parser.error("--intervalo-min não pode ser maior que --intervalo-max")

    try:
        mensagens = carregar_mensagens(Path(args.mensagens))
    except (OSError, ValueError) as exc:
        print(f"[ERRO] Não foi possível carregar os textos: {exc}")
        sys.exit(1)

    planilha = carregar_planilha(Path(args.arquivo))
    contatos = planilha.contatos

    if not contatos:
        if args.modo == "preview" and not (args.reset or args.respostas or args.listar):
            print("[INFO] Sem contatos válidos — usando exemplos fictícios só "
                  "para a prévia (nada é gravado).\n")
            contatos = [{c: "" for c in COLUNAS} | dict(e) for e in LISTA_EXEMPLO]
        else:
            print(f"[ERRO] Nenhum contato válido em '{planilha.caminho.name}'. "
                  "Rode o minerador (minerador_maps.py) ou edite o CSV.")
            sys.exit(1)

    if args.reset:
        elegiveis = sum(1 for c in contatos
                        if c["status"] == ENVIADO and c.get("resposta") != "respondeu")
        if not elegiveis:
            print("[RESET] Nada a reabrir (só reabre quem recebeu e não respondeu).")
            return
        if input(f"[RESET] Reabrir {elegiveis} contato(s) que receberam e não "
                 "responderam? Opt-outs e quem respondeu ficam como estão. "
                 "[s/N]: ").strip().lower() != "s":
            print("[RESET] Cancelado.")
            return
        reabertos = reabrir_fila(contatos)
        planilha.salvar()
        print(f"[RESET] {reabertos} contato(s) marcado(s) como 'pendente'.")
        return

    if args.clinica:
        filtro = args.clinica.lower()
        contatos = [c for c in contatos if filtro in c["clinica"].lower()]
        if not contatos:
            print(f"[ERRO] Nenhum contato corresponde a '{args.clinica}'.")
            sys.exit(1)

    if args.respostas:
        try:
            gerenciar_respostas(contatos, planilha)
        except (KeyboardInterrupt, EOFError):
            print("\n[INTERRUPÇÃO] Respostas já registradas foram salvas.")
            sys.exit(130)
        return

    if args.listar:
        exibir_contatos(contatos)
        return

    if args.followup:
        tipo = FOLLOWUP
        fila = [c for c in contatos
                if precisa_followup(c, args.dias_followup, args.max_followups)]
    else:
        tipo = INICIAL
        fila = [c for c in contatos if c["status"] == PENDENTE]

    print("=" * 64)
    print(" PROSPECÇÃO B2B — WHATSAPP (semi-supervisionada)")
    print(" Nenhum envio ocorre sem sua confirmação explícita.")
    print("=" * 64)
    exibir_contatos(contatos)

    try:
        rodar_fila(fila, planilha, args, mensagens, tipo)
    except (KeyboardInterrupt, EOFError):
        # Cada envio já é gravado no CSV na hora; não há nada pendente a salvar.
        print("\n\n[INTERRUPÇÃO] Sessão interrompida. Envios confirmados já estão "
              f"salvos em {planilha.caminho.name}. Rode o script novamente para "
              "continuar de onde parou.")
        sys.exit(130)


if __name__ == "__main__":
    main()
