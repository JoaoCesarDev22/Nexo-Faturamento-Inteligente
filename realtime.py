"""
NEXO - Faturamento Inteligente | Camada de tempo real (Socket.IO)
=================================================================
Handlers de conexão WebSocket. Cada usuário autenticado entra numa SALA
exclusiva (`user_<id>`), para a qual as notificações são emitidas de forma
direcionada (nunca em broadcast aberto). Conexões anônimas são recusadas.

A integração com Flask-Login funciona porque o handshake do Socket.IO carrega
o mesmo cookie de sessão da app (mesmo domínio) — então `current_user` está
disponível dentro dos handlers.
"""

import logging

from flask_login import current_user
from flask_socketio import join_room

from extensions import socketio

logger = logging.getLogger(__name__)


def sala_do_usuario(id_usuario) -> str:
    return f"user_{id_usuario}"


@socketio.on("connect")
def _on_connect():
    # Recusa o handshake de quem não está logado.
    if not getattr(current_user, "is_authenticated", False):
        return False
    join_room(sala_do_usuario(current_user.id_usuario))
    logger.info("Socket conectado: usuário %s", current_user.id_usuario)
    return True


@socketio.on("disconnect")
def _on_disconnect():
    # A saída da sala é automática ao desconectar; só registramos.
    if getattr(current_user, "is_authenticated", False):
        logger.info("Socket desconectado: usuário %s", current_user.id_usuario)
