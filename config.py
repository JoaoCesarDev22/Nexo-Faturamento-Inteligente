"""
NEXO - Faturamento Inteligente | Configuração da aplicação
==========================================================
Configuração centralizada lida do .env via python-dotenv.
Por que centralizar: facilita troca de ambiente (dev/prod) e teste,
e evita espalhar os.environ.get() pelo código.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Diretório raiz do projeto (pasta que contém app.py)
BASE_DIR = Path(__file__).resolve().parent

# Carrega variáveis do .env localizado na raiz do projeto.
# Em produção, as variáveis virão direto do ambiente do servidor.
load_dotenv(BASE_DIR / ".env")


def _resolve_database_uri() -> str:
    """
    Resolve a URI do banco. Para SQLite relativo, transforma em absoluto
    baseado em BASE_DIR — evita 'unable to open database file' quando o
    Python é executado de diretórios diferentes (init_db, seed, app, testes).
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
    return raw


class Config:
    """Configuração base. Use para dev local."""

    # === Flask ===
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-key-trocar-em-producao")

    # === SQLAlchemy ===
    SQLALCHEMY_DATABASE_URI = _resolve_database_uri()
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
