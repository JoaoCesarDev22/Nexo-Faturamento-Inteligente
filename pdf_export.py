"""
NEXO - Faturamento Inteligente | Exportação da Análise Executiva em PDF
======================================================================
Gera, com ReportLab (platypus), um PDF profissional da análise publicada:
cabeçalho da marca, indicadores, Curva ABC (Top 5), síntese executiva em
semáforos (5W2H) e a devolutiva do consultor.

Layout claro (fundo branco) — apropriado para impressão/anexo — com os acentos
de cor da identidade NEXO (roxo) e o mesmo código de cores da app para as
classes ABC e os semáforos. Mantém a regra: sem lucro/margem/CMV.

Função pública:
    gerar_pdf_analise(analise, semaforos, abc_top) -> bytes
"""

from io import BytesIO
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable,
)

# ---- Paleta NEXO ----
ROXO = HexColor("#7c3aed")
ROXO2 = HexColor("#a855f7")
TINTA = HexColor("#1f2330")
CINZA = HexColor("#6a7088")
LINHA = HexColor("#e6e8ef")
FUNDO_SUAVE = HexColor("#f5f6fa")

CLASSE_COR = {"A": HexColor("#22c55e"), "B": HexColor("#f59e0b"), "C": HexColor("#9aa0b7")}
SEMAFORO_COR = {
    "critico": HexColor("#ef4444"),
    "atencao": HexColor("#f59e0b"),
    "oportunidade": HexColor("#22c55e"),
}
SEMAFORO_FUNDO = {
    "critico": HexColor("#fdecec"),
    "atencao": HexColor("#fdf4e3"),
    "oportunidade": HexColor("#e9f9ef"),
}
SEMAFORO_ROTULO = {"critico": "CRÍTICO", "atencao": "ATENÇÃO", "oportunidade": "OPORTUNIDADE"}


def _brl(valor) -> str:
    """12345.6 -> '12.345,60' (sem o símbolo)."""
    try:
        v = float(valor or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"{v:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _num(valor) -> str:
    try:
        return f"{float(valor or 0):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


class _BarraTopo(Flowable):
    """Faixa roxa de cabeçalho com a marca NEXO."""
    def __init__(self, largura, titulo, subtitulo):
        super().__init__()
        self.largura = largura
        self.altura = 22 * mm
        self.titulo = titulo
        self.subtitulo = subtitulo

    def draw(self):
        c = self.canv
        c.setFillColor(ROXO)
        c.roundRect(0, 0, self.largura, self.altura, 6, stroke=0, fill=1)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(12 * mm, self.altura - 9 * mm, "NEXO")
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#ede9fb"))
        c.drawString(34 * mm, self.altura - 8.5 * mm, "Faturamento Inteligente")
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(white)
        c.drawRightString(self.largura - 12 * mm, self.altura - 8 * mm, self.titulo)
        c.setFont("Helvetica", 8.5)
        c.setFillColor(HexColor("#ede9fb"))
        c.drawRightString(self.largura - 12 * mm, self.altura - 13.5 * mm, self.subtitulo)


def _rodape(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINHA)
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(CINZA)
    canvas.drawString(
        18 * mm, 9 * mm,
        "Sinalização gerencial estimada (faturamento e Curva ABC). Não representa "
        "lucro, margem ou CMV.",
    )
    canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"Página {doc.page}")
    canvas.restoreState()


def _estilos():
    ss = getSampleStyleSheet()
    base = ss["Normal"]
    base.fontName = "Helvetica"
    base.textColor = TINTA
    estilos = {
        "h2": ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold",
                             fontSize=12.5, textColor=ROXO, spaceBefore=4, spaceAfter=7),
        "p": ParagraphStyle("p", parent=base, fontSize=9.5, leading=14, textColor=TINTA),
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=11, textColor=CINZA),
        "kpi_label": ParagraphStyle("kl", parent=base, fontSize=7.5, textColor=CINZA),
        "kpi_value": ParagraphStyle("kv", parent=base, fontName="Helvetica-Bold",
                                    fontSize=13, textColor=TINTA),
        "sc_title": ParagraphStyle("sct", parent=base, fontName="Helvetica-Bold",
                                   fontSize=10, textColor=TINTA),
        "sc_key": ParagraphStyle("sck", parent=base, fontName="Helvetica-Bold",
                                 fontSize=7, textColor=CINZA),
        "sc_val": ParagraphStyle("scv", parent=base, fontSize=8.5, leading=12, textColor=TINTA),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=12),
        "cell_b": ParagraphStyle("cellb", parent=base, fontName="Helvetica-Bold", fontSize=9),
    }
    return estilos


def _kpis_flowable(ind, largura, est):
    dados = [[
        Paragraph("FATURAMENTO TOTAL", est["kpi_label"]),
        Paragraph("TOTAL COMPRADO", est["kpi_label"]),
        Paragraph("PRESSÃO DE ESTOQUE", est["kpi_label"]),
    ], [
        Paragraph(f"R$ {_brl(ind.faturamento_total)}", est["kpi_value"]),
        Paragraph(f"R$ {_brl(ind.total_comprado)}", est["kpi_value"]),
        Paragraph(f"R$ {_brl(ind.saldo_estimado_compras_vendas)}", est["kpi_value"]),
    ]]
    col = largura / 3.0
    t = Table(dados, colWidths=[col, col, col])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), FUNDO_SUAVE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINHA),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINHA),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 9), ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _abc_flowable(abc_top, largura, est):
    head = ["#", "Produto", "Classe", "Faturamento (R$)", "% acum."]
    linhas = [head]
    for p in abc_top[:5]:
        linhas.append([
            str(p.posicao_ranking),
            Paragraph(p.produto_nome, est["cell"]),
            p.classe_abc,
            _brl(p.faturamento),
            f"{float(p.percentual_acumulado or 0):.1f}%",
        ])
    t = Table(linhas, colWidths=[10 * mm, largura - 90 * mm, 18 * mm, 34 * mm, 22 * mm])
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), ROXO),
        ("TEXTCOLOR", (0, 0), (-1, 0), white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("ALIGN", (3, 0), (4, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINHA),
        ("BOX", (0, 0), (-1, -1), 0.5, LINHA),
    ]
    # Cor da célula de classe + zebra
    for i, p in enumerate(abc_top[:5], start=1):
        cor = CLASSE_COR.get(p.classe_abc, CINZA)
        estilo.append(("TEXTCOLOR", (2, i), (2, i), cor))
        estilo.append(("FONTNAME", (2, i), (2, i), "Helvetica-Bold"))
        if i % 2 == 0:
            estilo.append(("BACKGROUND", (0, i), (-1, i), FUNDO_SUAVE))
    t.setStyle(TableStyle(estilo))
    return t


def _semaforo_flowable(card, largura, est):
    cor = SEMAFORO_COR.get(card["nivel"], CINZA)
    fundo = SEMAFORO_FUNDO.get(card["nivel"], FUNDO_SUAVE)
    rotulo = SEMAFORO_ROTULO.get(card["nivel"], card["nivel"].upper())
    interno = Table([
        [Paragraph(f'<b>{card["titulo"]}</b>  <font size="7" color="#6a7088">· {rotulo}</font>', est["sc_title"])],
        [Paragraph("O QUE FAZER", est["sc_key"])],
        [Paragraph(card["o_que_fazer"], est["sc_val"])],
        [Paragraph("POR QUE FAZER", est["sc_key"])],
        [Paragraph(card["por_que"], est["sc_val"])],
    ], colWidths=[largura - 8 * mm])
    interno.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 1), (0, 1), 5),
    ]))
    wrap = Table([[interno]], colWidths=[largura])
    wrap.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fundo),
        ("LINEBEFORE", (0, 0), (0, -1), 3, cor),
        ("BOX", (0, 0), (-1, -1), 0.4, LINHA),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return wrap


def gerar_pdf_analise(analise, semaforos, abc_top) -> bytes:
    """Monta o PDF da análise e devolve os bytes."""
    buf = BytesIO()
    margem = 18 * mm
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=margem, rightMargin=margem, topMargin=15 * mm, bottomMargin=20 * mm,
        title=f"Analise NEXO #{analise.id_analise}", author="NEXO",
    )
    largura = doc.width
    est = _estilos()
    emp = analise.empresa
    rel = analise.relatorio
    ind = analise.indicador

    elems = []
    ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
    elems.append(_BarraTopo(largura, "Análise Executiva", f"Referência {ref}"))
    elems.append(Spacer(1, 8 * mm))

    titulo = rel.titulo if rel else "Análise publicada"
    elems.append(Paragraph(titulo, ParagraphStyle(
        "tit", fontName="Helvetica-Bold", fontSize=16, textColor=TINTA, leading=19)))
    periodo = f"{analise.periodo_inicio.strftime('%d/%m/%Y')} – {analise.periodo_fim.strftime('%d/%m/%Y')}"
    elems.append(Paragraph(
        f"{emp.nome_fantasia or emp.razao_social} &nbsp;·&nbsp; CNPJ {emp.cnpj} &nbsp;·&nbsp; Período {periodo}",
        est["small"]))
    elems.append(Spacer(1, 6 * mm))

    # Indicadores
    if ind:
        elems.append(Paragraph("Indicadores do período", est["h2"]))
        elems.append(_kpis_flowable(ind, largura, est))
        elems.append(Spacer(1, 6 * mm))

    # Curva ABC
    if abc_top:
        elems.append(Paragraph("Curva ABC · Top 5 produtos (Princípio de Pareto)", est["h2"]))
        elems.append(_abc_flowable(abc_top, largura, est))
        elems.append(Paragraph(
            "Classe A: até 80% do faturamento acumulado (estratégicos) · "
            "B: 80–95% (táticos) · C: ~5% restantes (operacionais).", est["small"]))
        elems.append(Spacer(1, 6 * mm))

    # Síntese executiva (semáforos)
    if semaforos:
        elems.append(Paragraph("Síntese executiva (5W2H)", est["h2"]))
        for card in semaforos:
            elems.append(_semaforo_flowable(card, largura, est))
            elems.append(Spacer(1, 3 * mm))
        elems.append(Spacer(1, 3 * mm))

    # Devolutiva
    if rel:
        elems.append(Paragraph("Devolutiva do consultor", est["h2"]))
        if rel.resumo_executivo:
            elems.append(Paragraph(f'<b>Resumo executivo.</b> {rel.resumo_executivo}', est["p"]))
            elems.append(Spacer(1, 3 * mm))
        elems.append(Paragraph(f'<b>Conclusão estratégica.</b> {rel.conclusao_estrategica}', est["p"]))

    elems.append(Spacer(1, 6 * mm))
    elems.append(Paragraph(
        f"Documento gerado pelo portal NEXO em {datetime.now().strftime('%d/%m/%Y %H:%M')}.",
        est["small"]))

    doc.build(elems, onFirstPage=_rodape, onLaterPages=_rodape)
    return buf.getvalue()
