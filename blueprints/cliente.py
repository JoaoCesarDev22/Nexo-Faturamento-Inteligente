"""
NEXO - Blueprint do CLIENTE
============================
Rotas:
  /cliente/dashboard  → visão geral com a última devolutiva publicada (CONCLUIDO),
                        histórico recente, análises em andamento, KPIs do Indicador
                        de Pressão de Estoque, Curva ABC e semáforos de risco.
  /cliente/historico  → lista de análises publicadas.
  /cliente/analise/<id> → detalhe de uma análise publicada.
  /cliente/upload     → self-service onboarding: o cliente anexa os relatórios
                        ERP (Vendas + Compras) das análises abertas para a sua
                        empresa; o consultor NEXO homologa e dispara o ETL.

Regras:
  - Acesso restrito a Usuario.role == 'CLIENTE' (via cliente_required).
  - "Análise publicada" = status_analise == 'CONCLUIDO' (transição feita só
    quando o ADMIN publica a devolutiva, nunca automaticamente pelo ETL).
  - KPIs vêm de indicador_analise; o ranking ABC, de produto_curva_abc.
  - Uploads do cliente nascem com id_usuario_admin = NULL (aguardando validação).
"""

import os
import hashlib
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint, render_template, abort, request, redirect, url_for, flash, current_app,
    Response,
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import select

from extensions import db
from models import (
    Analise, IndicadorAnalise, RelatorioAnalise, UploadRelatorio,
    ChamadoSuporte, MensagemSuporte,
)
from insights import gerar_semaforos
from notifications import notificar_admins
from pdf_export import gerar_pdf_analise

cliente_bp = Blueprint("cliente", __name__)

# Estados em que uma análise ainda aceita anexos do cliente.
STATUS_ACEITA_UPLOAD = ("AGUARDANDO_RELATORIO", "RELATORIO_RECEBIDO")

# Categorias de ticket (token persistido -> rótulo exibido).
CATEGORIAS_TICKET = {
    "FINANCEIRO": "Financeiro",
    "DUVIDA_TECNICA": "Dúvida Técnica",
    "ERRO_INTEGRACAO": "Erro de Integração",
}


def cliente_required(f):
    """Decorator: bloqueia rota para qualquer não-CLIENTE."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_cliente:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# =====================================================================
# Helpers de montagem dos dados de gráfico (Plotly)
# =====================================================================
def _abc_chart(analise, limite: int = 10) -> dict | None:
    """
    Dados do Gráfico de Pareto da Curva ABC (Top-N por faturamento):
    barras (faturamento absoluto) + linha de % acumulado. O percentual
    acumulado é o do total geral (calculado no ETL via pandas cumsum),
    então a linha reflete a concentração real da receita.
    """
    if not analise or not analise.curva_abc:
        return None
    top = analise.curva_abc[:limite]  # já ordenado por posicao_ranking no relationship
    return {
        "labels": [p.produto_nome for p in top],
        "values": [float(p.faturamento or 0) for p in top],
        "classes": [p.classe_abc for p in top],
        "acumulado": [float(p.percentual_acumulado or 0) for p in top],
    }


def _historico_temporal(empresa) -> dict | None:
    """
    Série temporal Vendas (faturamento) x Compras (volume comprado) ao longo das
    análises CONCLUIDAS da empresa — alimenta o gráfico de linha comparativo.
    Precisa de >= 2 pontos para fazer sentido como histórico.
    """
    linhas = db.session.execute(
        select(Analise, IndicadorAnalise)
        .join(IndicadorAnalise, IndicadorAnalise.id_analise == Analise.id_analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise == "CONCLUIDO")
        .order_by(Analise.ano_referencia.asc(), Analise.mes_referencia.asc(),
                  Analise.id_analise.asc())
    ).all()
    if len(linhas) < 2:
        return None
    return {
        "labels": [f"{a.mes_referencia:02d}/{a.ano_referencia}" for a, _ in linhas],
        "vendas": [float(ind.faturamento_total or 0) for _, ind in linhas],
        "compras": [float(ind.total_comprado or 0) for _, ind in linhas],
    }


@cliente_bp.route("/dashboard")
@cliente_required
def dashboard():
    empresa = current_user.empresa  # garantido pelo CHECK do banco

    # Última análise CONCLUIDA (devolutiva publicada) da empresa do cliente.
    analise = db.session.execute(
        select(Analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise == "CONCLUIDO")
        .order_by(Analise.data_conclusao.desc().nullslast(), Analise.data_criacao.desc())
        .limit(1)
    ).scalar_one_or_none()

    # Lista de todas as análises publicadas (para "Histórico recente").
    analises = db.session.execute(
        select(Analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise == "CONCLUIDO")
        .order_by(Analise.data_conclusao.desc().nullslast(), Analise.data_criacao.desc())
    ).scalars().all()

    # Análises em andamento (não-CONCLUIDO) para a empresa.
    analises_em_andamento = db.session.execute(
        select(Analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise != "CONCLUIDO")
        .order_by(Analise.data_criacao.desc())
    ).scalars().all()

    # Dados para os gráficos Plotly e semáforos. Só montamos quando há indicador real.
    dashboard_data = None
    semaforos = []
    if analise and analise.indicador:
        ind: IndicadorAnalise = analise.indicador
        dashboard_data = {
            "comparativo": {
                "labels": ["Compras", "Vendas"],
                "values": [float(ind.total_comprado or 0), float(ind.faturamento_total or 0)],
            },
            "pressao_estoque": float(ind.saldo_estimado_compras_vendas or 0),
            "curva_abc": _abc_chart(analise),
            "historico": _historico_temporal(empresa),
        }
        semaforos = gerar_semaforos(ind, analise.curva_abc)

    return render_template(
        "cliente/dashboard.html",
        empresa=empresa,
        analise=analise,
        analises=analises,
        analises_em_andamento=analises_em_andamento,
        dashboard_data=dashboard_data,
        semaforos=semaforos,
    )


@cliente_bp.route("/historico")
@cliente_required
def historico():
    """
    Lista completa das análises CONCLUIDAS com devolutiva publicada para a empresa do cliente.
    Ordenada por data_conclusao desc com tiebreak determinístico em id_analise desc.
    """
    empresa = current_user.empresa
    analises = db.session.execute(
        select(Analise)
        .join(RelatorioAnalise, RelatorioAnalise.id_analise == Analise.id_analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise == "CONCLUIDO")
        .where(RelatorioAnalise.publicado.is_(True))
        .order_by(Analise.data_conclusao.desc().nullslast(), Analise.id_analise.desc())
    ).scalars().all()
    return render_template("cliente/historico.html", empresa=empresa, analises=analises)


@cliente_bp.route("/analise/<int:id_analise>")
@cliente_required
def analise(id_analise):
    """
    Visão detalhada de uma análise publicada (KPIs + Curva ABC + semáforos +
    texto da devolutiva).

    Guards de segurança/visibilidade:
      - A análise precisa pertencer à empresa do cliente logado.
      - Precisa estar em CONCLUIDO E ter relatorio.publicado=True. Senão → 404.
    """
    analise = db.session.get(Analise, id_analise)
    if (
        analise is None
        or analise.id_empresa != current_user.empresa.id_empresa
        or analise.status_analise != "CONCLUIDO"
        or analise.relatorio is None
        or not analise.relatorio.publicado
    ):
        abort(404)

    semaforos = gerar_semaforos(analise.indicador, analise.curva_abc) if analise.indicador else []
    abc_chart = _abc_chart(analise)
    return render_template(
        "cliente/analise.html",
        analise=analise,
        semaforos=semaforos,
        abc_chart=abc_chart,
    )


@cliente_bp.route("/analise/<int:id_analise>/pdf")
@cliente_required
def analise_pdf(id_analise):
    """Exporta a Análise Executiva (ABC + 5W2H + devolutiva) em PDF (ReportLab)."""
    analise = db.session.get(Analise, id_analise)
    if (
        analise is None
        or analise.id_empresa != current_user.empresa.id_empresa
        or analise.status_analise != "CONCLUIDO"
        or analise.relatorio is None
        or not analise.relatorio.publicado
    ):
        abort(404)

    semaforos = gerar_semaforos(analise.indicador, analise.curva_abc) if analise.indicador else []
    pdf_bytes = gerar_pdf_analise(analise, semaforos, analise.curva_abc)
    nome = f"analise_nexo_{analise.ano_referencia}{analise.mes_referencia:02d}_{analise.id_analise}.pdf"
    return Response(
        pdf_bytes, mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


# =====================================================================
# Self-service onboarding: upload de relatórios ERP pelo CLIENTE
# =====================================================================
def _buscar_upload(id_analise: int, tipo: str):
    return db.session.execute(
        select(UploadRelatorio).where(
            UploadRelatorio.id_analise == id_analise,
            UploadRelatorio.tipo_relatorio == tipo,
        )
    ).scalar_one_or_none()


@cliente_bp.route("/upload", methods=["GET", "POST"])
@cliente_required
def upload():
    """
    GET  → lista as análises abertas da empresa (aguardando relatório) e o status
           de cada anexo (Vendas/Compras).
    POST → o cliente anexa um arquivo (VENDAS ou COMPRAS) a uma análise sua:
        - valida que a análise é da empresa do cliente e aceita upload;
        - valida extensão (ALLOWED_EXTENSIONS); calcula SHA-256 e tamanho;
        - substitui o anexo anterior do mesmo tipo (UNIQUE id_analise+tipo);
        - cria UploadRelatorio com id_usuario_admin=NULL (aguardando validação);
        - transição AGUARDANDO_RELATORIO → RELATORIO_RECEBIDO quando os 2 existem.
    NUNCA dispara o ETL — a homologação é exclusiva do consultor (admin).
    """
    empresa = current_user.empresa

    analises_abertas = db.session.execute(
        select(Analise)
        .where(Analise.id_empresa == empresa.id_empresa)
        .where(Analise.status_analise.in_(STATUS_ACEITA_UPLOAD))
        .order_by(Analise.data_criacao.desc())
    ).scalars().all()

    if request.method == "POST":
        try:
            id_analise = int(request.form.get("id_analise", ""))
        except ValueError:
            flash("Selecione uma análise válida.", "danger")
            return redirect(url_for("cliente.upload"))

        analise = db.session.get(Analise, id_analise)
        # Guard: a análise tem que existir, ser DA EMPRESA do cliente e aceitar upload.
        if (
            analise is None
            or analise.id_empresa != empresa.id_empresa
            or analise.status_analise not in STATUS_ACEITA_UPLOAD
        ):
            abort(404)

        tipo = (request.form.get("tipo_relatorio") or "").strip().upper()
        if tipo not in ("VENDAS", "COMPRAS"):
            flash("Tipo de relatório inválido (esperado VENDAS ou COMPRAS).", "danger")
            return redirect(url_for("cliente.upload"))

        arquivo = request.files.get("arquivo")
        if not arquivo or not arquivo.filename:
            flash("Selecione um arquivo para enviar.", "warning")
            return redirect(url_for("cliente.upload"))

        nome_seguro = secure_filename(arquivo.filename)
        ext = os.path.splitext(nome_seguro)[1].lower().lstrip(".")
        if ext not in current_app.config["ALLOWED_EXTENSIONS"]:
            flash(
                f"Extensão .{ext} não suportada. Aceitos: "
                f"{sorted(current_app.config['ALLOWED_EXTENSIONS'])}.", "danger",
            )
            return redirect(url_for("cliente.upload"))

        conteudo = arquivo.read()
        if not conteudo:
            flash("Arquivo vazio — nada foi salvo.", "warning")
            return redirect(url_for("cliente.upload"))
        sha = hashlib.sha256(conteudo).hexdigest()

        upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
        upload_folder.mkdir(parents=True, exist_ok=True)
        nome_final = f"analise_{id_analise}_{tipo}_{sha[:8]}.{ext}"
        caminho_final = upload_folder / nome_final

        # UNIQUE(id_analise, tipo) — re-upload substitui o anterior (mesmo cuidado
        # do fluxo admin: só remove o arquivo físico antigo se o caminho mudou).
        existente = _buscar_upload(id_analise, tipo)
        if existente and existente.caminho_arquivo and existente.caminho_arquivo != str(caminho_final):
            try:
                if os.path.exists(existente.caminho_arquivo):
                    os.remove(existente.caminho_arquivo)
            except OSError:
                pass
        if existente:
            db.session.delete(existente)
            db.session.flush()

        caminho_final.write_bytes(conteudo)

        db.session.add(UploadRelatorio(
            id_analise=id_analise,
            id_usuario_admin=None,  # enviado pelo cliente → aguardando validação
            tipo_relatorio=tipo,
            nome_arquivo_original=nome_seguro,
            caminho_arquivo=str(caminho_final),
            extensao_arquivo=ext.upper(),
            tamanho_arquivo=len(conteudo),
            hash_arquivo=sha,
            status_processamento="PENDENTE",
        ))

        outro = _buscar_upload(id_analise, "COMPRAS" if tipo == "VENDAS" else "VENDAS")
        if outro and analise.status_analise == "AGUARDANDO_RELATORIO":
            analise.status_analise = "RELATORIO_RECEBIDO"

        # Gatilho: avisa o consultor que há planilha nova aguardando homologação.
        nome_emp = empresa.nome_fantasia or empresa.razao_social
        notificar_admins(
            f"Nova planilha de {tipo.capitalize()} enviada por {nome_emp} aguardando homologação.",
            url_for("admin.analise_upload", id_analise=id_analise),
        )

        db.session.commit()
        flash(
            f"Relatório de {tipo} enviado. A equipe NEXO fará a validação e o "
            "processamento.", "success",
        )
        return redirect(url_for("cliente.upload"))

    return render_template("cliente/upload.html", empresa=empresa, analises=analises_abertas)


# =====================================================================
# Suporte: tickets (chamados) do cliente
# =====================================================================
@cliente_bp.route("/tickets", methods=["GET", "POST"])
@cliente_required
def tickets():
    """
    GET  → lista os chamados da empresa do cliente + formulário de novo ticket.
    POST → abre um novo chamado (assunto + categoria + 1ª mensagem) e notifica
           os admins.
    """
    empresa = current_user.empresa

    if request.method == "POST":
        assunto = (request.form.get("assunto") or "").strip()
        categoria = (request.form.get("categoria") or "").strip().upper()
        mensagem = (request.form.get("mensagem") or "").strip()

        if not assunto or not mensagem:
            flash("Informe um assunto e descreva a sua dúvida.", "warning")
            return redirect(url_for("cliente.tickets"))
        if categoria not in CATEGORIAS_TICKET:
            flash("Selecione uma categoria válida.", "danger")
            return redirect(url_for("cliente.tickets"))

        chamado = ChamadoSuporte(
            id_empresa=empresa.id_empresa,
            id_usuario_cliente=current_user.id_usuario,
            assunto=assunto,
            categoria=categoria,
            status_chamado="ABERTO",
        )
        db.session.add(chamado)
        db.session.flush()  # garante id_chamado para a mensagem
        db.session.add(MensagemSuporte(
            id_chamado=chamado.id_chamado,
            id_usuario_remetente=current_user.id_usuario,
            mensagem=mensagem,
        ))

        nome_emp = empresa.nome_fantasia or empresa.razao_social
        notificar_admins(
            f"Novo chamado de {nome_emp}: “{assunto}”.",
            url_for("admin.ticket_detalhe", id_chamado=chamado.id_chamado),
        )

        db.session.commit()
        flash("Chamado aberto com sucesso. A equipe NEXO responderá em breve.", "success")
        return redirect(url_for("cliente.ticket_detalhe", id_chamado=chamado.id_chamado))

    chamados = db.session.execute(
        select(ChamadoSuporte)
        .where(ChamadoSuporte.id_empresa == empresa.id_empresa)
        .order_by(ChamadoSuporte.data_abertura.desc(), ChamadoSuporte.id_chamado.desc())
    ).scalars().all()
    return render_template(
        "cliente/tickets.html",
        empresa=empresa, chamados=chamados, categorias=CATEGORIAS_TICKET,
    )


@cliente_bp.route("/tickets/<int:id_chamado>", methods=["GET", "POST"])
@cliente_required
def ticket_detalhe(id_chamado):
    """Conversa de um chamado da empresa do cliente; POST envia nova mensagem."""
    chamado = db.session.get(ChamadoSuporte, id_chamado)
    if chamado is None or chamado.id_empresa != current_user.empresa.id_empresa:
        abort(404)

    if request.method == "POST":
        texto = (request.form.get("mensagem") or "").strip()
        if not texto:
            flash("Digite uma mensagem.", "warning")
            return redirect(url_for("cliente.ticket_detalhe", id_chamado=id_chamado))

        db.session.add(MensagemSuporte(
            id_chamado=id_chamado,
            id_usuario_remetente=current_user.id_usuario,
            mensagem=texto,
        ))
        # Cliente respondeu → reabre se estava RESOLVIDO; notifica os admins.
        if chamado.status_chamado == "RESOLVIDO":
            chamado.status_chamado = "EM_ATENDIMENTO"
        chamado.data_atualizacao = datetime.now(timezone.utc)
        nome_emp = current_user.empresa.nome_fantasia or current_user.empresa.razao_social
        notificar_admins(
            f"Nova resposta de {nome_emp} no chamado “{chamado.assunto}”.",
            url_for("admin.ticket_detalhe", id_chamado=id_chamado),
        )
        db.session.commit()
        return redirect(url_for("cliente.ticket_detalhe", id_chamado=id_chamado))

    return render_template(
        "cliente/ticket_detalhe.html",
        chamado=chamado, categorias=CATEGORIAS_TICKET,
    )
