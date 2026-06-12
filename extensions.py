"""
NEXO - Faturamento Inteligente | Extensões Flask
================================================
Instâncias compartilhadas de SQLAlchemy e LoginManager.
Padrão: criar aqui, inicializar dentro da factory em app.py.
Isso evita import circular entre models.py e app.py.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_mail import Mail
from flask_socketio import SocketIO
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base declarativa SQLAlchemy 2.x. Todos os modelos herdam dela."""
    pass


# Instância única usada em toda a aplicação.
db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()
# Flask-Migrate (Alembic): versionamento de schema do Postgres/Supabase.
migrate = Migrate()
# Flask-Mail: envio de e-mails transacionais (SMTP).
mail = Mail()
# Flask-SocketIO: notificações em tempo real (sininho ao vivo).
# async_mode "threading" é o modo estável no Python 3.12 (sem eventlet/gevent),
# usando o driver simple-websocket. cors liberado p/ o cliente JS da própria app.
socketio = SocketIO(async_mode="threading", cors_allowed_origins="*", logger=False, engineio_logger=False)

# Rota para onde Flask-Login redireciona quando @login_required falha.
login_manager.login_view = "auth.login"
login_manager.login_message = "Faça login para acessar esta página."
login_manager.login_message_category = "warning"
