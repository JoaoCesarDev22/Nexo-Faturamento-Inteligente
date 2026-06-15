"""
Cria (ou reseta) um CLIENTE de demonstração com primeiro_acesso=True, para
mostrar o fluxo de interceptação de primeiro acesso ao vivo na banca.

Idempotente: rodar de novo apenas re-arma a flag e a senha temporária.
Uso:  NEXO_DB_DIRECT=1 python criar_cliente_demo.py
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select

from app import create_app
from extensions import db
from models import Usuario, Empresa

EMAIL = "primeiroacesso@nexo.com"
SENHA_TEMP = "nexo12345"          # senha temporária — será trocada no 1º acesso
NOME = "Cliente Demo · Primeiro Acesso"

app = create_app("development")
with app.app_context():
    empresa = db.session.execute(
        select(Empresa).order_by(Empresa.id_empresa)
    ).scalars().first()
    if empresa is None:
        raise SystemExit("Nenhuma empresa cadastrada — crie uma empresa antes de rodar este script.")

    usuario = db.session.execute(
        select(Usuario).where(Usuario.email == EMAIL)
    ).scalar_one_or_none()

    if usuario is None:
        usuario = Usuario(
            nome=NOME, email=EMAIL, role="CLIENTE",
            id_empresa=empresa.id_empresa, ativo=True,
        )
        usuario.set_senha(SENHA_TEMP)
        usuario.primeiro_acesso = True
        db.session.add(usuario)
        acao = "criado"
    else:
        usuario.role = "CLIENTE"
        usuario.ativo = True
        if usuario.id_empresa is None:
            usuario.id_empresa = empresa.id_empresa
        usuario.set_senha(SENHA_TEMP)
        usuario.primeiro_acesso = True   # re-arma a flag para a demo
        acao = "atualizado (flag re-armada)"

    db.session.commit()
    print("=" * 56)
    print(f"Cliente demo {acao}.")
    print(f"  E-mail ........: {EMAIL}")
    print(f"  Senha temporária: {SENHA_TEMP}")
    print(f"  Empresa .......: {empresa.razao_social} (id {empresa.id_empresa})")
    print(f"  primeiro_acesso: {usuario.primeiro_acesso}")
    print("=" * 56)
    print("Faça login com ele para ver a interceptação de primeiro acesso ao vivo.")
