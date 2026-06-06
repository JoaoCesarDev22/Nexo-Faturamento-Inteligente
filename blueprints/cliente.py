"""
NEXO - Blueprint do CLIENTE
============================
Rotas:
  /cliente/dashboard  → visão geral com a última devolutiva publicada (CONCLUIDO),
                        histórico recente, análises em andamento e KPIs do
                        Indicador de Pressão de Estoque.

Regras:
  - Acesso restrito a Usuario.role == 'CLIENTE' (via cliente_required).
  - "Análise publicada" = status_analise == 'CONCLUIDO' (a transição para
    CONCLUIDO ocorre apenas quando o ADMIN publica a devolutiva estratégica,
    nunca automaticamente pelo ETL).
  - Os KPIs vêm de indicador_analise; o ETL persiste só os consolidados.
"""

from functools import wraps

from flask import Blueprint, render_template, abort
from flask_login import login_required, current_user
from sqlalchemy import select

from extensions import db
from models import Analise, IndicadorAnalise, RelatorioAnalise

cliente_bp = Blueprint("cliente", __name__)


def cliente_required(f):
    """Decorator: bloqueia rota para qualquer não-CLIENTE."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_cliente:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


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

    # Dados para os gráficos Plotly. Só montamos quando há indicador real;
    # o template renderiza este bloco apenas se `analise.indicador` existir.
    dashboard_data = None
    if analise and analise.indicador:
        ind: IndicadorAnalise = analise.indicador
        dashboard_data = {
            "comparativo": {
                "labels": ["Compras", "Vendas"],
                "values": [float(ind.total_comprado or 0), float(ind.faturamento_total or 0)],
            },
            "pressao_estoque": float(ind.saldo_estimado_compras_vendas or 0),
        }

    return render_template(
        "cliente/dashboard.html",
        empresa=empresa,
        analise=analise,
        analises=analises,
        analises_em_andamento=analises_em_andamento,
        dashboard_data=dashboard_data,
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
    Visão detalhada de uma análise publicada (KPIs + texto da devolutiva).

    Guards de segurança/visibilidade:
      - A análise precisa pertencer à empresa do cliente logado (não vê de outros lojistas).
      - A análise precisa estar em CONCLUIDO E ter relatorio.publicado=True.
        Caso contrário → 404 (do ponto de vista do cliente a entrega não existe ainda).
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
    return render_template("cliente/analise.html", analise=analise)
