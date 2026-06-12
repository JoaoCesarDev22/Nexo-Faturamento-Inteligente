"""
NEXO - Blueprint de Notificações (sininho)
===========================================
Rotas compartilhadas por ADMIN e CLIENTE (qualquer usuário autenticado):
  POST /notificacoes/marcar-lidas   → marca todas as não-lidas como lidas.
  GET  /notificacoes/<id>/abrir      → marca aquela como lida e redireciona
                                       para o link_destino (ou dashboard).
A leitura/contagem para o sininho é injetada em todas as páginas pelo
context processor (ver app.create_app).
"""

from flask import Blueprint, redirect, url_for, request
from flask_login import login_required, current_user
from sqlalchemy import select, update

from extensions import db
from models import Notificacao

notificacoes_bp = Blueprint("notificacoes", __name__, url_prefix="/notificacoes")


def _destino_padrao() -> str:
    """Dashboard do perfil do usuário logado (fallback de redirecionamento)."""
    return url_for("admin.dashboard") if current_user.is_admin else url_for("cliente.dashboard")


@notificacoes_bp.route("/marcar-lidas", methods=["POST"])
@login_required
def marcar_lidas():
    db.session.execute(
        update(Notificacao)
        .where(Notificacao.id_usuario == current_user.id_usuario, Notificacao.lida.is_(False))
        .values(lida=True)
    )
    db.session.commit()
    # Volta para a página de origem quando possível (UX do dropdown).
    return redirect(request.referrer or _destino_padrao())


@notificacoes_bp.route("/<int:id_notificacao>/abrir")
@login_required
def abrir(id_notificacao):
    notif = db.session.get(Notificacao, id_notificacao)
    # Só o dono da notificação pode abri-la; senão, cai no dashboard.
    if notif is None or notif.id_usuario != current_user.id_usuario:
        return redirect(_destino_padrao())
    if not notif.lida:
        notif.lida = True
        db.session.commit()
    return redirect(notif.link_destino or _destino_padrao())
