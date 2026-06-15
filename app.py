"""
NEXO - Faturamento Inteligente | Entry point Flask
===================================================
Application Factory pattern. A função `create_app()` é a única
forma de instanciar a aplicação — facilita testes, múltiplos ambientes
e evita import circular.

EVENT LISTENER CRÍTICO:
    O SQLite vem com foreign keys DESLIGADAS por padrão.
    O PRAGMA escrito no .sql só vale na sessão que executou.
    A função `_set_sqlite_pragma` abaixo é registrada via SQLAlchemy event
    e força `PRAGMA foreign_keys = ON` em TODA conexão nova.
    SEM ISSO, qualquer FK no schema é decorativa.
"""

import os
import secrets
import logging
from pathlib import Path

from flask import Flask, redirect, url_for, render_template, session, request
from flask_login import current_user, logout_user
from sqlalchemy import event
from sqlalchemy.engine import Engine

from config import config_by_name
from extensions import db, login_manager, migrate, mail, socketio


def _set_sqlite_pragma(dbapi_connection, connection_record):
    """
    Garante que CADA conexão SQLite seja aberta com:
      - foreign_keys = ON   → FKs validadas de verdade
      - journal_mode = WAL  → permite múltiplos leitores + 1 escritor (melhor no Flask)
      - synchronous = NORMAL → equilíbrio razoável entre durabilidade e velocidade

    Registrada globalmente abaixo via @event.listens_for(Engine, "connect").
    Aplica-se a TODOS os engines SQLite — tanto da app principal quanto de
    qualquer script auxiliar (init_db.py, seed.py) desde que importem este módulo.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()


# Registra o listener no nível do Engine SQLAlchemy.
# Atenção: tem que ser ANTES de qualquer conexão ser aberta.
@event.listens_for(Engine, "connect")
def _connect_listener(dbapi_connection, connection_record):
    # SQLAlchemy aceita engines de outros bancos (Postgres, MySQL).
    # Aplicar PRAGMA SQLite em Postgres explode. Por segurança, só roda
    # se a conexão for de fato SQLite.
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        _set_sqlite_pragma(dbapi_connection, connection_record)


def create_app(config_name: str = None) -> Flask:
    """
    Factory da aplicação. Use:
        from app import create_app
        app = create_app("development")
    """
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_by_name[config_name])

    # Identificador único deste BOOT do servidor. Como a SECRET_KEY é fixa, os
    # cookies de sessão sobreviveriam a um restart e re-logariam o usuário
    # automaticamente. Carimbamos a sessão com este BOOT_ID no login e, a cada
    # request, invalidamos qualquer sessão de um boot anterior (ver guard abaixo).
    # Resultado: derrubar/subir o servidor SEMPRE exige novo login.
    app.config["BOOT_ID"] = secrets.token_hex(8)

    # Modo DIRETO de banco: usado por DDL/migrações (Alembic) para falar com o
    # Postgres na conexão direta (5432) em vez do PgBouncer (6543). Ativado por
    #   NEXO_DB_DIRECT=1 flask db upgrade
    if os.environ.get("NEXO_DB_DIRECT") == "1":
        app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DIRECT_URI"]

    # Garante que a pasta instance/ exista (onde o SQLite vai morar).
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    # E a pasta de uploads.
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    # Inicializa extensões
    db.init_app(app)
    login_manager.init_app(app)
    # Flask-Migrate precisa enxergar os modelos — importados logo abaixo no
    # user_loader/blueprints; aqui basta registrar db + app.
    migrate.init_app(app, db)
    mail.init_app(app)
    socketio.init_app(app)
    # Registra os handlers de WebSocket (connect/disconnect → salas por usuário).
    import realtime  # noqa: F401

    # Configura logging básico
    logging.basicConfig(
        level=logging.INFO if not app.debug else logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    # Registra user loader do Flask-Login
    # (Necessário aqui porque depende do modelo Usuario)
    from models import Usuario

    @login_manager.user_loader
    def load_user(user_id: str):
        # Usuario.id_usuario é INTEGER; Flask-Login passa como string.
        return db.session.get(Usuario, int(user_id))

    @app.before_request
    def _guard_sessao():
        """
        Blindagem contra login automático:
          1) Sessão carimbada com um BOOT_ID diferente do atual = sobrou de um
             boot anterior do servidor (ou nunca foi carimbada) → desloga e
             limpa, forçando novo login após restart.
          2) Cookie órfão: aponta para um usuário que não existe mais no banco
             (ex.: banco resetado) → current_user vira anônimo, mas o resíduo
             _user_id é limpo da sessão.
        Não toca em requisições de assets estáticos (sem custo por arquivo).
        """
        if request.endpoint == "static":
            return
        if current_user.is_authenticated:
            if session.get("boot_id") != app.config["BOOT_ID"]:
                logout_user()
                session.clear()
        elif "_user_id" in session:
            session.clear()

    @app.before_request
    def _guard_primeiro_acesso():
        """
        Força a troca de senha no primeiro acesso: um CLIENTE logado com
        primeiro_acesso=True é interceptado e mandado para /auth/primeiro-acesso,
        bloqueando dashboard, suporte, upload etc. até definir a nova senha.
        Libera apenas: assets estáticos, a própria rota de troca e o logout.
        """
        if request.endpoint in ("static", "auth.primeiro_acesso", "auth.logout"):
            return
        if (
            current_user.is_authenticated
            and current_user.is_cliente
            and getattr(current_user, "primeiro_acesso", False)
        ):
            return redirect(url_for("auth.primeiro_acesso"))

    # Registra blueprints
    from blueprints.auth import auth_bp
    from blueprints.admin import admin_bp
    from blueprints.cliente import cliente_bp
    from blueprints.notificacoes import notificacoes_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(cliente_bp, url_prefix="/cliente")
    app.register_blueprint(notificacoes_bp)

    # Context processor: injeta os dados do sininho (contagem + 5 recentes) em
    # TODA página renderizada, para o usuário autenticado. Mantém o template
    # base.html agnóstico de qual rota o serviu.
    from notifications import contexto_sininho, classificar_notificacao

    @app.context_processor
    def _injeta_sininho():
        return contexto_sininho(current_user)

    # Expõe o classificador de badge ao template (mesma lógica do payload do socket).
    app.jinja_env.globals["badge_notif"] = classificar_notificacao

    # Rota raiz: serve a landing page institucional (entrada comercial pública).
    # Usuários já autenticados também caem aqui — basta clicar em "Acessar portal"
    # que o auth.login os redireciona direto para o dashboard do perfil correto.
    @app.route("/")
    def index():
        return render_template("landing.html")

    # Onboarding comercial: explica como obter acesso (handoff via WhatsApp + reunião).
    # Linkado pelo login ("É novo por aqui?") e pelo footer da landing.
    @app.route("/como-aderir")
    def como_aderir():
        return render_template("como_aderir.html")

    # Health check simples para verificar se a app subiu.
    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    return app


# Entry point para `python app.py` (dev local).
# Em produção, use um servidor compatível com WebSocket (ex.: gunicorn com
# worker apropriado). O socketio.run sobe o servidor com suporte a WebSocket.
if __name__ == "__main__":
    app = create_app()
    socketio.run(
        app,
        host="127.0.0.1",
        port=5000,
        debug=app.config.get("DEBUG", False),
        # Necessário para rodar sobre o servidor de dev do Werkzeug em modo
        # threading (Flask-SocketIO 5.x exige este opt-in fora de produção).
        allow_unsafe_werkzeug=True,
    )
