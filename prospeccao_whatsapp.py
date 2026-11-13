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
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from prospeccao.calendario import (  # noqa: F401
    FERIADOS_FIXOS,
    _pascoa,
    dentro_da_janela,
    dia_util,
    dias_uteis_entre,
    eh_feriado,
)
from prospeccao.classificador import (  # noqa: F401
    FRASES_BOT,
    PADROES_NEGATIVA,
    PADROES_OPTOUT,
    classificar_resposta,
    interpretar_bolha,
    mensagens_recebidas,
    opcao_atendente,
    resposta_negativa,
    resposta_para_robo,
    resultado_entrega,
    texto_confere,
)
from prospeccao.config import carregar_config
from prospeccao.telefones import (  # noqa: F401
    _escore_sinais,
    _sem_acentos,
    eh_celular,
    inferir_segmento,
    normalizar_telefone,
    validar_telefone,
)
from prospeccao.trava import (  # noqa: F401
    ARQUIVO_TRAVA,
    EnvioEmAndamento,
    TravaEnvio,
    _processo_vivo,
)

# ---------------------------------------------------------------------------
# 1. FONTES DE DADOS
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ARQUIVO_CSV = BASE_DIR / "prospeccao.csv"
ARQUIVO_LOG = BASE_DIR / "historico_envios.jsonl"
ARQUIVO_MENSAGENS = BASE_DIR / "mensagens.json"
PERFIL_SELENIUM = BASE_DIR / ".perfil_whatsapp_web"

# Por padrão o Chrome do script abre FORA DA TELA (não salta na frente do que
# você está fazendo, nem nos envios nem na leitura das conversas). Fica visível
# só com --conectar (para escanear o QR Code) ou --mostrar-janela.
JANELA_VISIVEL = False


def opcoes_janela(visivel: bool) -> list[str]:
    """Argumentos de posição/tamanho da janela do Chrome."""
    if visivel:
        return ["--start-maximized"]
    # Tamanho normal (o WhatsApp Web precisa do layout de desktop), mas fora da
    # área visível de qualquer monitor.
    return ["--window-size=1366,900", "--window-position=-32000,-32000"]

COLUNAS = ["clinica", "bairro", "cidade", "telefone", "site", "avaliacoes",
           "segmento", "status", "resposta", "atualizado_em", "ultimo_envio",
           "followups", "variante", "etapa", "menu", "versao_textos", "estagio"]
# segmento: "odonto" | "medico" — inferido na mineração (nome/termo de busca);
#        filtro --segmento no envio; vazio = não classificado
# variante: teste A/B ("curta" | "completa")
# etapa: "abertura" (curta enviada) | "apresentacao" (texto completo enviado)
#        | "resumo" (resumo enviado depois que a pessoa respondeu à completa)
#        | "recusou" (respondeu com uma negativa: nada automático, o operador decide)
#        | "despedida" (pediu para não ser contatado e recebeu o agradecimento final)
# versao_textos: "versao" do mensagens.json usada no 1º envio (o A/B compara
#        só contatos da versão atual; vazio = textos antigos)
# menu: "respondido" quando o script já escolheu "falar com atendente" no robô
# estagio: funil comercial pós-resposta ("reuniao_agendada" | "proposta_enviada"
#        | "fechado" | "perdido")

ESTAGIOS_COMERCIAIS = ("reuniao_agendada", "proposta_enviada", "fechado", "perdido")


def score_lead(contato: dict) -> int:
    """Pontua o lead para priorizar a fila: celular > volume de avaliações > nota >= 4,5 > site."""
    pontos = 1000 if eh_celular(contato.get("telefone") or "") else 0
    aval = contato.get("avaliacoes") or ""
    m_qtd = re.search(r"\((\d+)\s*avalia", aval)
    if m_qtd:
        pontos += min(int(m_qtd.group(1)), 500)
    m_nota = re.search(r"(\d+[.,]\d+)", aval)
    if m_nota and float(m_nota.group(1).replace(",", ".")) >= 4.5:
        pontos += 50
    if (contato.get("site") or "").strip():
        pontos += 10
    return pontos

CIDADE_PADRAO = str(carregar_config().get("cidade_padrao") or "Aracaju")

# Status do contato
PENDENTE = "pendente"
ENVIADO = "enviado"
NAO_CONTATAR = "nao_contatar"  # opt-out: nunca volta à fila (nem com --reset)

# Respostas que ainda admitem follow-up
RESPOSTAS_EM_ABERTO = {"", "aguardando", "sem resposta", "resposta automatica"}

# Segmentos de clínica (coluna "segmento"; filtro --segmento)
SEGMENTOS = ("odonto", "medico")

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
            registro["segmento"] = registro["segmento"].lower()
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
                    "estruturas", "followups", "aberturas", "estruturas_apresentacao",
                    "ctas_apresentacao", "estruturas_resumo", "despedidas"]
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


def nota_google(contato: dict, minima: float = 4.5) -> str:
    """Nota do Google para elogiar ("4,9"), só se for >= `minima`; senão ''."""
    m = re.search(r"(\d)[.,](\d)", contato.get("avaliacoes") or "")
    if not m:
        return ""
    nota = float(f"{m.group(1)}.{m.group(2)}")
    return f"{m.group(1)},{m.group(2)}" if minima <= nota <= 5 else ""


def _opcoes(lista: list[str], contexto: dict) -> list[str]:
    """Descarta textos com {nota} quando a clínica não tem nota para elogiar."""
    if contexto.get("nota"):
        return lista
    return [o for o in lista if "{nota}" not in o] or lista


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
        "nota": nota_google(contato),
    }


def montar_texto(contato: dict, mensagens: dict | None = None,
                 agora: datetime | None = None) -> str:
    """Monta uma mensagem inicial única e personalizada para o contato."""
    m = mensagens or carregar_mensagens()
    contexto = _contexto(contato, m, agora)
    contexto.update(
        gancho=random.choice(_opcoes(m["ganchos"], contexto)),
        autoridade=random.choice(m["autoridades"]),
        case=random.choice(m["cases"]),
        cta=random.choice(m["ctas"]),
    )
    return resolver_spintax(random.choice(m["estruturas"]), contexto)


def variante_ab(telefone: str) -> str:
    """Variante fixa por número (metade 'curta', metade 'completa')."""
    import hashlib
    return "curta" if int(hashlib.sha1(telefone.encode()).hexdigest(), 16) % 2 else "completa"


def montar_abertura(contato: dict, mensagens: dict | None = None,
                    agora: datetime | None = None) -> str:
    """Variante 'curta': uma pergunta simples para a recepção responder rápido."""
    m = mensagens or carregar_mensagens()
    contexto = _contexto(contato, m, agora)
    return resolver_spintax(random.choice(_opcoes(m["aberturas"], contexto)), contexto)


def montar_apresentacao(contato: dict, mensagens: dict | None = None,
                        agora: datetime | None = None) -> str:
    """2ª etapa da variante curta, enviada quando uma pessoa responde."""
    m = mensagens or carregar_mensagens()
    contexto = _contexto(contato, m, agora)
    contexto.update(case=random.choice(m["cases"]),
                    cta_apresentacao=random.choice(m["ctas_apresentacao"]))
    return _com_link_demonstracao(
        resolver_spintax(random.choice(m["estruturas_apresentacao"]), contexto), m, contato)


def montar_resumo_completa(contato: dict, mensagens: dict | None = None,
                           agora: datetime | None = None) -> str:
    """2ª etapa da variante completa: o resumo prometido no CTA ("te mando um
    resumo de 1 minuto"), enviado quando uma pessoa responde."""
    m = mensagens or carregar_mensagens()
    return _com_link_demonstracao(
        resolver_spintax(random.choice(m["estruturas_resumo"]),
                         _contexto(contato, m, agora)), m, contato)


def montar_despedida(contato: dict, mensagens: dict | None = None,
                      agora: datetime | None = None) -> str:
    """Agradecimento final, formal, para quem disse que não tem interesse."""
    m = mensagens or carregar_mensagens()
    return resolver_spintax(random.choice(m["despedidas"]), _contexto(contato, m, agora))


def montar_texto_followup(contato: dict, mensagens: dict | None = None,
                           agora: datetime | None = None) -> str:
    """Mensagem curta de retomada para quem não respondeu."""
    m = mensagens or carregar_mensagens()
    return resolver_spintax(random.choice(m["followups"]),
                            _contexto(contato, m, agora))


def demonstracao_html(mensagens: dict | None = None,
                      contato: dict | None = None) -> str:
    """URL da demonstração interativa ('' = não enviar o link).

    Quando `contato` é informado, personaliza com `?clinica=...&seg=...`
    para a página exibir o nome da clínica e o roteiro do segmento certo.
    """
    m = mensagens or carregar_mensagens()
    url = str((m.get("demonstracao") or {}).get("html") or "").strip()
    if not url or not contato:
        return url
    nome = nome_para_mensagem(
        contato.get("clinica", ""),
        contato.get("bairro", ""),
        contato.get("cidade") or CIDADE_PADRAO,
    )
    seg = (contato.get("segmento") or inferir_segmento(contato.get("clinica", ""))).lower()
    params = {}
    if nome:
        params["clinica"] = nome
    if seg in SEGMENTOS:
        params["seg"] = seg
    if not params:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode(params)}"


def demonstracao_pdf(mensagens: dict | None = None) -> Path | None:
    """Caminho do PDF anexado na 2ª etapa (None = sem anexo)."""
    m = mensagens or carregar_mensagens()
    nome = str((m.get("demonstracao") or {}).get("pdf") or "").strip()
    if not nome:
        return None
    caminho = Path(nome)
    if not caminho.is_absolute():
        caminho = Path(__file__).resolve().parent / caminho
    return caminho if caminho.is_file() else None


def _com_link_demonstracao(texto: str, mensagens: dict,
                           contato: dict | None = None) -> str:
    """Acrescenta o link da demonstração quando hospedado (html preenchido)."""
    url = demonstracao_html(mensagens, contato)
    return f"{texto}\n\nDemonstração interativa: {url}" if url else texto


def montar_link(contato: dict, mensagem: str) -> str:
    """Codifica a mensagem e monta o link do WhatsApp Web."""
    codificada = quote(mensagem, safe="")
    return (f"https://web.whatsapp.com/send?phone={contato['telefone']}"
            f"&text={codificada}")


# ---------------------------------------------------------------------------
# 3. CONTROLE DE VOLUME (anti-ban) E HISTÓRICO
# ---------------------------------------------------------------------------

_CACHE_HISTORICO: dict[Path, tuple[tuple[int, int], list[dict]]] = {}


def ler_historico() -> list[dict]:
    caminho = Path(ARQUIVO_LOG)
    if not caminho.exists():
        return []
    chave = caminho.resolve()
    stat = caminho.stat()
    assinatura = (stat.st_mtime_ns, stat.st_size)
    em_cache = _CACHE_HISTORICO.get(chave)
    if em_cache and em_cache[0] == assinatura:
        return list(em_cache[1])
    registros = []
    with caminho.open(encoding="utf-8") as f:
        for linha in f:
            try:
                registros.append(json.loads(linha))
            except json.JSONDecodeError:
                continue
    _CACHE_HISTORICO[chave] = (assinatura, registros)
    return list(registros)


# Fora do limite diário: escolher "falar com atendente" no robô e a 2ª etapa
# (apresentação/resumo), que só vai para quem respondeu. Se contasse, o envio
# das 10h30 esgotaria a cota e a resposta a quem falou com a gente ficaria
# adiada dia após dia. A 2ª etapa tem um teto próprio (segundas_etapas_hoje).
SEGUNDAS_ETAPAS = {"apresentacao", "resumo"}
TIPOS_FORA_DO_LIMITE = {"menu", "despedida"} | SEGUNDAS_ETAPAS


def envios_hoje() -> int:
    hoje = datetime.now().date().isoformat()
    return sum(1 for r in ler_historico()
               if str(r.get("timestamp", "")).startswith(hoje)
               and r.get("tipo") not in TIPOS_FORA_DO_LIMITE)


def segundas_etapas_hoje() -> int:
    """Apresentações/resumos enviados hoje (teto de segurança próprio)."""
    hoje = datetime.now().date().isoformat()
    return sum(1 for r in ler_historico()
               if str(r.get("timestamp", "")).startswith(hoje)
               and r.get("tipo") in SEGUNDAS_ETAPAS)


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


def atualizar_historico(contato: dict, mensagem: str, modo: str,
                        tipo: str = INICIAL, variante: str = "") -> None:
    """Anexa o envio ao log JSONL (auditoria)."""
    registro = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "clinica": contato["clinica"],
        "bairro": contato["bairro"],
        "telefone": contato["telefone"],
        "modo": modo,
        "tipo": tipo,
        "variante": variante or contato.get("variante", ""),
        "mensagem": mensagem,
    }
    with ARQUIVO_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    _CACHE_HISTORICO.pop(Path(ARQUIVO_LOG).resolve(), None)


def marcar_enviado(contato: dict, planilha: Planilha, tipo: str = INICIAL,
                   salvar: bool = True) -> None:
    """Atualiza status/timestamps do contato e grava o CSV."""
    agora = datetime.now().isoformat(timespec="seconds")
    contato["status"] = ENVIADO
    contato["resposta"] = "aguardando"
    contato["atualizado_em"] = agora
    contato["ultimo_envio"] = agora
    if tipo == FOLLOWUP:
        contato["followups"] = str(_inteiro(contato.get("followups")) + 1)
    if salvar:
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
    return dias_uteis_entre(referencia, agora or datetime.now()) >= dias


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


class SessaoWeb:
    """WhatsApp Web via Selenium, com UM navegador reaproveitado na sessão.

    O texto entra na conversa pelo próprio link `send?text=` (que preenche a
    caixa de mensagem). Não usamos `send_keys` para digitar: o ChromeDriver
    não aceita emoji (fora do BMP) e digitar por cima do texto pré-preenchido
    duplicaria a mensagem.
    """

    SELETORES = {
        "caixa": ('footer div[contenteditable="true"], '
                  'div[contenteditable="true"][data-tab="10"], '
                  'div[contenteditable="true"][data-tab="6"]'),
        "lista_chats": '#pane-side',
        "qr": "canvas[aria-label*='QR' i], [data-ref] canvas",
        "dialogo": "[role='dialog'], [data-animate-modal-popup]",
        "bolha": "#main [data-pre-plain-text]",
        "bolha_texto": "[data-testid='selectable-text']",
        "anexar": ('div[title="Anexar"], div[title="Attach"], '
                   'span[data-icon="plus"], span[data-icon="clip"]'),
        "enviar": 'span[data-icon="send"]',
    }
    SELETOR_CAIXA = SELETORES["caixa"]

    def healthcheck(self, segundos: int = 25) -> dict:
        """Verifica se o WhatsApp Web está logado e com os seletores esperados."""
        from selenium.webdriver.common.by import By
        driver = self._garantir_driver()
        driver.get("https://web.whatsapp.com/")
        fim = time.time() + segundos
        status = "timeout"
        while time.time() < fim:
            if driver.find_elements(By.CSS_SELECTOR, self.SELETORES["lista_chats"]):
                status = "conectado"
                break
            if driver.find_elements(By.CSS_SELECTOR, self.SELETORES["qr"]):
                status = "deslogado"
                break
            time.sleep(0.5)
        return {
            "status": status,
            "seletores_ativos": {
                k: len(driver.find_elements(By.CSS_SELECTOR, v))
                for k, v in self.SELETORES.items()
            },
        }

    def __init__(self) -> None:
        self.driver = None
        self._trava: TravaEnvio | None = None
        self.ultimo_erro: str | None = None
        self.confirmado = False

    def _garantir_driver(self):
        if self.driver is None:
            # O perfil do WhatsApp Web só pode ser usado por um processo por vez.
            if self._trava is None:
                trava = TravaEnvio()
                trava.__enter__()  # levanta EnvioEmAndamento se já houver outro
                self._trava = trava
            from selenium import webdriver
            opcoes = webdriver.ChromeOptions()
            for flag in opcoes_janela(JANELA_VISIVEL):
                opcoes.add_argument(flag)
            # A janela fica fora da tela/atrás de outras: sem isto o Chrome
            # desacelera a aba e a conferência de entrega fica lenta/inconclusiva.
            for flag in ("--disable-background-timer-throttling",
                         "--disable-backgrounding-occluded-windows",
                         "--disable-renderer-backgrounding"):
                opcoes.add_argument(flag)
            opcoes.add_argument(f"--user-data-dir={PERFIL_SELENIUM}")  # mantém o login
            self.driver = webdriver.Chrome(options=opcoes)
            self.aberto_em = time.time()
            print("\n[AGUARDE] Escaneie o QR Code se necessário (o perfil salvo "
                  "acelera nas próximas vezes)...")
        return self.driver

    # Resultado do último envio (para o modo automático decidir o que fazer)
    SEM_WHATSAPP = "sem_whatsapp"        # número não tem conta no WhatsApp
    DESLOGADO = "deslogado"              # WhatsApp Web pedindo QR Code
    TEXTO_NAO_CONFERE = "texto_nao_confere"
    NAO_CONFIRMADO = "nao_confirmado"    # ENTER dado, mas a bolha não foi vista
    TECNICO = "tecnico"
    BLOQUEADO = "bloqueado"
    CANCELADO = "cancelado"

    def enviar(self, contato: dict, mensagem: str,
               permitir_reenvio: bool = False, confirmar: bool = True) -> bool:
        """Envia UMA mensagem. Retorna True se o ENTER foi dado.

        Depois do retorno, `self.ultimo_erro` diz por que falhou (ou None) e
        `self.confirmado` diz se a bolha enviada foi lida de volta na conversa
        com exatamente o texto aprovado.

        `confirmar=False` (modo automático) pula as perguntas ao operador; todas
        as checagens automáticas continuam valendo.
        """
        self.ultimo_erro = None
        self.confirmado = False
        if not permitir_reenvio and enviado_hoje(contato["telefone"]):
            print(f"[BLOQUEADO] +{contato['telefone']} já recebeu mensagem hoje "
                  "— envio duplicado evitado.")
            self.ultimo_erro = self.BLOQUEADO
            return False
        _cabecalho_alvo(contato)
        print("=" * 64)
        if confirmar and input("\nPreparar o envio no WhatsApp Web (Selenium)? [s/N]: "
                               ).strip().lower() != "s":
            print("[SKIP] Envio cancelado pelo operador.")
            self.ultimo_erro = self.CANCELADO
            return False

        try:
            from selenium.webdriver.common.keys import Keys
            driver = self._garantir_driver()
        except ImportError:
            print("[ERRO] Selenium não instalado. Rode: pip install -r requirements.txt")
            self.ultimo_erro = self.TECNICO
            return False
        except EnvioEmAndamento as exc:
            print(f"[BLOQUEADO] {exc}")
            self.ultimo_erro = self.BLOQUEADO
            return False
        except Exception as exc:  # noqa: BLE001
            print(f"[ERRO] Não foi possível abrir o Chrome: {exc}")
            self.ultimo_erro = self.TECNICO
            self.fechar()
            return False

        enter_dado = False
        try:
            driver.get(montar_link(contato, mensagem))
            caixa, situacao = self._esperar_conversa(driver)
            if situacao == self.SEM_WHATSAPP:
                print(f"[SEM WHATSAPP] +{contato['telefone']} não está no WhatsApp.")
                self.ultimo_erro = self.SEM_WHATSAPP
                return False
            if situacao == self.DESLOGADO:
                print("[ERRO] WhatsApp Web desconectado (pedindo QR Code).")
                self.ultimo_erro = self.DESLOGADO
                return False
            if caixa is None:
                print("[ERRO] A conversa não abriu a tempo.")
                self.ultimo_erro = self.TECNICO
                return False

            # Espera o WhatsApp terminar de preencher a caixa com o texto do link.
            # NUNCA digitamos/colamos por cima: isso já duplicou mensagens
            # (cópia sem quebras de linha misturada ao texto original).
            confere = False
            for _ in range(40):  # até ~20s
                time.sleep(0.5)
                caixa = self._caixa_atual(driver) or caixa  # o WhatsApp recria a caixa
                if texto_confere(self._texto(caixa), mensagem):
                    confere = True
                    break
            if not confere:
                print("[ERRO] O texto na caixa não confere com a mensagem aprovada "
                      "(faltando ou duplicado) — NADA foi enviado.")
                self._limpar_caixa(caixa)
                self.ultimo_erro = self.TEXTO_NAO_CONFERE
                return False

            if confirmar:
                print("\n" + "=" * 64)
                print("MENSAGEM NA CAIXA DA CONVERSA, MAS *NÃO* ENVIADA.")
                print("Confira no navegador. Só envie se você revisar o texto.")
                print("=" * 64)
                if input("ENVIAR AGORA? Digite 'ENVIAR' (ou qualquer outra coisa "
                         "para abortar): ").strip() != "ENVIAR":
                    print("[SKIP] Envio abortado pelo operador (nada foi enviado).")
                    self._limpar_caixa(caixa)
                    self.ultimo_erro = self.CANCELADO
                    return False

            # Última conferência: alguém pode ter enviado enquanto preparávamos.
            if not permitir_reenvio and enviado_hoje(contato["telefone"]):
                print("[BLOQUEADO] Este número recebeu mensagem durante a preparação "
                      "— envio duplicado evitado.")
                self._limpar_caixa(caixa)
                self.ultimo_erro = self.BLOQUEADO
                return False
            # ...e o texto ainda é exatamente o aprovado?
            caixa = self._caixa_atual(driver) or caixa
            if not texto_confere(self._texto(caixa), mensagem):
                print("[ERRO] O texto na caixa mudou — NADA foi enviado.")
                self._limpar_caixa(caixa)
                self.ultimo_erro = self.TEXTO_NAO_CONFERE
                return False

            antes = self._contar_bolhas_iguais(driver, mensagem)
            caixa.send_keys(Keys.ENTER)
            enter_dado = True

            # Confirmação: apareceu na conversa UMA bolha nova com exatamente o
            # texto aprovado? (Conta bolhas iguais, então uma resposta automática
            # da clínica chegando logo depois não atrapalha.)
            for _ in range(30):  # até ~15s
                time.sleep(0.5)
                if self._contar_bolhas_iguais(driver, mensagem) == antes + 1:
                    self.confirmado = True
                    break
            if self.confirmado:
                # A bolha aparece antes da transmissão terminar: espera um pouco
                # antes de navegar/fechar o Chrome para não perder a mensagem.
                time.sleep(6)
                print("[OK] Mensagem enviada e conferida na conversa.")
            else:
                print("[ALERTA] ENTER dado, mas a mensagem enviada não foi conferida "
                      "na conversa — verifique manualmente.")
                self.ultimo_erro = self.NAO_CONFIRMADO
            return True

        except Exception as exc:  # noqa: BLE001 - seletores da Meta mudam com frequência
            print(f"[ERRO] Falha na automação do WhatsApp Web: {exc}")
            self.fechar()  # navegador pode ter sido fechado; reabre no próximo
            if enter_dado:
                # A mensagem PODE ter saído: tratar como enviada não conferida,
                # para registrar no histórico e nunca reenviar por engano.
                print("[ALERTA] O erro ocorreu depois do ENTER — considere a "
                      "mensagem enviada e verifique manualmente.")
                self.ultimo_erro = self.NAO_CONFIRMADO
                return True
            print("       Alternativa: use --modo manual")
            self.ultimo_erro = self.TECNICO
            return False

    def _caixa_atual(self, driver):
        """Caixa de mensagem atual (o WhatsApp recria o elemento ao carregar)."""
        from selenium.webdriver.common.by import By
        caixas = driver.find_elements(By.CSS_SELECTOR, self.SELETOR_CAIXA)
        return caixas[0] if caixas else None

    @staticmethod
    def _texto(elemento) -> str:
        """innerText tolerante a elemento obsoleto ('' se sumiu)."""
        try:
            return elemento.get_attribute("innerText") or ""
        except Exception:  # noqa: BLE001 - StaleElementReference etc.
            return ""

    def _esperar_conversa(self, driver, segundos: int = 60):
        """Espera a caixa de mensagem OU um motivo para desistir.

        Retorna (caixa, None) | (None, SEM_WHATSAPP) | (None, DESLOGADO) |
        (None, None) no tempo esgotado.
        """
        from selenium.webdriver.common.by import By
        qr_visto_desde = None
        fim = time.time() + segundos
        while time.time() < fim:
            caixas = driver.find_elements(By.CSS_SELECTOR, self.SELETOR_CAIXA)
            if caixas:
                return caixas[0], None
            for dialogo in driver.find_elements(
                    By.CSS_SELECTOR, "[role='dialog'], [data-animate-modal-popup]"):
                texto = self._texto(dialogo).lower()
                if "não está no whatsapp" in texto or "inválido" in texto \
                        or "not on whatsapp" in texto or "invalid" in texto:
                    return None, self.SEM_WHATSAPP
            if driver.find_elements(By.CSS_SELECTOR,
                                    "canvas[aria-label*='QR' i], [data-ref] canvas"):
                qr_visto_desde = qr_visto_desde or time.time()
                if time.time() - qr_visto_desde > 20:  # dá tempo de reconectar
                    return None, self.DESLOGADO
            time.sleep(0.5)
        return None, None

    @staticmethod
    def _contar_bolhas_iguais(driver, mensagem: str) -> int:
        """Quantas mensagens visíveis na conversa têm exatamente este texto."""
        from selenium.webdriver.common.by import By
        total = 0
        for bolha in driver.find_elements(By.CSS_SELECTOR,
                                          "#main [data-pre-plain-text]"):
            try:
                textos = bolha.find_elements(By.CSS_SELECTOR,
                                             "[data-testid='selectable-text']")
                texto = (textos[0] if textos else bolha).get_attribute("innerText") or ""
            except Exception:  # noqa: BLE001 - bolha re-renderizada; conta na próxima
                continue
            if texto_confere(texto, mensagem):
                total += 1
        return total

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

    def enviar_arquivo(self, caminho) -> bool:
        """Anexa um arquivo (ex.: demonstracao.pdf) na conversa aberta.

        Melhor esforço: a mensagem de texto já foi enviada antes — falha aqui
        só avisa o operador para anexar manualmente; nunca desfaz nem reenvia.
        """
        if self.driver is None:
            return False
        caminho = Path(caminho)
        if not caminho.is_file():
            print(f"[AVISO] Demonstração não encontrada: {caminho.name}")
            return False
        try:
            from selenium.webdriver.common.by import By
            driver = self.driver
            botoes = [b for b in driver.find_elements(
                By.CSS_SELECTOR,
                'div[title="Anexar"], div[title="Attach"], span[data-icon="plus"], '
                'span[data-icon="clip"]') if b.is_displayed()]
            if not botoes:
                print("[AVISO] Botão de anexo não encontrado — anexe o PDF "
                      "manualmente no WhatsApp Web.")
                return False
            botoes[0].click()
            time.sleep(1.0)
            alvo = None
            for item in driver.find_elements(
                    By.CSS_SELECTOR, '[role="menuitem"], [role="button"]'):
                rotulo = (item.get_attribute("innerText") or "").strip().lower()
                if "document" in rotulo or item.find_elements(
                        By.CSS_SELECTOR, 'span[data-icon="document"]'):
                    alvo = item
                    break
            if alvo is None:
                print("[AVISO] Opção 'Documento' não encontrada — anexe o PDF "
                      "manualmente no WhatsApp Web.")
                return False
            alvo.click()
            entradas = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
            if not entradas:
                print("[AVISO] Seletor de arquivo não encontrado — anexe o PDF "
                      "manualmente no WhatsApp Web.")
                return False
            try:
                entradas[0].send_keys(str(caminho.resolve()))
            except Exception:  # noqa: BLE001 - input oculto em algumas versões
                driver.execute_script("arguments[0].style.display='block';",
                                      entradas[0])
                entradas[0].send_keys(str(caminho.resolve()))
            # tela de conferência do documento: espera o botão de envio visível
            botao_envio = None
            for _ in range(40):  # até ~20s
                time.sleep(0.5)
                visiveis = [b for b in driver.find_elements(
                    By.CSS_SELECTOR, 'span[data-icon="send"]') if b.is_displayed()]
                if visiveis:
                    botao_envio = visiveis[0]
                    break
            if botao_envio is None:
                print("[AVISO] Documento selecionado, mas a tela de envio não "
                      "abriu — confira no WhatsApp Web e envie manualmente.")
                return False
            botao_envio.click()
            time.sleep(4)
            print(f"[OK] {caminho.name} anexado na conversa.")
            return True
        except Exception as exc:  # noqa: BLE001 - seletores da Meta mudam
            print(f"[AVISO] Falha ao anexar {caminho.name} ({exc}) — anexe o "
                  "PDF manualmente no WhatsApp Web.")
            return False

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
          f"{'SEG':<6} | {'STATUS':<12} | RESPOSTA")
    print("-" * 113)
    for i, c in enumerate(contatos, start=1):
        fixo = "" if eh_celular(c["telefone"]) else " (fixo)"
        print(f"{i:>3} | {c['clinica'][:28]:<28} | {c['bairro'][:14]:<14} | "
              f"{c['telefone']:<15} | {(c.get('segmento') or '-'):<6} | "
              f"{c['status']:<12} | {c.get('resposta') or '-'}{fixo}")


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
            est = input("Estágio comercial? [1] reunião  [2] proposta  "
                        "[3] fechado  [4] perdido  [ENTER] manter: ").strip()
            mapa_est = {"1": "reuniao_agendada", "2": "proposta_enviada",
                        "3": "fechado", "4": "perdido"}
            if est in mapa_est:
                c["estagio"] = mapa_est[est]
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

                # Mesma clínica já contatada por outro número?
                if chave_clinica(contato) in clinicas_ja_contatadas(planilha.contatos):
                    print(f"\n[ALERTA] {contato['clinica']} parece ser uma clínica já "
                          "contatada por outro número.")
                    if input("Enviar mesmo assim? [s/N]: ").strip().lower() != "s":
                        print("[SKIP] Mesma clínica — não enviado.")
                        indice += 1
                        continue

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
# 6. MODO AUTOMÁTICO (agendado) — sem operador, com checagens em cada etapa
# ---------------------------------------------------------------------------

PASTA_LOGS = BASE_DIR / "logs"
TEXTOS_PROIBIDOS = ("proimagem", "prévia", "previa")


# Nomes que são só a especialidade (o Maps às vezes põe a marca depois do "|").
NOMES_GENERICOS = {
    "implantesdentarios", "implantedentario", "dentista", "dentistas", "odontologia",
    "clinicaodontologica", "consultorioodontologico", "clinicamedica", "clinica",
    "consultorio", "ortodontia", "ortodontista", "centromedico", "clinicadesaude",
    "dentista24horas", "dentista24h", "urgenciaodontologica",
}


LIMITES_TAMANHO = {"completa": (200, 1500), "curta": (80, 400),
                   "apresentacao": (150, 1300), "resumo": (150, 1300),
                   "followup": (60, 500), "despedida": (60, 400)}


def validar_mensagem(mensagem: str, contato: dict, tipo: str = "completa") -> str | None:
    """Checagem final de uma mensagem gerada. Retorna o problema ou None.

    No modo automático ninguém revisa o texto: qualquer coisa estranha faz o
    contato ser pulado (e registrado no log) em vez de enviado.
    """
    nome = nome_para_mensagem(contato["clinica"], contato.get("bairro", ""),
                              contato.get("cidade") or CIDADE_PADRAO)
    if "{" in mensagem or "}" in mensagem or "\x00" in mensagem:
        return "token/spintax não resolvido"
    if "|" in mensagem:
        return "nome com lixo de SEO ('|')"
    baixo = mensagem.lower()
    for proibido in TEXTOS_PROIBIDOS:
        if proibido in baixo:
            return f"texto proibido: '{proibido}'"
    if tipo not in ("apresentacao", "resumo") and nome not in mensagem:
        return "nome da clínica ausente"
    if chave_clinica({"clinica": nome}) in NOMES_GENERICOS:
        return f"nome genérico, sem a marca da clínica: '{nome}' (corrija no CSV)"
    if len(nome) > 50 or len(re.findall(r"[^\W\d_]", nome)) < 3:
        return f"nome suspeito para envio automático: '{nome}'"
    minimo, maximo = LIMITES_TAMANHO.get(tipo, (200, 1500))
    if not minimo <= len(mensagem) <= maximo:
        return f"tamanho fora do padrão ({len(mensagem)} caracteres)"
    linhas = [l.strip() for l in mensagem.splitlines() if len(l.strip()) > 20]
    if len(linhas) != len(set(linhas)):
        return "parágrafo repetido"
    return None


def _registrar(texto: str) -> None:
    """Imprime e grava no log do dia (logs/automatico_AAAA-MM-DD.log)."""
    linha = f"{datetime.now():%H:%M:%S} {texto}"
    print(linha, flush=True)
    try:
        PASTA_LOGS.mkdir(exist_ok=True)
        with (PASTA_LOGS / f"automatico_{datetime.now():%Y-%m-%d}.log").open(
                "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


def conectar_whatsapp(segundos: int = 300) -> bool:
    """Abre o WhatsApp Web com o perfil do script e espera o login (QR Code)."""
    from selenium.webdriver.common.by import By
    sessao = SessaoWeb()
    try:
        driver = sessao._garantir_driver()
        driver.get("https://web.whatsapp.com")
        print(f"Escaneie o QR Code no Chrome (até {segundos // 60} min)...")
        fim = time.time() + segundos
        while time.time() < fim:
            if driver.find_elements(By.CSS_SELECTOR, "#pane-side, #side"):
                time.sleep(5)  # deixa sincronizar
                print("[OK] WhatsApp Web conectado. O envio automático já pode rodar.")
                return True
            time.sleep(1)
        print("[ERRO] Não conectou a tempo. Rode --conectar de novo.")
        return False
    except EnvioEmAndamento as exc:
        print(f"[BLOQUEADO] {exc}")
        return False
    finally:
        sessao.fechar()


BLOQUEIO_NOMES = ("proimagem",)  # nunca contatar (empregador do operador)
STATUS_MESMA_CLINICA = "mesma_clinica"
STATUS_BLOQUEADO = "bloqueado"


def chave_clinica(contato: dict) -> str:
    """Identidade da clínica pelo nome limpo, sem acento/caixa/pontuação.

    "Clínica CALE - Consultas | Exames" e "Clinica Cale" viram a mesma chave,
    para não mandar duas mensagens à mesma clínica com números diferentes.
    """
    import unicodedata
    nome = nome_para_mensagem(contato["clinica"], contato.get("bairro", ""),
                              contato.get("cidade") or CIDADE_PADRAO)
    sem_acento = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", sem_acento.lower())


def nome_bloqueado(contato: dict) -> bool:
    baixo = contato["clinica"].lower()
    return any(b in baixo for b in BLOQUEIO_NOMES)


def clinicas_ja_contatadas(contatos: list[dict]) -> set[str]:
    return {chave_clinica(c) for c in contatos if c["status"] in (ENVIADO, NAO_CONTATAR)}


# --- Leitura das conversas (somente leitura) --------------------------------

JS_BOLHAS = """
return [...document.querySelectorAll('#main [data-pre-plain-text]')].map(b => {
  const t = b.querySelector('[data-testid="selectable-text"]');
  return [b.getAttribute('data-pre-plain-text') || '', (t || b).innerText || ''];
});
"""


def ler_conversa(sessao: "SessaoWeb", numero: str,
                 esperar_texto: str | None = None) -> list[dict] | str:
    """Abre a conversa SEM texto (nada é digitado) e devolve as bolhas visíveis.

    O histórico carrega aos poucos (principalmente logo após abrir o Chrome):
    lê de novo até a lista estabilizar; com `esperar_texto`, espera até ~20s
    essa mensagem aparecer antes de concluir que ela não está lá.
    Retorna a lista de bolhas, ou o código de erro (sem_whatsapp/deslogado/tecnico).
    """
    driver = sessao._garantir_driver()
    driver.get(f"https://web.whatsapp.com/send?phone={numero}")
    caixa, situacao = sessao._esperar_conversa(driver, segundos=45)
    if situacao:
        return situacao
    if caixa is None:
        return SessaoWeb.TECNICO

    # O histórico carrega em etapas, com pausas de vários segundos entre elas
    # (medido: só a última bolha aparece primeiro). Por isso não basta a lista
    # "parar de mudar" por 1–2s.
    bolhas: list[dict] = []
    anterior, estavel = -1, 0
    inicio = time.time()
    while time.time() - inicio < 30:
        time.sleep(1)
        try:
            bolhas = [interpretar_bolha(pre, txt)
                      for pre, txt in driver.execute_script(JS_BOLHAS)]
        except Exception:  # noqa: BLE001 - re-renderização; tenta de novo
            continue
        if esperar_texto:
            if any(texto_confere(b["texto"], esperar_texto) for b in bolhas):
                break
            continue
        estavel = estavel + 1 if len(bolhas) == anterior else 0
        anterior = len(bolhas)
        if estavel >= 5 and time.time() - inicio >= 8:
            break
    return bolhas


def mensagens_enviadas_por_numero(desde_dias: int = 30) -> dict[str, list[dict]]:
    """Histórico agrupado por telefone (mais antigo -> mais recente)."""
    limite = datetime.now().timestamp() - desde_dias * 86400
    grupos: dict[str, list[dict]] = {}
    for r in ler_historico():
        quando = _data(r.get("timestamp", ""))
        if quando and quando.timestamp() >= limite and r.get("telefone"):
            grupos.setdefault(r["telefone"], []).append(r)
    return grupos


def verificar_conversas(planilha: Planilha, sessao: "SessaoWeb",
                        max_conversas: int = 40) -> dict:
    """Uma passada de leitura: confere as entregas de hoje e lê respostas.

    - Entregas: para cada número que recebeu hoje, a última mensagem enviada
      aparece exatamente 1 vez na conversa? (0 = não chegou, 2+ = duplicada)
    - Respostas: classifica e atualiza o CSV (respondeu / resposta automatica /
      sem interesse -> nao_contatar).
    """
    hoje = datetime.now().date()
    historico = mensagens_enviadas_por_numero(30)
    por_tel = {c["telefone"]: c for c in planilha.contatos}

    conferir = {tel for tel, regs in historico.items()
                if any((_data(r["timestamp"]) or datetime.min).date() == hoje for r in regs)}
    responder = {c["telefone"] for c in planilha.contatos
                 if c["status"] == ENVIADO and c["telefone"] in historico
                 and ((c.get("resposta") or "") in RESPOSTAS_EM_ABERTO
                      # pessoa respondeu à abertura curta e a apresentação ainda não foi
                      or (c.get("resposta") == "respondeu" and _segunda_etapa(c)))}
    relatorio = {"conferidas": 0, "nao_verificaveis": 0, "problemas": [], "respostas": [],
                 "optouts": [], "automaticas": [], "erro": None,
                 "acoes": [], "respostas_texto": []}

    for tel in sorted(conferir | responder)[:max_conversas]:
        contato = por_tel.get(tel, {"clinica": tel})
        enviadas = [r.get("mensagem", "") for r in historico.get(tel, [])]
        bolhas = ler_conversa(sessao, tel,
                              esperar_texto=enviadas[-1] if tel in conferir and enviadas
                              else None)
        if isinstance(bolhas, str):
            if bolhas == SessaoWeb.DESLOGADO:
                relatorio["erro"] = "WhatsApp Web desconectado"
                break
            continue
        if tel in conferir and enviadas:
            enviado_em = _data(historico[tel][-1].get("timestamp", "")) or datetime.now()
            resultado = resultado_entrega(bolhas, enviadas[-1], enviado_em)
            if resultado == "inconclusivo":  # histórico ainda vindo do celular
                releitura = ler_conversa(sessao, tel, esperar_texto=enviadas[-1])
                if not isinstance(releitura, str):
                    bolhas = releitura
                    resultado = resultado_entrega(bolhas, enviadas[-1], enviado_em)
            if resultado == "ok":
                relatorio["conferidas"] += 1
            elif resultado == "inconclusivo":
                relatorio["nao_verificaveis"] += 1
            else:
                relatorio["problemas"].append(
                    f"{contato['clinica'][:40]}: mensagem "
                    f"{'NÃO encontrada' if resultado == 'ausente' else 'DUPLICADA'}")
        if tel in responder and "status" in contato:
            desde = _data(contato.get("ultimo_envio") or contato.get("atualizado_em") or "")
            if desde is None:
                continue
            resposta = classificar_resposta(bolhas, enviadas, desde)
            primeiro = _data(historico[tel][0].get("timestamp", "")) or desde
            recebidas = mensagens_recebidas(bolhas, enviadas, primeiro)
            alterado = False
            if resposta == "respondeu" and _segunda_etapa(contato):
                ultimas = [b["texto"] for b in mensagens_recebidas(bolhas, enviadas, desde)]
                if resposta_negativa(ultimas):
                    if contato.get("etapa") != "recusou":
                        contato["etapa"] = "recusou"  # não insiste; o operador decide
                        alterado = True
                else:
                    relatorio["acoes"].append((_segunda_etapa(contato), tel, None))
            if resposta != "respondeu" and resposta != "sem interesse" \
                    and not contato.get("menu"):
                resp_robo = resposta_para_robo([b["texto"] for b in recebidas])
                if resp_robo:
                    relatorio["acoes"].append(("menu", tel, resp_robo))
            if resposta and resposta != contato.get("resposta"):
                contato["resposta"] = resposta
                contato["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
                nome = nome_para_mensagem(contato["clinica"], contato.get("bairro", ""),
                                          contato.get("cidade") or CIDADE_PADRAO)
                if resposta == "sem interesse":
                    contato["status"] = NAO_CONTATAR
                    relatorio["optouts"].append(nome)
                    if contato.get("etapa") != "despedida":
                        relatorio["acoes"].append(("despedida", tel, None))
                elif resposta == "respondeu":
                    relatorio["respostas"].append(nome)
                    desde_min = desde.replace(second=0, microsecond=0)
                    ultimas = [b for b in recebidas if b["quando"] and b["quando"] >= desde_min]
                    relatorio["respostas_texto"].append(
                        (nome, (ultimas[-1]["texto"] if ultimas else "")[:200]))
                else:
                    relatorio["automaticas"].append(nome)
                alterado = True
            if alterado:
                planilha.salvar()
    return relatorio


# --- Avisos -------------------------------------------------------------------

def notificar_sistema(titulo: str, texto: str) -> None:
    """Notificação nativa (Windows Toast, Linux notify-send, macOS osascript)."""
    import subprocess
    if sys.platform.startswith("linux"):
        try:
            subprocess.run(["notify-send", titulo, texto[:250]],
                           timeout=10, capture_output=True)
        except Exception:  # noqa: BLE001
            pass
        return
    if sys.platform == "darwin":
        try:
            t_esc = titulo.replace('"', '\\"')
            m_esc = texto[:250].replace('"', '\\"')
            subprocess.run(["osascript", "-e",
                            f'display notification "{m_esc}" with title "{t_esc}"'],
                           timeout=10, capture_output=True)
        except Exception:  # noqa: BLE001
            pass
        return
    if sys.platform != "win32":
        return
    import base64
    from xml.sax.saxutils import escape
    xml = ("<toast><visual><binding template='ToastGeneric'>"
           f"<text>{escape(titulo)}</text><text>{escape(texto[:250])}</text>"
           "</binding></visual></toast>")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
        " ContentType = WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml(@'\n{xml}\n'@);"
        "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app)"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-EncodedCommand",
                        base64.b64encode(script.encode("utf-16-le")).decode()],
                       timeout=30, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


notificar_windows = notificar_sistema  # alias de compatibilidade


def montar_resumo(enviadas: list[str], sem_whatsapp: list[str], puladas: list[str],
                  verificacao: dict | None, pendentes: int, limite_dia: int,
                  parada: str | None, acoes: list[str] | None = None,
                  ab: str = "") -> str:
    """Texto do resumo diário enviado para o próprio WhatsApp do operador."""
    linhas = [f"Prospecção {datetime.now():%d/%m} — resumo automático", ""]
    linhas.append(f"Enviadas: {len(enviadas)}"
                  + (f" ({', '.join(enviadas)})" if enviadas else ""))
    if verificacao:
        linhas.append(f"Entregas conferidas hoje: {verificacao['conferidas']}")
        if verificacao.get("nao_verificaveis"):
            linhas.append(f"Sem histórico no navegador (já conferidas no envio): "
                          f"{verificacao['nao_verificaveis']}")
        for p in verificacao["problemas"]:
            linhas.append(f"ATENÇÃO: {p}")
        textos = dict(verificacao.get("respostas_texto") or [])
        for nome in verificacao["respostas"]:
            trecho = textos.get(nome, "")
            linhas.append(f"📩 RESPONDEU: {nome}" + (f" — “{trecho}”" if trecho else ""))
        if verificacao["optouts"]:
            linhas.append(f"Pediram para não contatar: {', '.join(verificacao['optouts'])}")
        if verificacao["automaticas"]:
            linhas.append(f"Só resposta automática: {', '.join(verificacao['automaticas'])}")
    for acao in acoes or []:
        linhas.append(f"Ação automática: {acao}")
    if sem_whatsapp:
        linhas.append(f"Sem WhatsApp (removidos da fila): {len(sem_whatsapp)}")
    if puladas:
        linhas.append(f"Puladas: {len(puladas)}")
    dias = pendentes // max(limite_dia, 1)
    linhas.append(f"Fila: {pendentes} pendente(s) (~{dias} dia(s) útil(eis))")
    if pendentes < 2 * limite_dia:
        linhas.append("Fila acabando: rode o minerador com novos termos.")
    if ab:
        linhas.append(ab)
    if parada:
        linhas.append(f"PARADA: {parada}")
    return "\n".join(linhas)


def _resumo_contato(numero: str) -> dict:
    return {"clinica": "Resumo diário", "bairro": "", "cidade": CIDADE_PADRAO,
            "telefone": normalizar_telefone(numero)}


# --- Execução diária ----------------------------------------------------------

def _envio_automatico(sessao: "SessaoWeb", contato: dict, mensagem: str,
                      permitir_reenvio: bool = False) -> str:
    """Envia sem operador (1 retentativa se o texto não conferir).

    Retorna "ok", "nao_conferido" (ENTER dado, sem confirmação) ou o código
    de erro do SessaoWeb.
    """
    ok = sessao.enviar(contato, mensagem, permitir_reenvio=permitir_reenvio,
                       confirmar=False)
    if not ok and sessao.ultimo_erro == SessaoWeb.TEXTO_NAO_CONFERE:
        _registrar(f"[RETENTATIVA] {contato['clinica'][:50]}: texto não conferiu")
        ok = sessao.enviar(contato, mensagem, permitir_reenvio=permitir_reenvio,
                           confirmar=False)
    if ok:
        return "ok" if sessao.confirmado else "nao_conferido"
    return sessao.ultimo_erro or SessaoWeb.TECNICO


def _segunda_etapa(contato: dict) -> str | None:
    """Qual mensagem automática a pessoa recebe ao responder, se alguma:

    - curta, depois da abertura -> "apresentacao";
    - completa, depois do texto completo -> "resumo" (só para contatos dos
      textos versionados, cujo CTA promete o resumo; quem recebeu os textos
      antigos e respondeu é atendido à mão, como antes).
    """
    variante, etapa = contato.get("variante"), contato.get("etapa")
    if variante == "curta" and etapa == "abertura":
        return "apresentacao"
    if variante == "completa" and etapa == "apresentacao" and contato.get("versao_textos"):
        return "resumo"
    return None


def executar_acoes(relatorio: dict, planilha: Planilha, sessao: "SessaoWeb",
                   mensagens: dict, args: argparse.Namespace) -> list[str]:
    """Executa o que a leitura das conversas pediu:

    - "apresentacao": pessoa respondeu à abertura curta -> envia o texto completo;
    - "resumo": pessoa respondeu à mensagem completa -> envia o resumo prometido;
    - "despedida": pessoa disse que não tem interesse -> agradece, formal (uma vez);
    - "menu": robô com menu -> escolhe "falar com atendente" (uma vez por clínica).
    """
    feitos: list[str] = []
    por_tel = {c["telefone"]: c for c in planilha.contatos}
    for tipo, tel, dado in relatorio.get("acoes", []):
        contato = por_tel.get(tel)
        if contato is None:
            continue
        nome = nome_para_mensagem(contato["clinica"], contato.get("bairro", ""),
                                  contato.get("cidade") or CIDADE_PADRAO)
        if tipo in ("apresentacao", "resumo"):
            rotulo = "apresentação" if tipo == "apresentacao" else "resumo"
            if segundas_etapas_hoje() >= args.limite_dia:  # teto de segurança
                _registrar(f"[ADIADO] {rotulo} para {nome}: já foram "
                           f"{args.limite_dia} apresentações/resumos hoje.")
                continue
            montar = montar_apresentacao if tipo == "apresentacao" else montar_resumo_completa
            mensagem = montar(contato, mensagens)
            problema = validar_mensagem(mensagem, contato, tipo)
            if problema:
                _registrar(f"[PULADO] {rotulo} para {nome}: {problema}")
                continue
        elif tipo == "despedida":
            if contato.get("etapa") == "despedida":
                continue
            mensagem = montar_despedida(contato, mensagens)
            problema = validar_mensagem(mensagem, contato, "despedida")
            if problema:
                _registrar(f"[PULADO] despedida para {nome}: {problema}")
                continue
        else:
            mensagem = str(dado)
        restante = segundos_de_espera(ultimo_envio(),
                                      random.randint(args.intervalo_min, args.intervalo_max),
                                      datetime.now())
        if restante:
            time.sleep(restante)
        resultado = _envio_automatico(sessao, contato, mensagem, permitir_reenvio=True)
        if resultado not in ("ok", "nao_conferido"):
            _registrar(f"[FALHA] {tipo} para {nome}: {resultado}")
            if resultado == SessaoWeb.DESLOGADO:
                break
            continue
        atualizar_historico(contato, mensagem, "automatico", tipo)
        if tipo in ("apresentacao", "resumo"):
            marcar_enviado(contato, planilha, tipo)  # volta a "aguardando"
            contato["etapa"] = tipo
            feitos.append(f"{'apresentação enviada' if tipo == 'apresentacao' else 'resumo enviado'}"
                          f" para {nome}")
            pdf = demonstracao_pdf(mensagens)
            if pdf is not None:
                if sessao.enviar_arquivo(pdf):
                    _registrar(f"[AÇÃO] {pdf.name} anexado para {nome}")
                else:
                    _registrar(f"[AVISO] {pdf.name} não anexado para {nome} — "
                               "anexe manualmente")
        elif tipo == "despedida":
            contato["etapa"] = "despedida"  # status continua nao_contatar
            contato["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
            feitos.append(f"agradecimento final enviado para {nome} (sem interesse)")
        else:
            contato["menu"] = "respondido"
            feitos.append(f"opção {mensagem} (falar com atendente) escolhida no robô de {nome}")
        planilha.salvar()
        _registrar(f"[AÇÃO] {feitos[-1]}")
    return feitos


def avisar_respostas(relatorio: dict, sessao: "SessaoWeb", destino: str | None) -> None:
    """Avisa NA HORA quando uma pessoa responde (WhatsApp do operador + Windows)."""
    respostas = relatorio.get("respostas_texto") or []
    optouts = relatorio.get("optouts") or []
    if not respostas and not optouts:
        return
    linhas = ["📩 Prospecção: clínica respondeu!", ""]
    for nome, texto in respostas:
        linhas.append(f"• {nome}: “{texto}”" if texto else f"• {nome}")
    for nome in optouts:
        linhas.append(f"• {nome}: pediu para não ser contatada (removida da fila)")
    if respostas:
        linhas += ["", "Responda pelo WhatsApp para não esfriar a conversa."]
    texto = "\n".join(linhas)
    if destino:
        sessao.enviar(_resumo_contato(destino), texto, permitir_reenvio=True, confirmar=False)
    notificar_windows("Prospecção: clínica respondeu",
                      ", ".join([n for n, _ in respostas] + optouts))


def estatisticas_ab(contatos: list[dict], versao: str = "") -> str:
    """Linha do resumo com o resultado do teste A/B até agora.

    Com `versao`, conta só os contatos abordados com essa versão dos textos
    (mudar os textos no meio do teste misturaria resultados)."""
    partes = []
    for variante in ("curta", "completa"):
        grupo = [c for c in contatos if c.get("variante") == variante
                 and c["status"] in (ENVIADO, NAO_CONTATAR)
                 and (not versao or c.get("versao_textos") == versao)]
        humanas = sum(1 for c in grupo
                      if c.get("resposta") in ("respondeu", "sem interesse")
                      or c.get("etapa") == "resumo"
                      or c.get("etapa") == "apresentacao" and variante == "curta")
        if grupo:
            partes.append(f"{variante}: {humanas}/{len(grupo)} responderam")
    if not partes:
        return ""
    titulo = f"Teste A/B (textos de {versao})" if versao else "Teste A/B"
    return f"{titulo} — " + " | ".join(partes)


def rodar_acompanhamento(planilha: Planilha, args: argparse.Namespace,
                         mensagens: dict) -> None:
    """Rodada curta ao longo do dia (agendada): lê respostas, avisa na hora
    quando uma pessoa responde, envia a 2ª etapa (apresentação da curta ou
    resumo da completa) e
    escolhe "falar com atendente" em menus de robô. Não prospecta ninguém novo."""
    agora = datetime.now()
    if not dia_util(agora) or not dentro_da_janela(args.hora_inicio, args.hora_fim, agora):
        _registrar("[ACOMPANHAMENTO] Fora do dia útil/janela — nada a fazer.")
        return
    try:
        trava = TravaEnvio().__enter__()
    except EnvioEmAndamento as exc:
        _registrar(f"[ACOMPANHAMENTO] {exc}")
        return
    recarregada = carregar_planilha(planilha.caminho)
    planilha.linhas, planilha.contatos, planilha.colunas = (
        recarregada.linhas, recarregada.contatos, recarregada.colunas)
    sessao = SessaoWeb()
    try:
        relatorio = verificar_conversas(planilha, sessao)
        if relatorio["erro"]:
            _registrar(f"[ACOMPANHAMENTO] {relatorio['erro']}")
            notificar_windows("Prospecção: atenção", relatorio["erro"])
            return
        feitos = executar_acoes(relatorio, planilha, sessao, mensagens, args)
        avisar_respostas(relatorio, sessao, getattr(args, "resumo_para", None))
        _registrar(f"[ACOMPANHAMENTO] respostas {len(relatorio['respostas'])}, "
                   f"opt-out {len(relatorio['optouts'])}, ações {len(feitos)}")
    finally:
        sessao.fechar()
        trava.__exit__(None, None, None)


def rodar_automatico(planilha: Planilha, args: argparse.Namespace,
                     mensagens: dict) -> int:
    """Execução diária sem operador.

    1. Espera um atraso aleatório (não começa sempre no mesmo minuto).
    2. Envia a cota do dia: celulares primeiro, depois fixos (número sem
       WhatsApp é detectado sem enviar nada). Pula a mesma clínica com outro
       número e nomes bloqueados.
    3. Confere as entregas do dia e lê respostas (somente leitura).
    4. Manda o resumo para o próprio WhatsApp (--resumo-para) e avisa no
       Windows se algo deu errado ou se a fila está acabando.

    Para tudo (sem insistir) se: WhatsApp desconectado, envio sem confirmação
    na conversa ou 2 falhas técnicas seguidas. Retorna o número de envios.
    """
    agora = datetime.now()
    if not dia_util(agora):
        _registrar("[AUTO] Hoje não é dia útil (fim de semana/feriado) — nada a fazer.")
        return 0
    atraso = random.randint(0, max(0, getattr(args, "atraso_max", 0)) * 60)
    if atraso:
        _registrar(f"[AUTO] Início em {atraso // 60} min {atraso % 60} s (horário variado).")
        time.sleep(atraso)
    if not dentro_da_janela(args.hora_inicio, args.hora_fim):
        _registrar(f"[AUTO] Fora da janela {args.hora_inicio}h–{args.hora_fim}h — nada a fazer.")
        return 0

    try:
        trava = TravaEnvio().__enter__()
    except EnvioEmAndamento as exc:
        _registrar(f"[AUTO] {exc}")
        notificar_windows("Prospecção: não rodou", str(exc))
        return 0

    # Relê o CSV já com a trava: durante o atraso aleatório o minerador pode
    # ter incluído contatos — salvar a versão antiga os apagaria.
    recarregada = carregar_planilha(planilha.caminho)
    planilha.linhas, planilha.contatos, planilha.colunas = (
        recarregada.linhas, recarregada.contatos, recarregada.colunas)

    enviadas: list[str] = []
    sem_whatsapp: list[str] = []
    puladas: list[str] = []
    parada: str | None = None
    verificacao: dict | None = None
    acoes_feitas: list[str] = []
    sessao = SessaoWeb()
    try:
        cota = args.limite_dia - envios_hoje()
        candidatos = [c for c in planilha.contatos if c["status"] == PENDENTE]
        novos = sorted(candidatos, key=score_lead, reverse=True)  # celulares + maior potencial 1º
        retomar = [c for c in planilha.contatos
                   if precisa_followup(c, getattr(args, "dias_followup", 2),
                                       getattr(args, "max_followups", 1))]
        fila = [(c, "followup") for c in retomar] + [(c, "novo") for c in novos]
        contatadas = clinicas_ja_contatadas(planilha.contatos)
        _registrar(f"[AUTO] Início: cota {max(cota, 0)}, {len(retomar)} follow-up(s), "
                   f"{len(novos)} pendente(s) "
                   f"({sum(eh_celular(c['telefone']) for c in novos)} celular(es)).")

        intervalo_alvo = random.randint(args.intervalo_min, args.intervalo_max)
        falhas_seguidas = 0
        for contato, origem in fila:
            if len(enviadas) >= cota:
                break
            if not dentro_da_janela(args.hora_inicio, args.hora_fim):
                _registrar("[AUTO] Janela de horário encerrou — parando.")
                break
            if enviado_hoje(contato["telefone"]):
                continue
            nome = contato["clinica"][:50]
            if origem == "novo" and nome_bloqueado(contato):
                contato["status"] = STATUS_BLOQUEADO
                planilha.salvar()
                _registrar(f"[BLOQUEADO] {nome}: nome na lista de bloqueio.")
                continue
            if origem == "novo" and chave_clinica(contato) in contatadas:
                contato["status"] = STATUS_MESMA_CLINICA
                planilha.salvar()
                _registrar(f"[MESMA CLÍNICA] {nome}: já contatada por outro número.")
                continue

            if origem == "followup":
                variante, tipo_msg, tipo_hist = contato.get("variante", ""), "followup", FOLLOWUP
                mensagem = montar_texto_followup(contato, mensagens)
            else:
                variante = contato.get("variante") or variante_ab(contato["telefone"])
                tipo_msg, tipo_hist = variante, INICIAL
                mensagem = (montar_abertura(contato, mensagens) if variante == "curta"
                            else montar_texto(contato, mensagens))
            problema = validar_mensagem(mensagem, contato, tipo_msg)
            if problema:
                puladas.append(nome)
                _registrar(f"[PULADO] {nome}: {problema}")
                continue

            restante = segundos_de_espera(ultimo_envio(), intervalo_alvo, datetime.now())
            if restante:
                time.sleep(restante)

            resultado = _envio_automatico(sessao, contato, mensagem)
            if resultado in ("ok", "nao_conferido"):
                atualizar_historico(contato, mensagem, "automatico", tipo_hist, variante)
                marcar_enviado(contato, planilha, tipo_hist, salvar=(origem != "novo"))
                if origem == "novo":
                    contato["variante"] = variante
                    contato["etapa"] = "abertura" if variante == "curta" else "apresentacao"
                    contato["versao_textos"] = str(mensagens.get("versao", ""))
                    planilha.salvar()
                contatadas.add(chave_clinica(contato))
                rotulo = nome_para_mensagem(contato["clinica"], contato.get("bairro", ""),
                                            contato.get("cidade") or CIDADE_PADRAO)
                enviadas.append(f"{rotulo} (follow-up)" if origem == "followup" else rotulo)
                falhas_seguidas = 0
                intervalo_alvo = random.randint(args.intervalo_min, args.intervalo_max)
                if resultado == "ok":
                    _registrar(f"[ENVIADO] {nome} (+{contato['telefone']}) "
                               f"[{tipo_msg}]")
                    continue
                parada = (f"mensagem para {nome} não foi conferida na conversa — "
                          "verifique manualmente")
                _registrar(f"[PARADO] {parada}")
                break

            erro = resultado
            if erro == SessaoWeb.SEM_WHATSAPP:
                contato["status"] = "sem_whatsapp"
                contato["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
                planilha.salvar()
                sem_whatsapp.append(nome)
                _registrar(f"[SEM WHATSAPP] {nome} — removido da fila.")
                continue
            if erro == SessaoWeb.DESLOGADO:
                parada = "WhatsApp Web desconectado — rode: python prospeccao_whatsapp.py --conectar"
                _registrar(f"[PARADO] {parada}")
                break
            falhas_seguidas += 1
            _registrar(f"[FALHA] {nome}: {erro} ({falhas_seguidas} seguida(s))")
            if falhas_seguidas >= 2:
                parada = "2 falhas técnicas seguidas — interrompido por segurança"
                _registrar(f"[PARADO] {parada}")
                break

        # Leitura (sem digitar nada): entregas de hoje + respostas
        if parada is None or "desconectado" not in parada:
            try:
                verificacao = verificar_conversas(planilha, sessao)
                _registrar(f"[VERIFICAÇÃO] conferidas {verificacao['conferidas']}, "
                           f"problemas {len(verificacao['problemas'])}, "
                           f"respostas {len(verificacao['respostas'])}, "
                           f"opt-out {len(verificacao['optouts'])}")
                for p in verificacao["problemas"]:
                    _registrar(f"[ATENÇÃO] {p}")
                if verificacao["erro"]:
                    parada = parada or verificacao["erro"]
                else:
                    acoes_feitas = executar_acoes(verificacao, planilha, sessao,
                                                  mensagens, args)
            except Exception as exc:  # noqa: BLE001
                _registrar(f"[VERIFICAÇÃO] falhou: {exc}")

        pendentes = sum(1 for c in planilha.contatos if c["status"] == PENDENTE)
        resumo = montar_resumo(enviadas, sem_whatsapp, puladas, verificacao,
                               pendentes, args.limite_dia, parada,
                               acoes=acoes_feitas, ab=estatisticas_ab(planilha.contatos,
                                                 str(mensagens.get("versao", ""))))
        resumo_ok = False
        destino = getattr(args, "resumo_para", None)
        if destino and (parada is None or "desconectado" not in parada):
            resumo_ok = sessao.enviar(_resumo_contato(destino), resumo,
                                      permitir_reenvio=True, confirmar=False)
            _registrar(f"[RESUMO] enviado para o seu WhatsApp: {resumo_ok}")

        if verificacao and verificacao.get("respostas_texto"):
            notificar_windows("Prospecção: clínica respondeu",
                              ", ".join(n for n, _ in verificacao["respostas_texto"]))
        problemas = bool(parada) or bool(verificacao and verificacao["problemas"])
        if problemas or (destino and not resumo_ok):
            detalhe = (parada or "; ".join((verificacao or {}).get("problemas", []))
                       or "o resumo no seu WhatsApp não foi enviado")
            notificar_windows("Prospecção: atenção", detalhe)
        elif pendentes < 2 * args.limite_dia:
            notificar_windows("Prospecção: fila acabando",
                              f"Restam {pendentes} pendentes. Rode o minerador.")
    finally:
        sessao.fechar()
        trava.__exit__(None, None, None)
        _registrar(f"[AUTO] Fim: {len(enviadas)} enviada(s) nesta execução; "
                   f"total do dia {envios_hoje()}/{args.limite_dia}.")
    return len(enviadas)


# ---------------------------------------------------------------------------
# 7. CLI
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
    parser.add_argument("--dias-followup", type=int, default=2,
                        help="Dias ÚTEIS desde o último envio para o follow-up (padrão: 2)")
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
    parser.add_argument("--segmento", choices=list(SEGMENTOS), default=None,
                        help="Filtra a fila pelo segmento do CSV: odonto | medico "
                             "(contatos sem segmento ficam de fora)")
    parser.add_argument("--automatico", action="store_true",
                        help="Envio SEM operador (para o agendador): pendentes com "
                             "celular até --limite-dia, com todas as checagens")
    parser.add_argument("--atraso-max", type=int, default=0,
                        help="--automatico: começa após um atraso aleatório de 0 a N "
                             "minutos (horário de início variado)")
    parser.add_argument("--resumo-para", default=None,
                        help="--automatico: telefone que recebe o resumo diário "
                             "no WhatsApp (o seu próprio número)")
    parser.add_argument("--verificar", action="store_true",
                        help="Somente leitura: confere as entregas de hoje e lê as "
                             "respostas das clínicas (atualiza o CSV)")
    parser.add_argument("--acompanhar", action="store_true",
                        help="Rodada curta (agendada ao longo do dia): lê respostas, avisa "
                             "na hora, envia a apresentação da variante curta e responde "
                             "menus de robô")
    parser.add_argument("--conectar", action="store_true",
                        help="Abre o WhatsApp Web do script para escanear o QR Code "
                             "(janela visível)")
    parser.add_argument("--healthcheck", action="store_true",
                        help="Verifica login e seletores do WhatsApp Web sem enviar nada")
    parser.add_argument("--mostrar-janela", action="store_true",
                        help="Mostra a janela do Chrome (padrão: abre fora da tela, "
                             "sem saltar na frente do que você está fazendo)")
    parser.add_argument("--reset", action="store_true",
                        help="Reabre como 'pendente' quem recebeu e não respondeu "
                             "(nunca reabre opt-out nem quem respondeu)")
    return parser


def main() -> None:
    parser = _criar_parser()
    args = parser.parse_args()

    global JANELA_VISIVEL
    JANELA_VISIVEL = bool(args.mostrar_janela or args.conectar)

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

    if args.automatico:
        rodar_automatico(planilha, args, mensagens)
        return
    if args.acompanhar:
        rodar_acompanhamento(planilha, args, mensagens)
        return
    if args.conectar:
        conectar_whatsapp()
        return
    if args.healthcheck:
        sessao = SessaoWeb()
        try:
            res = sessao.healthcheck()
        except Exception as exc:  # noqa: BLE001
            print(f"[HEALTHCHECK] Erro: {exc}")
            sys.exit(1)
        finally:
            sessao.fechar()
        print(f"[HEALTHCHECK] Status: {res['status']}")
        for k, qtd in res["seletores_ativos"].items():
            print(f"  - {k}: {qtd} elemento(s)")
        return
    if args.verificar:
        sessao = SessaoWeb()
        try:
            rel = verificar_conversas(planilha, sessao)
        except EnvioEmAndamento as exc:
            print(f"[BLOQUEADO] {exc}")
            return
        finally:
            sessao.fechar()
        print(f"\n[VERIFICAÇÃO] Entregas de hoje conferidas: {rel['conferidas']}"
              + (f" (+{rel['nao_verificaveis']} ainda sem histórico no navegador — "
                 "conferidas no momento do envio)" if rel["nao_verificaveis"] else ""))
        for problema in rel["problemas"]:
            print(f"  ATENÇÃO: {problema}")
        for rotulo, chave in (("Responderam (pessoa)", "respostas"),
                              ("Só resposta automática", "automaticas"),
                              ("Opt-out (não contatar)", "optouts")):
            if rel[chave]:
                print(f"  {rotulo}: {', '.join(rel[chave])}")
        if rel["erro"]:
            print(f"  ERRO: {rel['erro']}")
        return

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

    if args.segmento:
        contatos = [c for c in contatos
                    if c.get("segmento", "").lower() == args.segmento]
        if not contatos:
            print(f"[ERRO] Nenhum contato com segmento '{args.segmento}'. "
                  "Classifique o CSV (coluna segmento) ou use outro filtro.")
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
