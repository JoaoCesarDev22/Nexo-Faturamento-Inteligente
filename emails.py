"""
NEXO - Faturamento Inteligente | E-mails transacionais (Flask-Mail / SMTP)
==========================================================================
Envio de e-mails HTML disparados por eventos de negócio (publicação de análise,
resolução de chamado). Princípios:

  - FAIL-SAFE: se MAIL_USERNAME/PASSWORD não estiverem configurados (MAIL_ATIVO
    False), o envio é SUPRIMIDO (loga e segue) — nada quebra em dev/demo.
  - NÃO-BLOQUEANTE: o envio roda numa thread com app_context próprio, para não
    travar a request enquanto o SMTP faz handshake.
  - Qualquer falha de SMTP é capturada e logada — jamais derruba o fluxo que
    disparou o e-mail (publicar análise, resolver ticket).
"""

import logging
import threading

from flask import current_app, render_template
from flask_mail import Message

from extensions import mail

logger = logging.getLogger(__name__)


def _enviar_async(app, msg: Message) -> None:
    with app.app_context():
        try:
            mail.send(msg)
            logger.info("E-mail enviado: '%s' -> %s", msg.subject, msg.recipients)
        except Exception as e:  # noqa: BLE001
            logger.warning("Falha ao enviar e-mail '%s': %s", msg.subject, e)


def enviar_email(destinatarios, assunto: str, template: str, **ctx) -> bool:
    """
    Renderiza templates/emails/<template> e dispara em background.
    Retorna True se o envio foi agendado; False se suprimido/sem destinatário.
    """
    if isinstance(destinatarios, str):
        destinatarios = [destinatarios]
    destinatarios = [d for d in (destinatarios or []) if d]
    if not destinatarios:
        return False

    if not current_app.config.get("MAIL_ATIVO"):
        logger.info(
            "MAIL inativo (sem credenciais) — e-mail '%s' suprimido. Destino: %s",
            assunto, destinatarios,
        )
        return False

    html = render_template(f"emails/{template}", **ctx)
    msg = Message(subject=assunto, recipients=destinatarios, html=html)
    app = current_app._get_current_object()
    threading.Thread(target=_enviar_async, args=(app, msg), daemon=True).start()
    return True


def _emails_da_empresa(empresa) -> list[str]:
    """Coleta e-mails de contato da empresa + dos usuários CLIENTE ativos."""
    emails = set()
    if getattr(empresa, "email_contato", None):
        emails.add(empresa.email_contato)
    for u in getattr(empresa, "usuarios", []) or []:
        if u.role == "CLIENTE" and u.ativo and u.email:
            emails.add(u.email)
    return list(emails)


# ---------------------------------------------------------------------
# Gatilhos de alto nível (chamados nos blueprints)
# ---------------------------------------------------------------------
def email_analise_publicada(analise, link_url: str) -> bool:
    """E-mail ao cliente quando a devolutiva estratégica é publicada."""
    ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
    return enviar_email(
        _emails_da_empresa(analise.empresa),
        f"📊 Sua Análise Executiva NEXO de {ref} está disponível",
        "analise_publicada.html",
        analise=analise, ref=ref, link_url=link_url,
    )


def email_ticket_resolvido(chamado, link_url: str) -> bool:
    """E-mail ao cliente quando um chamado é marcado como RESOLVIDO."""
    return enviar_email(
        _emails_da_empresa(chamado.empresa),
        f"✅ Chamado resolvido: “{chamado.assunto}”",
        "ticket_resolvido.html",
        chamado=chamado, link_url=link_url,
    )
