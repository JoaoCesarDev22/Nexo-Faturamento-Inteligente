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
import logging
from pathlib import Path

from flask import Flask, redirect, url_for
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.engine import Engine

from config import config_by_name
from extensions import db, login_manager


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

    # Garante que a pasta instance/ exista (onde o SQLite vai morar).
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    # E a pasta de uploads.
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    # Inicializa extensões
    db.init_app(app)
    login_manager.init_app(app)

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

    # Registra blueprints
    from blueprints.auth import auth_bp
    from blueprints.admin import admin_bp
    from blueprints.cliente import cliente_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(cliente_bp, url_prefix="/cliente")

    # Rota raiz: redireciona conforme o perfil logado.
    @app.route("/")
    def index():
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if current_user.is_admin:
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("cliente.dashboard"))

    # Health check simples para verificar se a app subiu.
    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    return app


# Entry point para `python app.py` (dev local).
# Em produção, use um WSGI server (gunicorn, waitress).
if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=app.config.get("DEBUG", False))
