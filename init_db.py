"""
NEXO - Script para recriar o banco do zero
===========================================
Execute UMA VEZ, no início da Semana 1:
    python init_db.py

ATENÇÃO: este script APAGA o banco existente se houver.
Em dev é o que queremos (recomeçar limpo).
Em produção, NUNCA rode isto — use migrations (Alembic) na v2.

Por que existe: o banco que veio do zip tinha uma tabela fantasma
(`historico_planos`) e foi criado sem PRAGMA foreign_keys ativo.
Esta limpeza inicial garante alinhamento total com o schema V4.
"""

from pathlib import Path
import os

# Importa app + db ANTES de qualquer operação para que o event listener
# de PRAGMA foreign_keys seja registrado.
from app import create_app
from extensions import db

# Importa modelos para que sejam registrados no metadata.
import models  # noqa: F401


def main():
    app = create_app("development")

    # Caminho do banco a partir da URI do SQLAlchemy
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    # Para SQLite, a URI é "sqlite:///caminho/para/arquivo.db"
    if uri.startswith("sqlite:///"):
        db_path = Path(uri.replace("sqlite:///", "", 1))
        if db_path.exists():
            print(f"⚠️  Apagando banco existente: {db_path}")
            db_path.unlink()
        else:
            print(f"ℹ️  Banco ainda não existe em {db_path}. Criando do zero.")
    else:
        print(f"ℹ️  Banco não-SQLite detectado: {uri}. Vou apenas drop_all + create_all.")

    with app.app_context():
        # drop_all serve para banco não-SQLite ou se já existir alguma tabela
        db.drop_all()
        db.create_all()
        print("✅ Tabelas do MVP criadas com sucesso.")

        # Verificação rápida: lista as tabelas que existem agora.
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tabelas = sorted(inspector.get_table_names())
        print(f"📋 {len(tabelas)} tabelas no banco:")
        for t in tabelas:
            print(f"   - {t}")

        # Verifica se PRAGMA foreign_keys está ATIVO (validação do event listener)
        with db.engine.connect() as conn:
            from sqlalchemy import text
            result = conn.execute(text("PRAGMA foreign_keys")).scalar()
            estado = "✅ ATIVO" if result == 1 else "❌ DESLIGADO (problema!)"
            print(f"🔑 PRAGMA foreign_keys: {estado}")


if __name__ == "__main__":
    main()
