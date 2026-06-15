"""
NEXO - Faturamento Inteligente | Configuração da aplicação
==========================================================
Configuração centralizada lida do .env via python-dotenv.
Por que centralizar: facilita troca de ambiente (dev/prod) e teste,
e evita espalhar os.environ.get() pelo código.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from dotenv import load_dotenv

# Diretório raiz do projeto (pasta que contém app.py)
BASE_DIR = Path(__file__).resolve().parent

# Carrega variáveis do .env localizado na raiz do projeto.
# Em produção, as variáveis virão direto do ambiente do servidor.
load_dotenv(BASE_DIR / ".env")


# Query params que alguns provedores (Prisma/Supabase) anexam à URL mas que o
# psycopg2/SQLAlchemy NÃO reconhecem — passá-los adiante quebra a conexão com
# "invalid connection option". São removidos antes de entregar a URL ao engine.
_PARAMS_PG_INCOMPATIVEIS = {"pgbouncer", "connection_limit", "pool_timeout", "schema"}


def _limpar_url_pg(url: str) -> str:
    """Remove de uma URL Postgres os query params que o psycopg2 não aceita."""
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.scheme.startswith("postgres"):
        return url
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _PARAMS_PG_INCOMPATIVEIS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _resolve_database_uri() -> str:
    """
    Resolve a URI do banco (runtime da app). Para SQLite relativo, transforma em
    absoluto baseado em BASE_DIR. Para Postgres, remove params incompatíveis
    (ex.: ?pgbouncer=true) antes de entregar ao SQLAlchemy.
    """
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        # Sem env: usa o default absoluto.
        return f"sqlite:///{(BASE_DIR / 'instance' / 'nexo_mvp.db').as_posix()}"
    # Se a URI SQLite for relativa, transforma em absoluta.
    if raw.startswith("sqlite:///") and not raw.startswith("sqlite:////"):
        caminho_relativo = raw.replace("sqlite:///", "", 1)
        caminho_abs = (BASE_DIR / caminho_relativo).resolve()
        return f"sqlite:///{caminho_abs.as_posix()}"
    return _limpar_url_pg(raw)


def _resolve_direct_uri() -> str:
    """
    URI de conexão DIRETA (porta 5432) usada para DDL/migrações (Alembic).
    No Supabase, migrações devem usar a conexão direta, não o PgBouncer (6543).
    Cai na URI principal se DIRECT_URL não estiver definida.
    """
    raw = os.environ.get("DIRECT_URL", "").strip()
    return _limpar_url_pg(raw) if raw else _resolve_database_uri()


class Config:
    """Configuração base. Use para dev local."""

    # === Flask ===
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-key-trocar-em-producao")

    # === SQLAlchemy ===
    SQLALCHEMY_DATABASE_URI = _resolve_database_uri()
    # URI direta (5432) para Alembic/Flask-Migrate. A app usa a pooled (6543);
    # as migrações usam esta quando create_app roda em modo direto.
    SQLALCHEMY_DIRECT_URI = _resolve_direct_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Mostra SQL gerado no console — útil em dev, ruim em prod.
    SQLALCHEMY_ECHO = False

    # === Admin Master (seed) ===
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@nexo.com.br")
    ADMIN_NOME = os.environ.get("ADMIN_NOME", "Administrador NEXO")
    ADMIN_SENHA = os.environ.get("ADMIN_SENHA")  # sem default — quebra se faltar

    # === Uploads ===
    UPLOAD_FOLDER = BASE_DIR / os.environ.get("UPLOAD_FOLDER", "uploads")
    MAX_CONTENT_LENGTH = int(
        os.environ.get("MAX_CONTENT_LENGTH_MB", 20)
    ) * 1024 * 1024  # MB → bytes

    # === E-mail (Flask-Mail / SMTP) ===
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USE_SSL = os.environ.get("MAIL_USE_SSL", "false").lower() == "true"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME") or None
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD") or None
    MAIL_DEFAULT_SENDER = os.environ.get(
        "MAIL_DEFAULT_SENDER",
        os.environ.get("MAIL_USERNAME") or "nexofaturamentointeligente@gmail.com",
    )
    # SEGURANÇA: por padrão o Flask-Mail herda app.debug e, em modo debug, o
    # smtplib imprime toda a conversa SMTP no log — inclusive a linha AUTH em
    # base64, que contém a senha. Forçamos OFF para a credencial nunca vazar.
    MAIL_DEBUG = False
    # Quando não há credenciais, o envio é suprimido pela camada emails.py
    # (sem quebrar fluxos). Flag derivada para checagem rápida nos gatilhos.
    MAIL_ATIVO = bool(MAIL_USERNAME and MAIL_PASSWORD)
    # Formatos REALMENTE suportados pelo ETL. .xls fica de fora porque não há
    # dependência (xlrd) nem caminho de leitura — não anunciamos o que não lemos.
    ALLOWED_EXTENSIONS = {"csv", "xlsx"}


class DevelopmentConfig(Config):
    DEBUG = True
    SQLALCHEMY_ECHO = True


class ProductionConfig(Config):
    DEBUG = False
    # Em produção, SECRET_KEY DEVE vir do ambiente. Sem fallback.
    SECRET_KEY = os.environ["SECRET_KEY"]


# Dicionário usado pela factory para escolher config por nome.
config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
}
