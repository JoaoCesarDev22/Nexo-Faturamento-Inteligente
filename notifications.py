"""
NEXO - Faturamento Inteligente | Notificações (sininho da navbar)
=================================================================
Helpers para CRIAR notificações dirigidas a usuários. Seguem a disciplina de
transação do projeto: apenas fazem `session.add(...)` — quem chama é responsável
pelo commit (normalmente já há um commit logo após o gatilho).

Gatilhos atuais:
  - cliente envia relatório (upload)      -> notifica ADMIN(s)
  - admin homologa/processa o ETL         -> notifica CLIENTE(s) da empresa
  - admin publica a devolutiva            -> notifica CLIENTE(s) da empresa
  - nova resposta em chamado de suporte   -> notifica a contraparte
"""

from sqlalchemy import select

from extensions import db
from models import Notificacao, Usuario


def criar_notificacao(id_usuario: int, texto: str, link_destino: str | None = None) -> Notificacao:
    """Cria UMA notificação (add na sessão, sem commit)."""
    n = Notificacao(id_usuario=id_usuario, texto=texto, link_destino=link_destino)
    db.session.add(n)
    return n


def notificar_admins(texto: str, link_destino: str | None = None) -> int:
    """Notifica TODOS os usuários ADMIN ativos. Retorna a quantidade gerada."""
    admins = db.session.execute(
        select(Usuario.id_usuario).where(Usuario.role == "ADMIN", Usuario.ativo.is_(True))
    ).scalars().all()
    for uid in admins:
        criar_notificacao(uid, texto, link_destino)
    return len(admins)


def notificar_clientes_empresa(id_empresa: int, texto: str, link_destino: str | None = None) -> int:
    """Notifica os usuários CLIENTE ativos vinculados a uma empresa."""
    clientes = db.session.execute(
        select(Usuario.id_usuario).where(
            Usuario.role == "CLIENTE",
            Usuario.id_empresa == id_empresa,
            Usuario.ativo.is_(True),
        )
    ).scalars().all()
    for uid in clientes:
        criar_notificacao(uid, texto, link_destino)
    return len(clientes)


def contexto_sininho(usuario) -> dict:
    """
    Dados do sininho para o usuário autenticado: contagem de não-lidas e as
    5 notificações mais recentes. Usado pelo context processor (toda página).
    """
    if usuario is None or not getattr(usuario, "is_authenticated", False):
        return {"notif_nao_lidas": 0, "notif_recentes": []}

    nao_lidas = db.session.execute(
        select(db.func.count(Notificacao.id_notificacao)).where(
            Notificacao.id_usuario == usuario.id_usuario,
            Notificacao.lida.is_(False),
        )
    ).scalar() or 0

    recentes = db.session.execute(
        select(Notificacao)
        .where(Notificacao.id_usuario == usuario.id_usuario)
        .order_by(Notificacao.data_criacao.desc(), Notificacao.id_notificacao.desc())
        .limit(5)
    ).scalars().all()

    return {"notif_nao_lidas": int(nao_lidas), "notif_recentes": recentes}
