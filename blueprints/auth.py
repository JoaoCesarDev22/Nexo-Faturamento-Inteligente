from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import select

from extensions import db
from models import Usuario

# Criação do Blueprint de Autenticação
auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Gere o ecrã e a lógica de login do utilizador."""
    # Se já estiver autenticado, redireciona para a página inicial (que faz o devido redirecionamento)
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        senha = request.form.get("senha", "")

        # Procura o utilizador no banco de dados pelo e-mail
        usuario = db.session.execute(
            select(Usuario).where(Usuario.email == email)
        ).scalar_one_or_none()

        # Valida o utilizador e a senha (usando o método check_senha definido no Model)
        if usuario and usuario.check_senha(senha):
            if not usuario.ativo:
                flash("Esta conta está desativada. Contacte o administrador.", "danger")
                return render_template("auth/login.html")

            # Efetua o login na sessão
            login_user(usuario)
            
            # Trata o redirecionamento caso o utilizador tenha tentado aceder a uma página protegida antes
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)

            # Redirecionamento padrão com base no perfil (Role)
            if usuario.is_admin:
                return redirect(url_for("admin.dashboard"))
            return redirect(url_for("cliente.dashboard"))
        
        # Mensagem genérica por motivos de segurança (não revelar se o erro foi o e-mail ou a senha)
        flash("E-mail ou senha incorretos.", "danger")

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    """Termina a sessão do utilizador."""
    logout_user()
    flash("Sessão terminada com sucesso.", "success")
    return redirect(url_for("auth.login"))