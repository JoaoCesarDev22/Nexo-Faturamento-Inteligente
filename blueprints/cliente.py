"""
NEXO - Blueprint do CLIENTE
============================
Rotas: /cliente/dashboard

Stub da Semana 1. O dashboard real (com Plotly.js e KPIs) é
responsabilidade da Pessoa 3 na Semana 3.
"""

from functools import wraps
from flask import Blueprint, render_template, abort
from flask_login import login_required, current_user

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
    """
    Estado inicial: mostra mensagem de "primeira análise em preparação"
    quando empresa do cliente ainda não tem análise publicada.
    Lógica completa será implementada na Semana 3.
    """
    return render_template(
        "cliente/dashboard.html",
        empresa=current_user.empresa,
    )
