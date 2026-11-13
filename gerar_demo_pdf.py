# Gera demonstracao.pdf (1 página) a partir da simulação em demonstracao.html.
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

BASE = Path(__file__).resolve().parent
HTML = BASE / "demonstracao.html"
SAIDA = BASE / "demonstracao.pdf"

AZUL = colors.HexColor("#123a6b")
AZUL_ESC = colors.HexColor("#0c2749")
AZUL_CLARO = colors.HexColor("#eaf1fa")
VERDE = colors.HexColor("#128c7e")
VERDE_CLARO = colors.HexColor("#e9f6ef")
VERMELHO_CLARO = colors.HexColor("#fbeeed")
VERMELHO = colors.HexColor("#b4453c")
CINZA = colors.HexColor("#5b6470")
BORDA = colors.HexColor("#d9dee6")
TEXTO = colors.HexColor("#1d2733")

W, H = A4
M = 14 * mm


def capturar_celular() -> Path:
    destino = Path(tempfile.gettempdir()) / "demo_celular.png"
    with sync_playwright() as p:
        navegador = p.chromium.launch()
        pagina = navegador.new_page(viewport={"width": 1280, "height": 900})
        pagina.goto(HTML.resolve().as_uri())
        pagina.wait_for_timeout(26000)
        pagina.evaluate("document.getElementById('conversa').scrollTop = 0")
        pagina.wait_for_timeout(300)
        pagina.locator(".celular").screenshot(path=str(destino))
        navegador.close()
    return destino


def par(txt: str, size: float = 8.4, cor=TEXTO, bold: bool = False,
        leading: float | None = None, align: int = 0) -> Paragraph:
    estilo = ParagraphStyle(
        "p", fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size, textColor=cor, leading=leading or size * 1.35,
        alignment=align, spaceAfter=0, spaceBefore=0,
    )
    return Paragraph(txt, estilo)


def main() -> None:
    img = capturar_celular()

    c = canvas.Canvas(str(SAIDA), pagesize=A4)
    c.setTitle("Automações para Clínicas — Demonstração")

    # ---------------- cabeçalho ----------------
    cab_h = 26 * mm
    c.setFillColor(AZUL_ESC)
    c.rect(0, H - cab_h, W, cab_h, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(W / 2, H - 11 * mm, "Automações para Clínicas")
    c.setFont("Helvetica", 9)
    c.drawCentredString(
        W / 2, H - 16.5 * mm,
        "Demonstração — triagem de pacientes pelo WhatsApp e emissão de NF-e")
    c.setFillColor(colors.HexColor("#9fb4d4"))
    c.setFont("Helvetica", 7.6)
    c.drawCentredString(
        W / 2, H - 21.5 * mm,
        "Demonstração ilustrativa · funcionalidade já implantada em clínicas da região")

    y = H - cab_h - 7 * mm

    # ---------------- etapa 1: triagem ----------------
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(M, y, "1. TRIAGEM PELO WHATSAPP")
    y -= 4.6 * mm
    p_intro = par(
        "O paciente conversa normalmente; ao final, a recepção recebe a queixa "
        "já resumida e organizada — sem repetir as mesmas perguntas.")
    _, h_intro = p_intro.wrapOn(c, W - 2 * M, H)
    p_intro.drawOn(c, M, y - h_intro)
    y -= h_intro + 4 * mm

    # coluna esquerda: print do celular
    img_w = 52 * mm
    from reportlab.lib.utils import ImageReader
    ir = ImageReader(str(img))
    iw, ih = ir.getSize()
    img_h = img_w * ih / iw
    c.drawImage(ir, M, y - img_h, width=img_w, height=img_h, mask="auto")

    # coluna direita: cartão de resumo
    x_dir = M + img_w + 6 * mm
    larg_dir = W - M - x_dir

    resumo = [
        [par("TRIAGEM ORGANIZADA — o que a recepção recebe", 8.6, VERDE, True)],
        [par("<b>Paciente:</b> Mariana Costa", 8.4)],
        [par("<b>Motivo:</b> dor aguda — molar inferior direito", 8.4)],
        [par("<b>Histórico:</b> há 3 dias; analgésico sem alívio", 8.4)],
        [par("<b>Convênio:</b> particular", 8.4)],
        [par("<b>Preferência:</b> tarde — quinta-feira", 8.4)],
        [par("<b>Próxima ação:</b> recepção confirma o encaixe e o paciente "
             "chega com os dados já registrados.", 8.4)],
    ]
    t_resumo = Table(resumo, colWidths=[larg_dir])
    t_resumo.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), VERDE_CLARO),
        ("LINEBEFORE", (0, 0), (0, -1), 3, VERDE),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDA),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 3.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    _, h_res = t_resumo.wrapOn(c, larg_dir, H)
    ty = y - h_res
    t_resumo.drawOn(c, x_dir, ty)

    # antes / depois
    ty -= 4 * mm
    col_w = (larg_dir - 3 * mm) / 2
    antes = [
        par("HOJE", 8.2, VERMELHO, True),
        par("• mensagens espalhadas ao longo do dia", 7.9),
        par("• mesmas perguntas repetidas a cada contato", 7.9),
        par("• informação importante se perde na conversa", 7.9),
    ]
    depois = [
        par("COM A TRIAGEM", 8.2, VERDE, True),
        par("• conversa conduzida em um único contato", 7.9),
        par("• queixa e dados já resumidos", 7.9),
        par("• recepção recebe tudo organizado", 7.9),
    ]
    t_antes = Table([[a] for a in antes], colWidths=[col_w - 8])
    t_depois = Table([[d] for d in depois], colWidths=[col_w - 8])
    for t, fundo in ((t_antes, VERMELHO_CLARO), (t_depois, VERDE_CLARO)):
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), fundo),
            ("BOX", (0, 0), (-1, -1), 0.6, BORDA),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 2.6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
        ]))
    _, h_a = t_antes.wrapOn(c, col_w, H)
    _, h_d = t_depois.wrapOn(c, col_w, H)
    t_antes.drawOn(c, x_dir, ty - h_a)
    t_depois.drawOn(c, x_dir + col_w + 3 * mm, ty - h_d)
    y = min(ty - h_a, ty - h_d, y - img_h) - 11 * mm

    # ---------------- etapa 2: NF-e ----------------
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(M, y, "2. EMISSÃO DE NF-E")
    y -= 4.6 * mm
    p2 = par(
        "Os dados do atendimento já estão capturados: a nota deixa de ser manual "
        "e é concluída em poucos minutos, com validação em um clique.")
    _, h2 = p2.wrapOn(c, W - 2 * M, H)
    p2.drawOn(c, M, y - h2)
    y -= h2 + 5 * mm

    col2 = (W - 2 * M - 5 * mm) / 2
    manual = [
        par("PROCESSO MANUAL — COMO FUNCIONA HOJE", 8.2, CINZA, True),
        par("<b>1.</b> atendimento concluído, dados anotados ou repassados", 7.9),
        par("<b>2.</b> digitação no sistema, campo a campo", 7.9),
        par("<b>3.</b> conferência manual para evitar erro", 7.9),
        par("<b>4.</b> validação repetida para cada nota", 7.9),
        [par("vários minutos por nota, com risco de erro", 7.9, VERMELHO, True)],
    ]
    auto = [
        par("COM AUTOMAÇÃO — COMO PASSARIA A FUNCIONAR", 8.2, AZUL, True),
        par("<b>1.</b> dados já capturados na triagem", 7.9),
        par("<b>2.</b> nota gerada em poucos minutos", 7.9),
        par("<b>3.</b> equipe confere e valida em 1 clique", 7.9),
        par("", 7.9),
        [par("poucos minutos · validação em um clique", 7.9, AZUL, True)],
    ]
    t_manual = Table([[x] for x in manual], colWidths=[col2 - 8])
    t_auto = Table([[x] for x in auto], colWidths=[col2 - 8])
    t_manual.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f5f7")),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDA),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.8),
    ]))
    t_auto.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), AZUL_CLARO),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDA),
        ("LINEBEFORE", (0, 0), (0, -1), 3, AZUL),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.8),
    ]))
    _, h_m = t_manual.wrapOn(c, col2, H)
    t_auto.wrapOn(c, col2, H)
    t_manual.drawOn(c, M, y - h_m)
    t_auto.drawOn(c, M + col2 + 5 * mm, y - h_m)
    y -= h_m + 11 * mm

    # ---------------- como funciona ----------------
    c.setFillColor(AZUL)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(M, y, "3. COMO FUNCIONA A IMPLANTAÇÃO")
    y -= 5 * mm
    passos = [
        par("<b>1. Demonstração</b><br/>conversa de 10 minutos, online ou "
            "presencial, no horário conveniente", 8),
        par("<b>2. Implantação</b><br/>configuração da triagem e da NF-e "
            "conforme a rotina da clínica", 8),
        par("<b>3. Uso diário</b><br/>conversas organizadas na recepção e "
            "notas validadas em um clique", 8),
    ]
    t_passos = Table([[p for p in passos]],
                     colWidths=[(W - 2 * M - 6 * mm) / 3] * 3)
    t_passos.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.6, BORDA),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, BORDA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    _, h_p = t_passos.wrapOn(c, W - 2 * M, H)
    t_passos.drawOn(c, M, y - h_p)
    y -= h_p + 12 * mm

    # ---------------- CTA ----------------
    cta_h = 34 * mm
    c.setFillColor(AZUL_ESC)
    c.rect(M, y - cta_h, W - 2 * M, cta_h, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(W / 2, y - 9 * mm, "Quer ver funcionando na sua clínica?")
    c.setFont("Helvetica", 8.6)
    c.drawCentredString(
        W / 2, y - 15 * mm,
        "Posso apresentar o funcionamento em uma reunião de 10 minutos,")
    c.drawCentredString(
        W / 2, y - 19.2 * mm,
        "sem compromisso, com você ou com o responsável pela gestão.")
    c.setFillColor(colors.HexColor("#25d366"))
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(W / 2, y - 25.5 * mm,
                        "Agende pelo WhatsApp: (79) 98167-7954")
    c.setFillColor(colors.HexColor("#9fb4d4"))
    c.setFont("Helvetica", 7.6)
    c.drawCentredString(W / 2, y - 30.5 * mm, "wa.me/5579981677954")
    y -= cta_h

    # ---------------- rodapé ----------------
    c.setFillColor(CINZA)
    c.setFont("Helvetica", 7)
    c.drawCentredString(
        W / 2, 8 * mm,
        "Demonstração ilustrativa · Automações para clínicas · "
        "Triagem pelo WhatsApp e emissão de NF-e")

    c.showPage()
    c.save()
    print(f"PDF gerado: {SAIDA}")


if __name__ == "__main__":
    main()
