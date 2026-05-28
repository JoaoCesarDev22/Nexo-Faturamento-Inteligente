"""
NEXO - Blueprint de autenticação
=================================
Rotas: /auth/login, /auth/logout

Estratégia de senha: werkzeug.security (PBKDF2 com SHA-256 e salt).
Padrão do Flask, suficiente para o MVP. Alternativa: bcrypt/argon2
(mais lento, mais resistente) em produção real pós-PI3.
"""

from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import select

from extensions import db
from models import Usuario

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # Se já está logado, manda direto pra raiz que decide o destino.
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")

        if not email or not senha:
            flash("Informe e-mail e senha.", "warning")
            return render_template("auth/login.html"), 400

        usuario = db.session.execute(
            select(Usuario).where(Usuario.email == email)
        ).scalar_one_or_none()

        # Mensagem genérica para não vazar se o e-mail existe ou não.
        if not usuario or not usuario.check_senha(senha):
            flash("E-mail ou senha inválidos.", "danger")
            return render_template("auth/login.html"), 401

        if not usuario.ativo:
            flash("Conta desativada. Contate o administrador.", "warning")
            return render_template("auth/login.html"), 403

        login_user(usuario)
        usuario.ultimo_acesso = datetime.utcnow()
        db.session.commit()

        flash(f"Bem-vindo, {usuario.nome}!", "success")
        return redirect(url_for("index"))

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Você saiu da sua conta.", "info")
    return redirect(url_for("auth.login"))
