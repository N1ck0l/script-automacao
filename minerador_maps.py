#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minerador de empresas no Google Maps (Playwright) -> prospeccao.csv

Extrai dados B2B de estabelecimentos (clínicas, médicos etc.) a partir de um
termo de busca no Google Maps e integra os resultados ao MESMO CSV usado pelo
script de envio (`prospeccao_whatsapp.py`), sem sobrescrever registros
existentes — a duplicidade é verificada pelo número de telefone.

CARACTERÍSTICAS DE ROBUSTEZ / EVASÃO BÁSICA:
  - Seletores resilientes: atributos semânticos (`data-item-id`, `role`,
    `aria-label`, `href`) em vez de classes geradas dinamicamente pelo Google.
  - Rolagem suave e incremental na barra lateral (`div[role="feed"]`) para
    forçar o carregamento dinâmico dos resultados.
  - Pausas aleatórias curtas (1–3s, configuráveis) entre navegações.
  - Extração campo a campo com fallbacks: se um elemento não aparece, tenta
    outro seletor antes de desistir (não derruba a mineração inteira).
  - Só celulares por padrão (fixos raramente têm WhatsApp; --incluir-fixos).

USO:
  python minerador_maps.py --termo "clinica odontologica em Aracaju SE" --max 40
  python minerador_maps.py --termo "clinica medica em Aracaju SE" --max 30 --visivel
  python minerador_maps.py --termo "dentista em Aracaju" --dry-run   # só mostra

INSTALAÇÃO (uma vez):
  pip install -r requirements.txt
  playwright install chromium
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import prospeccao_whatsapp as pw  # esquema de colunas + higienização de telefone

BASE_DIR = Path(__file__).resolve().parent
CIDADE_PADRAO = "Aracaju"

# Pausa padrão entre navegações (anti-bloqueio)
DELAY_MIN = 1.0
DELAY_MAX = 3.0

# ---------------------------------------------------------------------------
# 1. PARÂMETROS E UTILITÁRIOS PUROS (testáveis sem navegador)
# ---------------------------------------------------------------------------


def montar_url_busca(termo: str) -> str:
    """URL de busca do Maps com o termo codificado e interface em pt-BR."""
    return f"https://www.google.com/maps/search/{quote(termo.strip())}?hl=pt-BR"


def pausa(minima: float = DELAY_MIN, maxima: float = DELAY_MAX) -> None:
    """Pausa aleatória curta entre ações (1–3s por padrão)."""
    time.sleep(random.uniform(minima, maxima))


def _parte_util(parte: str, cidade: str) -> str | None:
    """Filtra partes de endereço que não podem ser bairro (cidade, UF, CEP...)."""
    p = parte.strip()
    if not p:
        return None
    if p.lower() in {cidade.lower(), "brasil", "br"}:
        return None
    if re.search(r"\d", p):          # CEP ou numeração
        return None
    if re.fullmatch(r"[A-Z]{2}(,.*)?", p):  # UF ("SE", "SE, 49010-000")
        return None
    return p


def extrair_bairro(endereco: str, cidade: str = CIDADE_PADRAO) -> str:
    """Extrai o bairro da linha de endereço do Google Maps.

    Formatos cobertos:
      A) "Av. Marechal Rondon, 1470 - Centro - Aracaju - SE, 49010-000 - Brasil"
      B) "Rua Santo Amaro, 35 - São José - Aracaju - SE, 49010-000 - Brasil"
      C) "Ponta d'Areia, Aracaju - SE, 49030-000 - Brasil" (sem travessão)
      D) "Rua das Flores, 123 - Bairro Centro, Aracaju - SE, 49000-000 - Brasil"
    """
    if not endereco:
        return ""

    # Estratégia 1: separar por " - " e pegar a primeira parte útil entre a rua e a UF
    partes = [x.strip() for x in endereco.split(" - ") if x.strip()]
    if len(partes) >= 3:
        for parte in partes[1:-1]:
            util = _parte_util(parte, cidade)
            if util:
                # Formato D: "Bairro Centro, Aracaju" -> só antes da vírgula
                return util.split(",")[0].strip()

    # Estratégia 2: ", <cidade>" -> tudo antes da vírgula
    m = re.search(r"([^,-]+),\s*" + re.escape(cidade), endereco)
    if m:
        candidato = m.group(1).strip()
        if candidato and not re.search(r"\d", candidato):
            return candidato

    return ""


def extrair_avaliacoes(texto: str) -> str:
    """Extrai nota + volume de avaliações de um bloco de texto do Maps.

    Saída normalizada: "4.8 estrelas (1234 avaliações)" | "4.8 estrelas" |
    "1234 avaliações" | "" (quando não há dados).
    """
    if not texto:
        return ""

    nota = qtd = None

    # Padrão "4,8 (1.234)" — nota seguida do total entre parênteses
    m = re.search(r"(\d+[,.]\d)[^\d]{0,12}\(([\d.]+)\)", texto)
    if m:
        nota, qtd = m.group(1), m.group(2)

    # Total de avaliações: "1.234 avaliações"
    if not qtd:
        m = re.search(r"([\d.]+)\s*avaliaç", texto, re.IGNORECASE)
        if m:
            qtd = m.group(1)

    # Nota explícita: "4,8 estrelas" / "4,8 de 5"
    if not nota:
        m = re.search(r"(\d+[,.]\d)\s*(?:estrelas?|de\s*5)", texto, re.IGNORECASE)
        if m:
            nota = m.group(1)
        else:
            # Último recurso: linha isolada só com a nota (ex.: "4,8" em uma linha)
            m = re.search(r"(?:^|\n)(\d[,.]\d)(?=\n|$)", texto, re.MULTILINE)
            if m:
                nota = m.group(1)

    nota_norm = nota.replace(",", ".") if nota else ""
    qtd_norm = re.sub(r"\D", "", qtd) if qtd else ""

    if nota_norm and qtd_norm:
        return f"{nota_norm} estrelas ({qtd_norm} avaliações)"
    if nota_norm:
        return f"{nota_norm} estrelas"
    if qtd_norm:
        return f"{qtd_norm} avaliações"
    return ""


def preparar_novo_registro(dados: dict, cidade: str = CIDADE_PADRAO,
                           segmento: str = "") -> dict | None:
    """Converte um resultado bruto no registro do CSV; None se inválido.

    Telefones fixos são aceitos aqui; o filtro de celular fica em `minerar`.

    Regras: telefone obrigatório e válido (DDI 55 + DDD + número), nome
    obrigatório. `status` nasce como "pendente" conforme o fluxo de envio.
    `segmento` (odonto/medico) vem do termo de busca; sem termo, infere do
    nome da clínica.
    """
    clinica = (dados.get("clinica") or "").strip()
    telefone = pw.normalizar_telefone(dados.get("telefone") or "")
    if not clinica:
        return None
    if not pw.validar_telefone(telefone):
        return None
    registro = {c: "" for c in pw.COLUNAS}  # mesmo esquema/ordem do envio
    registro.update(
        clinica=clinica,
        bairro=(dados.get("bairro") or "").strip(),
        cidade=cidade,
        telefone=telefone,
        site=(dados.get("site") or "").strip(),
        avaliacoes=(dados.get("avaliacoes") or "").strip(),
        segmento=(segmento or pw.inferir_segmento(clinica)).lower(),
        status=pw.PENDENTE,
    )
    return registro


def mesclar_registros(existentes: list[dict],
                      novos: list[dict]) -> tuple[list[dict], int]:
    """Deduplica por telefone. Registros já existentes sempre prevalecem.

    Os telefones dos novos são higienizados antes da comparação, para que
    "(79) 99963-1871" e "5579999631871" sejam tratados como o mesmo contato.
    """
    vistos = {c["telefone"] for c in existentes}
    inseridos: list[dict] = []
    pulados = 0
    for dados in novos:
        registro = dict(dados)
        registro["telefone"] = pw.normalizar_telefone(
            registro.get("telefone") or "")
        if not pw.validar_telefone(registro["telefone"]):
            continue  # inválido: não entra no CSV nem conta como duplicado
        if registro["telefone"] in vistos:
            pulados += 1
            continue
        vistos.add(registro["telefone"])
        inseridos.append(registro)
    return inseridos, pulados


def integrar_csv(novos: list[dict], caminho: Path) -> tuple[int, int]:
    """Anexa resultados novos ao CSV existente sem sobrescrever duplicados.

    Linhas já existentes (inclusive inválidas ou duplicadas, que ficam fora da
    fila) são preservadas; a gravação é atômica. Retorna (inseridos, pulados).
    """
    caminho = Path(caminho)
    planilha = (pw.carregar_planilha(caminho) if caminho.exists()
                else pw.Planilha(caminho))

    inseridos, pulados = mesclar_registros(planilha.contatos, novos)
    if not inseridos:
        return 0, pulados

    for registro in inseridos:
        planilha.adicionar(registro)
    planilha.salvar()
    return len(inseridos), pulados


# ---------------------------------------------------------------------------
# 2. EXTRAÇÃO NO NAVEGADOR (Playwright) — seletores semânticos com fallbacks
# ---------------------------------------------------------------------------


def _texto(page, seletor: str, timeout: int = 3000) -> str:
    """Texto de um elemento; '' se não existir (nunca derruba a extração)."""
    try:
        loc = page.locator(seletor).first
        if loc.count():
            return loc.inner_text(timeout=timeout).strip()
    except Exception:  # noqa: BLE001 - elemento pode desaparecer a qualquer momento
        pass
    return ""


def _atributo(page, seletor: str, atributo: str, timeout: int = 3000) -> str:
    try:
        loc = page.locator(seletor).first
        if loc.count():
            return (loc.get_attribute(atributo, timeout=timeout) or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _extrair_telefone(page) -> str:
    """Telefone do place: data-item-id > href tel: > padrão no texto."""
    # 1) data-item-id semântico: "phone:tel:+55 79 99963-1871"
    valor = _atributo(page, '[data-item-id^="phone:tel:"]', "data-item-id")
    if "phone:tel:" in valor:
        return valor.split("phone:tel:", 1)[1]

    # 2) link <a href="tel:...">
    href = _atributo(page, 'a[href^="tel:"]', "href")
    if href:
        return href.replace("tel:", "").strip()

    # 3) aria-label com "Telefone"
    for seletor in ('button[aria-label*="Telefone" i]',
                    'a[aria-label*="Telefone" i]'):
        rotulo = _atributo(page, seletor, "aria-label")
        m = re.search(r"\d[\d\s().+-]{8,}\d", rotulo)
        if m:
            return m.group(0)

    # 4) padrão de telefone no texto do principal
    try:
        texto = page.locator('[role="main"]').first.inner_text(timeout=2000)
    except Exception:  # noqa: BLE001
        texto = ""
    m = re.search(r"\(?\d{2}\)?\s?\d{4,5}-?\d{4}", texto)
    return m.group(0) if m else ""


def _extrair_avaliacoes_da_pagina(page) -> str:
    """Reúne aria-labels de estrelas + texto principal e parseia a nota."""
    partes: list[str] = []
    try:
        rotulos = page.locator(
            '[aria-label*="estrela" i], [aria-label*="estrelas" i], '
            '[aria-label*="avalia" i]'
        )
        partes.extend(rotulos.all_inner_texts(timeout=2000))
        for i in range(min(rotulos.count(), 10)):
            rotulo = rotulos.nth(i).get_attribute("aria-label")
            if rotulo:
                partes.append(rotulo)
    except Exception:  # noqa: BLE001
        pass
    try:
        partes.append(page.locator('[role="main"]').first.inner_text(timeout=3000))
    except Exception:  # noqa: BLE001
        pass
    return extrair_avaliacoes("\n".join(partes))


def minerar_local(page, url: str, cidade: str = CIDADE_PADRAO) -> dict | None:
    """Navega até o card (place) e extrai os campos com fallbacks."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_selector("h1", timeout=20000)
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERRO] Falha ao abrir {url}: {exc}")
        return None

    pausa(1.0, 2.0)  # deixa o card terminar de renderizar

    nome = _texto(page, "h1") or _texto(page, '[data-item-id="place-name"]')
    endereco = _texto(page, '[data-item-id="address"]')
    site = _atributo(page, 'a[data-item-id="authority"]', "href")
    telefone = _extrair_telefone(page)
    avaliacoes = _extrair_avaliacoes_da_pagina(page)

    if not nome:
        return None

    return {
        "clinica": nome,
        "bairro": extrair_bairro(endereco, cidade),
        "cidade": cidade,
        "telefone": telefone,
        "site": site,
        "avaliacoes": avaliacoes,
    }


# ---------------------------------------------------------------------------
# 3. COLETA DA LISTA LATERAL — rolagem suave + pausas aleatórias
# ---------------------------------------------------------------------------


def coletar_links(page, max_alvos: int,
                  tentativas_sem_novo: int = 8) -> list[str]:
    """Coleta links dos cards na barra lateral com rolagem suave incremental.

    A barra lateral do Maps é um `div[role="feed"]` virtualizado: sem rolagem,
    só os ~12 primeiros resultados existem no DOM. Rolamos até atingir
    `max_alvos` ou parar de aparecerem resultados novos.
    """
    # Caso especial: resultado único (a URL já é um place, sem feed lateral)
    if page.locator('div[role="feed"]').count() == 0:
        if "/maps/place/" in page.url:
            return [page.url]
        return []

    vistos: dict[str, None] = {}
    sem_novo = 0
    while len(vistos) < max_alvos and sem_novo < tentativas_sem_novo:
        hrefs = page.locator('a[href*="/maps/place/"]').evaluate_all(
            "els => els.map(e => e.href)"
        )
        antes = len(vistos)
        for href in hrefs:
            if href and href not in vistos:
                vistos[href] = None
        if len(vistos) >= max_alvos:
            break

        # Rolagem suave e incremental no feed lateral
        try:
            page.locator('div[role="feed"]').first.evaluate(
                "el => el.scrollBy({top: Math.round(el.clientHeight * 0.9), "
                "behavior: 'smooth'})"
            )
        except Exception:  # noqa: BLE001
            break
        pausa(0.6, 1.4)

        sem_novo = 0 if len(vistos) > antes else sem_novo + 1

    return list(vistos)[:max_alvos]


# ---------------------------------------------------------------------------
# 4. ORQUESTRAÇÃO
# ---------------------------------------------------------------------------


def minerar(termo: str, max_alvos: int = 40, cidade: str = CIDADE_PADRAO,
            visivel: bool = False, incluir_fixos: bool = False,
            delay_min: float = DELAY_MIN,
            delay_max: float = DELAY_MAX) -> list[dict]:
    """Abre o Maps, coleta os links e minera cada place. Retorna registros
    prontos para o CSV (ou lista vazia em falha total)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERRO] Playwright não instalado. Rode:")
        print("       pip install -r requirements.txt")
        print("       playwright install chromium")
        sys.exit(1)

    print(f"[BUSCA] {termo} | alvo: até {max_alvos} estabelecimentos")
    segmento = pw.inferir_segmento(termo)

    with sync_playwright() as p:
        navegador = p.chromium.launch(
            headless=not visivel,
            args=["--disable-blink-features=AutomationControlled"],
        )
        contexto = navegador.new_context(
            locale="pt-BR",
            viewport={"width": 1366, "height": 900},
        )
        page = contexto.new_page()

        try:
            page.goto(montar_url_busca(termo), wait_until="domcontentloaded",
                      timeout=45000)
            try:
                page.wait_for_selector(
                    'div[role="feed"], a[href*="/maps/place/"]', timeout=25000)
            except Exception:  # noqa: BLE001
                print("[AVISO] Lista lateral não apareceu (bloqueio ou CAPTCHA).")
                return []

            links = coletar_links(page, max_alvos)
            print(f"[LISTA] {len(links)} card(s) encontrado(s) na barra lateral.\n")

            registros: list[dict] = []
            for i, url in enumerate(links, start=1):
                print(f"[{i}/{len(links)}] {url.split('/maps/place/')[-1][:70]}")
                pausa(delay_min, delay_max)  # pausa aleatória 1–3s entre cliques

                dados = minerar_local(page, url, cidade)
                if dados is None:
                    print("         ↳ pulado: não foi possível extrair o card")
                    continue

                registro = preparar_novo_registro(dados, cidade,
                                                  segmento=segmento)
                if registro is None:
                    print(f"         ↳ pulado: telefone ausente/inválido "
                          f"({dados.get('telefone') or 'sem número'})")
                    continue

                if not incluir_fixos and not pw.eh_celular(registro["telefone"]):
                    print(f"         ↳ pulado: telefone fixo +{registro['telefone']} "
                          "(raramente tem WhatsApp; use --incluir-fixos)")
                    continue

                registros.append(registro)
                print(f"         ↳ {registro['clinica']} | {registro['bairro'] or '?'} "
                      f"| +{registro['telefone']} | {registro['avaliacoes'] or 'sem nota'}")

            return registros
        finally:
            navegador.close()


# ---------------------------------------------------------------------------
# 5. RODÍZIO SEMANAL DE TERMOS (agendado)
# ---------------------------------------------------------------------------

ARQUIVO_TERMOS = BASE_DIR / "termos_mineracao.txt"
ARQUIVO_RODIZIO = BASE_DIR / ".rodizio_mineracao"


def ler_termos(caminho: Path = ARQUIVO_TERMOS) -> list[str]:
    """Termos do arquivo (um por linha; '#' = comentário)."""
    if not Path(caminho).exists():
        return []
    return [linha.strip() for linha in Path(caminho).read_text(encoding="utf-8").splitlines()
            if linha.strip() and not linha.strip().startswith("#")]


def proximo_termo(termos: list[str], estado: Path = ARQUIVO_RODIZIO) -> tuple[str, int]:
    """Próximo termo do rodízio e seu índice (volta ao início no fim da lista)."""
    try:
        indice = int(Path(estado).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        indice = 0
    indice %= len(termos)
    return termos[indice], indice


def avancar_rodizio(indice: int, estado: Path = ARQUIVO_RODIZIO) -> None:
    Path(estado).write_text(str(indice + 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minera empresas no Google Maps e integra ao prospeccao.csv "
                    "(deduplicado por telefone)."
    )
    parser.add_argument("--termo", default="clinica odontologica em Aracaju SE",
                        help='Termo de busca (ex.: "clinica medica em Aracaju SE")')
    parser.add_argument("--max", type=int, default=40,
                        help="Máximo de estabelecimentos a coletar (padrão: 40)")
    parser.add_argument("--cidade", default=CIDADE_PADRAO,
                        help=f"Cidade dos resultados (padrão: {CIDADE_PADRAO})")
    parser.add_argument("--saida", default=str(BASE_DIR / "prospeccao.csv"),
                        help="CSV de saída/integração (padrão: prospeccao.csv)")
    parser.add_argument("--delay-min", type=float, default=DELAY_MIN,
                        help="Pausa mínima entre navegações em segundos (padrão: 1)")
    parser.add_argument("--delay-max", type=float, default=DELAY_MAX,
                        help="Pausa máxima entre navegações em segundos (padrão: 3)")
    parser.add_argument("--visivel", action="store_true",
                        help="Abre o navegador visível (headless é o padrão)")
    parser.add_argument("--incluir-fixos", action="store_true",
                        help="Inclui telefones fixos (por padrão só celulares, "
                             "que quase sempre têm WhatsApp)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostra o resultado sem gravar no CSV")
    parser.add_argument("--rodizio", action="store_true",
                        help="Usa o próximo termo de termos_mineracao.txt (agendamento "
                             "semanal); inclui fixos — o envio detecta quem tem WhatsApp")
    args = parser.parse_args()

    if args.delay_min > args.delay_max:
        parser.error("--delay-min não pode ser maior que --delay-max")

    indice_rodizio = None
    if args.rodizio:
        termos = ler_termos()
        if not termos:
            print(f"[ERRO] Nenhum termo em {ARQUIVO_TERMOS.name}.")
            sys.exit(1)
        args.termo, indice_rodizio = proximo_termo(termos)
        args.incluir_fixos = True
        print(f"[RODÍZIO] Termo {indice_rodizio + 1}/{len(termos)}: {args.termo}")

    registros = minerar(args.termo, max_alvos=args.max, cidade=args.cidade,
                        visivel=args.visivel, incluir_fixos=args.incluir_fixos,
                        delay_min=args.delay_min, delay_max=args.delay_max)

    if indice_rodizio is not None and not args.dry_run:
        avancar_rodizio(indice_rodizio)  # avança mesmo sem resultado (não trava num termo)

    if not registros:
        print("\n[NADA] Nenhum registro coletado.")
        return

    if args.dry_run:
        print(f"\n[DRY-RUN] {len(registros)} registro(s) coletado(s) "
              "(nada gravado):")
        for r in registros:
            print(f"  {r['clinica'][:34]:<34} | {r['bairro'][:16]:<16} | "
                  f"+{r['telefone']} | {r['site'][:30]}")
        return

    caminho = Path(args.saida)
    try:
        # Mesma trava do envio: nunca grava o CSV enquanto um envio o usa.
        with pw.TravaEnvio():
            inseridos, pulados = integrar_csv(registros, caminho)
    except pw.EnvioEmAndamento as exc:
        print(f"[BLOQUEADO] {exc} Rode o minerador de novo depois.")
        sys.exit(1)
    print(f"\n[CSV] {inseridos} novo(s) salvo(s) em {caminho.name}; "
          f"{pulados} duplicado(s) já existente(s) ignorado(s) "
          "(chave: telefone).")
    print(f"[CSV] Colunas: {', '.join(pw.COLUNAS)}")
    print("\nPróximo passo: python prospeccao_whatsapp.py --listar")


if __name__ == "__main__":
    main()
