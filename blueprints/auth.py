import logging
from datetime import datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadData
from sqlalchemy import select

from extensions import db
from models import Usuario
from emails import email_recuperacao_senha

logger = logging.getLogger(__name__)

# Criação do Blueprint de Autenticação
auth_bp = Blueprint("auth", __name__)

# --- Recuperação de senha (itsdangerous) ---
_SALT_RESET = "nexo-reset-senha-v1"   # namespaceia o token; trocar invalida tokens antigos
RESET_MAX_AGE = 900                   # 15 minutos, em segundos


def _serializer() -> URLSafeTimedSerializer:
    """Serializer assinado com a SECRET_KEY da app (token criptografado/assinado)."""
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_SALT_RESET)


def _ler_token_reset(token: str):
    """Devolve o e-mail embutido no token se válido e dentro de 15 min; senão None."""
    try:
        return _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (SignatureExpired, BadData):
        return None


def _validar_nova_senha(nova: str, confirma: str):
    """Regras mínimas de senha. Retorna mensagem de erro ou None se OK."""
    if len(nova) < 8:
        return "A senha deve ter ao menos 8 caracteres."
    if nova != confirma:
        return "As senhas não coincidem."
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Gere o ecrã e a lógica de login do utilizador."""
    # Se já estiver autenticado, vai DIRETO ao dashboard do perfil correto.
    # (Antes redirecionava para "index", mas "index" agora serve a landing
    # institucional — voltar pra lá seria UX hostil pra quem já está logado.)
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("cliente.dashboard"))

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

            # Efetua o login na sessão (sem "remember": cookie morre ao fechar o
            # navegador). Carimba a sessão com o BOOT_ID atual para que um
            # restart do servidor invalide o login automaticamente.
            login_user(usuario, remember=False)
            session["boot_id"] = current_app.config["BOOT_ID"]

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


@auth_bp.route("/primeiro-acesso", methods=["GET", "POST"])
@login_required
def primeiro_acesso():
    """
    Troca obrigatória de senha no primeiro acesso do cliente.
    O guard em app.py manda o cliente para cá enquanto primeiro_acesso=True.
    Ao salvar, baixa a flag e libera o portal.
    """
    # Já trocou (ou é admin): não há o que fazer aqui.
    if not getattr(current_user, "primeiro_acesso", False):
        destino = "admin.dashboard" if current_user.is_admin else "cliente.dashboard"
        return redirect(url_for(destino))

    if request.method == "POST":
        nova = request.form.get("nova_senha", "")
        confirma = request.form.get("confirma_senha", "")
        erro = _validar_nova_senha(nova, confirma)
        if erro:
            flash(erro, "danger")
            return render_template("auth/primeiro_acesso.html")

        current_user.set_senha(nova)
        current_user.primeiro_acesso = False
        current_user.data_atualizacao = datetime.now(timezone.utc)
        db.session.commit()
        flash("Senha definida com sucesso. Bem-vindo(a) ao NEXO!", "success")
        return redirect(url_for("cliente.dashboard"))

    return render_template("auth/primeiro_acesso.html")


@auth_bp.route("/recuperar-senha", methods=["GET", "POST"])
def recuperar_senha():
    """
    Solicita o link de redefinição. Por segurança, a resposta é SEMPRE genérica
    (não revela se o e-mail existe). Se existir, gera token assinado (15 min) e
    envia por e-mail; sem SMTP configurado, entra em modo simulação (exibe/loga o link).
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        usuario = db.session.execute(
            select(Usuario).where(Usuario.email == email)
        ).scalar_one_or_none()

        if usuario and usuario.ativo:
            token = _serializer().dumps(usuario.email)
            link = url_for("auth.resetar_senha", token=token, _external=True)
            logger.info("Link de recuperação para %s: %s", usuario.email, link)
            enviado = email_recuperacao_senha(usuario, link)
            if not enviado and current_app.debug:
                # Modo simulação (SMTP não configurado) — facilita o teste local.
                flash(f"[Modo simulação · SMTP não configurado] Link de redefinição: {link}", "info")

        # Mensagem genérica sempre (anti-enumeração de e-mails).
        flash("Se o e-mail estiver cadastrado, enviamos um link de redefinição válido por 15 minutos.", "success")
        return redirect(url_for("auth.recuperar_senha"))

    return render_template("auth/recuperar_senha.html")


@auth_bp.route("/resetar-senha/<token>", methods=["GET", "POST"])
def resetar_senha(token):
    """
    Valida o token (assinatura + expiração de 15 min). Token adulterado ou
    expirado → 'Token inválido ou expirado'. Válido → permite definir nova senha.
    """
    email = _ler_token_reset(token)
    if email is None:
        flash("Token inválido ou expirado. Solicite um novo link de redefinição.", "danger")
        return redirect(url_for("auth.recuperar_senha"))

    usuario = db.session.execute(
        select(Usuario).where(Usuario.email == email)
    ).scalar_one_or_none()
    if usuario is None or not usuario.ativo:
        flash("Token inválido ou expirado. Solicite um novo link de redefinição.", "danger")
        return redirect(url_for("auth.recuperar_senha"))

    if request.method == "POST":
        nova = request.form.get("nova_senha", "")
        confirma = request.form.get("confirma_senha", "")
        erro = _validar_nova_senha(nova, confirma)
        if erro:
            flash(erro, "danger")
            return render_template("auth/resetar_senha.html", token=token)

        usuario.set_senha(nova)
        usuario.primeiro_acesso = False  # quem redefine a senha já está "ativado"
        usuario.data_atualizacao = datetime.now(timezone.utc)
        db.session.commit()
        flash("Senha redefinida com sucesso. Faça login com a nova senha.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/resetar_senha.html", token=token)