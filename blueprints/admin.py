import os
import hashlib
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Blueprint, render_template, redirect, url_for, flash, current_app, request, abort
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import select, func

from extensions import db
from models import (
    Analise, UploadRelatorio, Empresa, Plano, Segmento, Usuario, RelatorioAnalise,
    ChamadoSuporte, MensagemSuporte, GuiaTopico, ProdutoCurvaABC, Notificacao,
)
from etl_processor import processar_arquivos_analise
from notifications import notificar_clientes_empresa
from emails import email_analise_publicada, email_ticket_resolvido, email_boas_vindas
from sqlalchemy.exc import IntegrityError
import storage

# Categorias de ticket (token persistido -> rótulo exibido) — espelha cliente.py.
CATEGORIAS_TICKET = {
    "FINANCEIRO": "Financeiro",
    "DUVIDA_TECNICA": "Dúvida Técnica",
    "ERRO_INTEGRACAO": "Erro de Integração",
}
STATUS_CHAMADO = ("ABERTO", "EM_ATENDIMENTO", "RESOLVIDO")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(f):
    """Decorator: bloqueia rota para qualquer não-ADMIN (espelha cliente_required)."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


@admin_bp.route("/dashboard")
@admin_required
def dashboard():
    # --- Contagem de empresas por status_conta (ATIVA/SUSPENSA/CANCELADA) ---
    contagem_status = dict(
        db.session.execute(
            select(Empresa.status_conta, func.count(Empresa.id_empresa))
            .where(Empresa.deletado_em.is_(None))
            .group_by(Empresa.status_conta)
        ).all()
    )
    ativas = int(contagem_status.get("ATIVA", 0))
    suspensas = int(contagem_status.get("SUSPENSA", 0))
    canceladas = int(contagem_status.get("CANCELADA", 0))
    total_emp = ativas + suspensas + canceladas

    # --- Receita mensal estimada: soma dos planos atuais das empresas ATIVAS ---
    receita = db.session.execute(
        select(func.coalesce(func.sum(Plano.valor_mensal), 0))
        .select_from(Empresa)
        .join(Plano, Empresa.id_plano_atual == Plano.id_plano)
        .where(Empresa.status_conta == "ATIVA")
        .where(Empresa.deletado_em.is_(None))
    ).scalar() or 0

    stats = {
        "total": total_emp,
        "ativas": ativas,
        "suspensas": suspensas,
        "canceladas": canceladas,
        "receita_estimada": float(receita),
    }

    # --- Empresas por plano (para o gráfico) ---
    contagem_plano = dict(
        db.session.execute(
            select(Plano.nome_plano, func.count(Empresa.id_empresa))
            .select_from(Empresa)
            .join(Plano, Empresa.id_plano_atual == Plano.id_plano)
            .where(Empresa.deletado_em.is_(None))
            .group_by(Plano.nome_plano)
        ).all()
    )

    # --- Análises por status (para o gráfico) ---
    contagem_analises = dict(
        db.session.execute(
            select(Analise.status_analise, func.count(Analise.id_analise))
            .group_by(Analise.status_analise)
        ).all()
    )

    dashboard_data = {
        "empresas_status": {
            "labels": ["Ativas", "Suspensas", "Canceladas"],
            "values": [ativas, suspensas, canceladas],
        },
        "empresas_plano": {
            "labels": ["BRONZE", "PRATA", "OURO"],
            "values": [int(contagem_plano.get(k, 0)) for k in ("BRONZE", "PRATA", "OURO")],
        },
        "analises_status": {
            "labels": ["Aguardando relatório", "Relatório recebido", "Em análise", "Concluído"],
            "values": [
                int(contagem_analises.get(k, 0))
                for k in ("AGUARDANDO_RELATORIO", "RELATORIO_RECEBIDO", "EM_ANALISE", "CONCLUIDO")
            ],
        },
    }

    # --- Últimas análises (com empresa carregada para evitar N+1 no template) ---
    analises = db.session.execute(
        select(Analise)
        .order_by(Analise.data_criacao.desc(), Analise.id_analise.desc())
        .limit(6)
    ).scalars().all()

    # --- Fila de homologação: análises com anexos enviados pelo CLIENTE
    #     (id_usuario_admin IS NULL) ainda PENDENTES de validação/ETL. ---
    ids_pendentes = db.session.execute(
        select(UploadRelatorio.id_analise)
        .where(UploadRelatorio.id_usuario_admin.is_(None))
        .where(UploadRelatorio.status_processamento == "PENDENTE")
        .group_by(UploadRelatorio.id_analise)
    ).scalars().all()

    homologacoes = []
    for id_a in ids_pendentes:
        analise_p = db.session.get(Analise, id_a)
        if analise_p is None:
            continue
        up_v = _buscar_upload(id_a, "VENDAS")
        up_c = _buscar_upload(id_a, "COMPRAS")
        homologacoes.append({
            "analise": analise_p,
            "tem_vendas": up_v is not None,
            "tem_compras": up_c is not None,
            "pronto": up_v is not None and up_c is not None,
        })

    return render_template(
        "admin/dashboard.html",
        stats=stats,
        analises=analises,
        dashboard_data=dashboard_data,
        homologacoes=homologacoes,
    )


@admin_bp.route("/empresas")
@admin_required
def empresas():
    """Listagem da carteira de empresas clientes ATIVAS (exclui a lixeira)."""
    rows = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_(None))
        .order_by(Empresa.razao_social.asc())
    ).scalars().all()
    # Contagem para o badge do link "Lixeira" na própria tela.
    na_lixeira = db.session.execute(
        select(func.count(Empresa.id_empresa)).where(Empresa.deletado_em.is_not(None))
    ).scalar() or 0
    return render_template("admin/empresas.html", empresas=rows, na_lixeira=na_lixeira)


@admin_bp.route("/analises")
@admin_required
def analises():
    """Listagem global de análises (todas as empresas, ordenadas por data_criacao desc)."""
    # Tiebreak por id_analise para ordem determinística mesmo quando
    # várias análises foram criadas no mesmo segundo (resolução de CURRENT_TIMESTAMP).
    rows = db.session.execute(
        select(Analise).order_by(Analise.data_criacao.desc(), Analise.id_analise.desc())
    ).scalars().all()
    empresas_filtro = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_(None))
        .order_by(Empresa.razao_social.asc())
    ).scalars().all()
    return render_template("admin/analises.html", analises=rows, empresas=empresas_filtro)


@admin_bp.route("/analises/nova", methods=["GET", "POST"])
@admin_required
def analise_nova():
    """
    Abertura de nova análise. Regras de negócio:
      - Apenas empresas ATIVAS podem receber novas análises.
      - QUINZENAL exige plano OURO + quinzena_referencia em {1, 2}.
      - MENSAL exige quinzena_referencia = NULL.
      - id_plano_referencia é congelado a partir do plano atual da empresa
        (snapshot histórico — futuras trocas de plano não afetam esta análise).
      - status_analise inicia em AGUARDANDO_RELATORIO (default do schema).
    """
    empresas = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_(None))
        .order_by(Empresa.razao_social.asc())
    ).scalars().all()

    if request.method == "POST":
        form = request.form

        try:
            id_empresa = int(form["id_empresa"])
        except (KeyError, ValueError):
            flash("Selecione uma empresa válida.", "danger")
            return redirect(url_for("admin.analise_nova"))

        empresa = db.session.get(Empresa, id_empresa)
        if empresa is None:
            flash("Empresa não encontrada.", "danger")
            return redirect(url_for("admin.analise_nova"))
        if empresa.status_conta != "ATIVA":
            flash(f"Empresa em status {empresa.status_conta} — novas análises bloqueadas.", "danger")
            return redirect(url_for("admin.analise_nova"))

        tipo_analise = form.get("tipo_analise", "").strip().upper()
        if tipo_analise not in ("MENSAL", "QUINZENAL"):
            flash("Tipo de análise inválido.", "danger")
            return redirect(url_for("admin.analise_nova"))

        quinzena_ref = None
        if tipo_analise == "QUINZENAL":
            plano_nome = empresa.plano_atual.nome_plano if empresa.plano_atual else None
            if plano_nome != "OURO":
                flash("Análises QUINZENAIS estão disponíveis apenas para empresas do plano OURO.", "danger")
                return redirect(url_for("admin.analise_nova"))
            try:
                quinzena_ref = int(form.get("quinzena_referencia", ""))
            except ValueError:
                flash("Quinzena de referência inválida.", "danger")
                return redirect(url_for("admin.analise_nova"))
            if quinzena_ref not in (1, 2):
                flash("Quinzena de referência deve ser 1 ou 2.", "danger")
                return redirect(url_for("admin.analise_nova"))

        try:
            periodo_inicio = datetime.strptime(form["periodo_inicio"].strip(), "%Y-%m-%d").date()
            periodo_fim = datetime.strptime(form["periodo_fim"].strip(), "%Y-%m-%d").date()
        except (KeyError, ValueError):
            flash("Datas do período inválidas (use o seletor de data).", "danger")
            return redirect(url_for("admin.analise_nova"))
        if periodo_inicio > periodo_fim:
            flash("Período inválido: início é posterior ao fim.", "danger")
            return redirect(url_for("admin.analise_nova"))

        try:
            mes_ref = int(form["mes_referencia"])
            ano_ref = int(form["ano_referencia"])
        except (KeyError, ValueError):
            flash("Mês/ano de referência inválidos.", "danger")
            return redirect(url_for("admin.analise_nova"))
        if not (1 <= mes_ref <= 12):
            flash("Mês de referência deve estar entre 1 e 12.", "danger")
            return redirect(url_for("admin.analise_nova"))

        nova = Analise(
            id_empresa=id_empresa,
            id_plano_referencia=empresa.id_plano_atual,  # snapshot do plano atual
            id_usuario_admin_responsavel=current_user.id_usuario,
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
            mes_referencia=mes_ref,
            ano_referencia=ano_ref,
            tipo_analise=tipo_analise,
            quinzena_referencia=quinzena_ref,
            # status_analise: default 'AGUARDANDO_RELATORIO' (DB)
        )
        try:
            db.session.add(nova)
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.warning("Falha ao criar análise: %s", e.orig if e.orig else e)
            flash("Não foi possível criar a análise (dados inválidos ou duplicados). Revise e tente novamente.", "danger")
            return redirect(url_for("admin.analise_nova"))

        flash(
            f"Análise #{nova.id_analise} criada para {empresa.nome_fantasia or empresa.razao_social}.",
            "success",
        )
        return redirect(url_for("admin.analise_upload", id_analise=nova.id_analise))

    return render_template("admin/analise_form.html", empresas=empresas)


def _planos_segmentos():
    """Listas auxiliares para os <select> do formulário de empresa."""
    segmentos = db.session.execute(
        select(Segmento).where(Segmento.ativo).order_by(Segmento.nome_segmento)
    ).scalars().all()
    planos = db.session.execute(
        select(Plano).where(Plano.ativo).order_by(Plano.valor_mensal)
    ).scalars().all()
    return planos, segmentos


def _parse_decimal_opcional(valor: str):
    """Aceita string vazia / pt-BR / en-US para faturamento_base_mensal (opcional)."""
    if not valor or not valor.strip():
        return None
    s = valor.strip().replace(".", "").replace(",", ".") if "," in valor else valor.strip()
    try:
        return float(s)
    except ValueError:
        return None


@admin_bp.route("/empresas/nova", methods=["GET", "POST"])
@admin_required
def empresa_nova():
    """Cadastro de empresa cliente + usuário CLIENTE inicial vinculado."""
    planos, segmentos = _planos_segmentos()

    if request.method == "POST":
        form = request.form
        try:
            data_contratacao = datetime.strptime(form.get("data_contratacao", "").strip(), "%Y-%m-%d").date()
        except ValueError:
            flash("Data de contratação inválida (use o seletor de data).", "danger")
            return redirect(url_for("admin.empresa_nova"))

        nova_emp = Empresa(
            id_segmento=int(form["id_segmento"]),
            id_plano_atual=int(form["id_plano_atual"]),
            cnpj=form["cnpj"].strip(),
            razao_social=form["razao_social"].strip(),
            nome_fantasia=(form.get("nome_fantasia") or "").strip() or None,
            email_contato=form["email_contato"].strip(),
            telefone_contato=(form.get("telefone_contato") or "").strip() or None,
            data_contratacao=data_contratacao,
            faturamento_base_mensal=_parse_decimal_opcional(form.get("faturamento_base_mensal", "")),
            status_conta=form.get("status_conta", "ATIVA"),
        )
        novo_user = Usuario(
            nome=form["usuario_nome"].strip(),
            email=form["usuario_email"].strip(),
            role="CLIENTE",
            ativo=True,
        )
        # Sem senha provisória: o cliente DEFINE a senha pelo link de ativação.
        # Geramos um hash aleatório e inutilizável (senha_hash é NOT NULL) e
        # mantemos primeiro_acesso=True (default) — ninguém loga até ativar.
        novo_user.set_senha(secrets.token_urlsafe(32))
        novo_user.primeiro_acesso = True

        try:
            db.session.add(nova_emp)
            db.session.flush()
            novo_user.id_empresa = nova_emp.id_empresa
            db.session.add(novo_user)
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.warning("Falha ao cadastrar empresa/usuário: %s", e.orig if e.orig else e)
            flash("Não foi possível salvar: CNPJ ou e-mail já cadastrados (ou dados inválidos).", "danger")
            return redirect(url_for("admin.empresa_nova"))

        # Insert confirmado → e-mail de boas-vindas com LINK SEGURO de definição
        # de senha (token de 15 min), DIRETO pelo Flask (sem webhook externo).
        # Envio assíncrono e fail-safe; nunca derruba o cadastro.
        link_definir = None
        try:
            from blueprints.auth import gerar_token_definir_senha
            token = gerar_token_definir_senha(novo_user.email)
            link_definir = url_for("auth.ativar_acesso", token=token, _external=True)
            current_app.logger.info("Link de ativação para %s: %s", novo_user.email, link_definir)
            email_boas_vindas(novo_user, link_definir)
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("Falha ao agendar e-mail de boas-vindas: %s", e)

        # Sem SMTP configurado (dev): mostra o link ao admin para teste/repasse manual.
        if link_definir and not current_app.config.get("MAIL_ATIVO") and current_app.debug:
            flash(f"[Modo simulação · SMTP não configurado] Link de ativação do cliente: {link_definir}", "info")
        flash(f"Empresa '{nova_emp.razao_social}' cadastrada. Enviamos ao cliente um link para definir a senha.", "success")
        return redirect(url_for("admin.empresas"))

    return render_template("admin/empresa_form.html", empresa=None, planos=planos, segmentos=segmentos)


@admin_bp.route("/empresas/<int:id_empresa>/editar", methods=["GET", "POST"])
@admin_required
def empresa_editar(id_empresa):
    """Atualização de dados cadastrais da empresa (CNPJ é imutável pelo formulário)."""
    empresa = db.session.get(Empresa, id_empresa)
    if not empresa:
        flash("Empresa não encontrada.", "danger")
        return redirect(url_for("admin.empresas"))
    planos, segmentos = _planos_segmentos()

    if request.method == "POST":
        form = request.form
        try:
            data_contratacao = datetime.strptime(form.get("data_contratacao", "").strip(), "%Y-%m-%d").date()
        except ValueError:
            flash("Data de contratação inválida.", "danger")
            return redirect(url_for("admin.empresa_editar", id_empresa=id_empresa))

        # CNPJ é readonly no formulário; ignoramos qualquer valor vindo dele.
        empresa.id_segmento = int(form["id_segmento"])
        empresa.id_plano_atual = int(form["id_plano_atual"])
        empresa.razao_social = form["razao_social"].strip()
        empresa.nome_fantasia = (form.get("nome_fantasia") or "").strip() or None
        empresa.email_contato = form["email_contato"].strip()
        empresa.telefone_contato = (form.get("telefone_contato") or "").strip() or None
        empresa.data_contratacao = data_contratacao
        empresa.faturamento_base_mensal = _parse_decimal_opcional(form.get("faturamento_base_mensal", ""))
        empresa.status_conta = form.get("status_conta", "ATIVA")
        empresa.data_atualizacao = datetime.now(timezone.utc)

        try:
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.warning("Falha ao editar empresa %s: %s", id_empresa, e.orig if e.orig else e)
            flash("Não foi possível salvar as alterações (dados inválidos ou duplicados).", "danger")
            return redirect(url_for("admin.empresa_editar", id_empresa=id_empresa))

        flash("Empresa atualizada com sucesso.", "success")
        return redirect(url_for("admin.empresas"))

    return render_template("admin/empresa_form.html", empresa=empresa, planos=planos, segmentos=segmentos)


@admin_bp.route("/empresa/excluir/<int:id_empresa>", methods=["POST"])
@admin_required
def empresa_excluir(id_empresa):
    """
    SOFT DELETE — move a empresa para a Lixeira (estilo Google Drive/Windows).

    Não apaga nada: apenas carimba `deletado_em` com o instante atual, o que a
    remove das listagens normais (que filtram deletado_em IS NULL). Os usuários
    CLIENTE vinculados são desativados (ativo=False) para impedir login enquanto
    a empresa estiver na lixeira — a flag é restaurada em /empresa/restaurar.
    Reversível e não-destrutivo; a exclusão física só ocorre em
    /empresa/excluir-permanente. POST-only + confirmação no modal.
    """
    empresa = db.session.get(Empresa, id_empresa)
    if not empresa or empresa.deletado_em is not None:
        flash("Empresa não encontrada (ou já está na lixeira).", "danger")
        return redirect(url_for("admin.empresas"))

    nome_emp = empresa.nome_fantasia or empresa.razao_social
    agora = datetime.now(timezone.utc)
    empresa.deletado_em = agora
    empresa.data_atualizacao = agora
    # Bloqueia o acesso do cliente enquanto estiver na lixeira.
    for u in empresa.usuarios:
        u.ativo = False

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.warning("Falha ao mover empresa %s para a lixeira: %s", id_empresa, e.orig if e.orig else e)
        flash(f"Não foi possível mover '{nome_emp}' para a lixeira. Tente novamente.", "danger")
        return redirect(url_for("admin.empresas"))

    flash(f"Cliente '{nome_emp}' movido para a lixeira com sucesso.", "success")
    return redirect(url_for("admin.empresas"))


@admin_bp.route("/lixeira")
@admin_required
def lixeira():
    """Empresas que estão na lixeira (soft-deleted): deletado_em IS NOT NULL."""
    rows = db.session.execute(
        select(Empresa)
        .where(Empresa.deletado_em.is_not(None))
        .order_by(Empresa.deletado_em.desc())
    ).scalars().all()
    return render_template("admin/lixeira.html", empresas=rows)


@admin_bp.route("/empresa/restaurar/<int:id_empresa>", methods=["POST"])
@admin_required
def empresa_restaurar(id_empresa):
    """
    Restaura uma empresa da lixeira: limpa `deletado_em` (volta às listagens) e
    reativa os usuários CLIENTE vinculados. Traz de volta todo o histórico
    (análises, relatórios, chamados) intacto — nada havia sido apagado.
    """
    empresa = db.session.get(Empresa, id_empresa)
    if not empresa or empresa.deletado_em is None:
        flash("Empresa não encontrada na lixeira.", "danger")
        return redirect(url_for("admin.lixeira"))

    nome_emp = empresa.nome_fantasia or empresa.razao_social
    empresa.deletado_em = None
    empresa.data_atualizacao = datetime.now(timezone.utc)
    for u in empresa.usuarios:
        u.ativo = True

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.warning("Falha ao restaurar empresa %s: %s", id_empresa, e.orig if e.orig else e)
        flash(f"Não foi possível restaurar '{nome_emp}'. Tente novamente.", "danger")
        return redirect(url_for("admin.lixeira"))

    flash(f"Cliente '{nome_emp}' restaurado com sucesso.", "success")
    return redirect(url_for("admin.lixeira"))


@admin_bp.route("/empresa/excluir-permanente/<int:id_empresa>", methods=["POST"])
@admin_required
def empresa_excluir_permanente(id_empresa):
    """
    HARD DELETE — exclusão física DEFINITIVA e IRREVERSÍVEL.

    Só age sobre empresas que JÁ estão na lixeira (deletado_em IS NOT NULL),
    garantindo o duplo passo (soft → hard). Como os relationships não têm
    cascade (controle explícito do que sai do banco), removemos os filhos em
    ordem topológica, tudo na MESMA transação (segurança transacional):

      1) Por análise: ProdutoCurvaABC, UploadRelatorio (+arquivos físicos),
         IndicadorAnalise (1:1), RelatorioAnalise (1:1) → depois a Analise;
      2) Por chamado: MensagemSuporte → depois o ChamadoSuporte;
      3) Notificacao dos usuários da empresa;
      4) Usuario(s) da empresa;
      5) a própria Empresa.

    Qualquer falha de integridade → rollback atômico (nada é apagado) + aviso.
    """
    empresa = db.session.get(Empresa, id_empresa)
    if not empresa or empresa.deletado_em is None:
        flash("A exclusão permanente só é possível para empresas que estão na lixeira.", "danger")
        return redirect(url_for("admin.lixeira"))

    nome_emp = empresa.nome_fantasia or empresa.razao_social

    try:
        # 1) Análises e tudo que depende delas.
        analises = db.session.execute(
            select(Analise).where(Analise.id_empresa == id_empresa)
        ).scalars().all()
        for analise in analises:
            db.session.execute(
                ProdutoCurvaABC.__table__.delete().where(
                    ProdutoCurvaABC.id_analise == analise.id_analise
                )
            )
            uploads = db.session.execute(
                select(UploadRelatorio).where(UploadRelatorio.id_analise == analise.id_analise)
            ).scalars().all()
            for up in uploads:
                storage.remover(up.caminho_arquivo)  # best-effort (ver storage.py)
                db.session.delete(up)
            if analise.indicador is not None:
                db.session.delete(analise.indicador)
            if analise.relatorio is not None:
                db.session.delete(analise.relatorio)
            db.session.delete(analise)

        # 2) Chamados de suporte e suas mensagens.
        chamados = db.session.execute(
            select(ChamadoSuporte).where(ChamadoSuporte.id_empresa == id_empresa)
        ).scalars().all()
        for ch in chamados:
            db.session.execute(
                MensagemSuporte.__table__.delete().where(
                    MensagemSuporte.id_chamado == ch.id_chamado
                )
            )
            db.session.delete(ch)

        # 3/4) Notificações dos usuários da empresa + os próprios usuários.
        ids_usuarios = [u.id_usuario for u in empresa.usuarios]
        if ids_usuarios:
            db.session.execute(
                Notificacao.__table__.delete().where(
                    Notificacao.id_usuario.in_(ids_usuarios)
                )
            )
        for u in list(empresa.usuarios):
            db.session.delete(u)

        # 5) A própria empresa.
        db.session.delete(empresa)

        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        flash(
            f"Não foi possível excluir permanentemente '{nome_emp}': ainda há dados vinculados.",
            "danger",
        )
        current_app.logger.warning("Falha no hard delete da empresa %s: %s", id_empresa, e.orig if e.orig else e)
        return redirect(url_for("admin.lixeira"))

    flash(f"Cliente '{nome_emp}' e todos os seus dados foram excluídos permanentemente.", "success")
    return redirect(url_for("admin.lixeira"))


def _buscar_upload(id_analise: int, tipo: str):
    """Consulta o upload diretamente no banco (não confia em coleção carregada em memória)."""
    return db.session.execute(
        select(UploadRelatorio).where(
            UploadRelatorio.id_analise == id_analise,
            UploadRelatorio.tipo_relatorio == tipo,
        )
    ).scalar_one_or_none()


def _resolver_caminho(upload: UploadRelatorio) -> str:
    """Caminho absoluto do arquivo salvo. Delega à camada de storage (ponto
    único para futura migração a object storage — ver storage.py)."""
    return storage.resolver(upload.caminho_arquivo)


# GET exibe a página de upload; POST recebe o arquivo. As submissões são
# protegidas por CSRF (token no form) e restritas a ADMIN (@admin_required).
@admin_bp.route("/analise/<int:id_analise>/upload", methods=["GET", "POST"])
@admin_required
def analise_upload(id_analise):
    """
    GET  → exibe a página de upload + status dos uploads + KPIs (se existirem).
    POST → salva um arquivo (VENDAS ou COMPRAS):
        - valida extensão (config.ALLOWED_EXTENSIONS);
        - calcula SHA-256 e tamanho;
        - substitui o upload anterior do mesmo tipo (UNIQUE id_analise+tipo_relatorio);
        - cria UploadRelatorio status_processamento='PENDENTE';
        - transição AGUARDANDO_RELATORIO → RELATORIO_RECEBIDO se os 2 uploads existirem.
    Nunca dispara o ETL aqui — isso é responsabilidade da rota /processar.
    """
    analise = db.session.get(Analise, id_analise)
    if not analise:
        flash("Análise não encontrada.", "danger")
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        tipo = (request.form.get("tipo_relatorio") or "").strip().upper()
        if tipo not in ("VENDAS", "COMPRAS"):
            flash("Tipo de relatório inválido (esperado VENDAS ou COMPRAS).", "danger")
            return redirect(url_for("admin.analise_upload", id_analise=id_analise))

        arquivo = request.files.get("arquivo")
        if not arquivo or not arquivo.filename:
            flash("Selecione um arquivo para enviar.", "warning")
            return redirect(url_for("admin.analise_upload", id_analise=id_analise))

        nome_seguro = secure_filename(arquivo.filename)
        ext = os.path.splitext(nome_seguro)[1].lower().lstrip(".")
        if ext not in current_app.config["ALLOWED_EXTENSIONS"]:
            flash(
                f"Extensão .{ext} não suportada. Aceitos: {sorted(current_app.config['ALLOWED_EXTENSIONS'])}.",
                "danger",
            )
            return redirect(url_for("admin.analise_upload", id_analise=id_analise))

        conteudo = arquivo.read()
        if not conteudo:
            flash("Arquivo vazio — nada foi salvo.", "warning")
            return redirect(url_for("admin.analise_upload", id_analise=id_analise))
        sha = hashlib.sha256(conteudo).hexdigest()

        nome_final = f"analise_{id_analise}_{tipo}_{sha[:8]}.{ext}"

        # UNIQUE(id_analise, tipo_relatorio) — re-upload substitui o anterior.
        # ATENÇÃO: só removemos o arquivo físico antigo se a referência NOVA for
        # diferente, senão um re-upload idêntico (mesmo SHA → mesmo nome) apagaria
        # o arquivo recém-gravado.
        existente = _buscar_upload(id_analise, tipo)
        # Grava o novo via camada de storage (ponto único — ver storage.py).
        caminho_final = storage.salvar(conteudo, nome_final)
        if existente and existente.caminho_arquivo and existente.caminho_arquivo != caminho_final:
            storage.remover(existente.caminho_arquivo)
        if existente:
            db.session.delete(existente)
            db.session.flush()

        db.session.add(UploadRelatorio(
            id_analise=id_analise,
            id_usuario_admin=current_user.id_usuario,
            tipo_relatorio=tipo,
            nome_arquivo_original=nome_seguro,
            caminho_arquivo=caminho_final,
            extensao_arquivo=ext.upper(),
            tamanho_arquivo=len(conteudo),
            hash_arquivo=sha,
            status_processamento="PENDENTE",
        ))

        # Transição: se com este upload os 2 tipos existem e a análise está AGUARDANDO -> RELATORIO_RECEBIDO.
        outro = _buscar_upload(id_analise, "COMPRAS" if tipo == "VENDAS" else "VENDAS")
        if outro and analise.status_analise == "AGUARDANDO_RELATORIO":
            analise.status_analise = "RELATORIO_RECEBIDO"

        db.session.commit()
        flash(f"Relatório de {tipo} enviado com sucesso.", "success")
        return redirect(url_for("admin.analise_upload", id_analise=id_analise))

    return render_template("admin/upload.html", analise=analise)


@admin_bp.route("/analise/<int:id_analise>/relatorio", methods=["GET", "POST"])
@admin_required
def analise_relatorio(id_analise):
    """
    Editor da devolutiva estratégica.

    Ações (campo `acao` do form):
      - rascunho     → upsert do relatorio_analise; mantém publicado=False; NÃO mexe no status_analise.
      - publicar     → exige análise em EM_ANALISE (pós-ETL) + todos os 6 textos; marca publicado=True
                       e dispara a ÚNICA transição válida: status_analise → CONCLUIDO + data_conclusao.
      - despublicar  → exige análise em CONCLUIDO; volta publicado=False e status_analise → EM_ANALISE.

    Esta rota é o único ponto do sistema onde CONCLUIDO pode ser aplicado (nunca o ETL).
    """
    analise = db.session.get(Analise, id_analise)
    if not analise:
        flash("Análise não encontrada.", "danger")
        return redirect(url_for("admin.analises"))

    if request.method == "POST":
        form = request.form
        acao = form.get("acao", "").strip().lower()

        # --- Geração automatizada por IA (sem digitação manual) ---
        # Preenche resumo_executivo + conclusao_estrategica a partir dos números.
        # Não depende dos campos do form e não publica — o admin revisa e publica.
        if acao == "gerar_ia":
            if not analise.indicador:
                flash("Gere os indicadores (Processar) antes de gerar a devolutiva por IA.", "warning")
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
            from ai_devolutiva import gerar_devolutiva_ia
            ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
            rel = analise.relatorio
            if rel is None:
                rel = RelatorioAnalise(
                    id_analise=id_analise, titulo=f"Análise {ref}",
                    conclusao_estrategica="(gerando...)",
                )
                db.session.add(rel)
            gerado = gerar_devolutiva_ia(analise)
            rel.resumo_executivo = gerado["resumo_executivo"]
            rel.conclusao_estrategica = gerado["conclusao_estrategica"]
            rel.gerado_por_ia = (gerado["fonte"] == "ia")
            if not rel.titulo:
                rel.titulo = f"Análise {ref}"
            analise.data_atualizacao = datetime.now(timezone.utc)
            db.session.commit()
            if gerado["fonte"] == "ia":
                flash("Devolutiva gerada por Inteligência Artificial Nexo. Revise e publique.", "success")
            else:
                flash("Rascunho gerado pelo motor local (configure ANTHROPIC_API_KEY para a geração por IA completa). Revise e publique.", "warning")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        if acao not in ("rascunho", "publicar", "despublicar"):
            flash("Ação inválida.", "danger")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        titulo = (form.get("titulo") or "").strip()
        resumo = (form.get("resumo_executivo") or "").strip() or None
        pontos_pos = (form.get("pontos_positivos") or "").strip() or None
        pontos_alerta = (form.get("pontos_de_alerta") or "").strip() or None
        recomendacoes = (form.get("recomendacoes") or "").strip() or None
        conclusao = (form.get("conclusao_estrategica") or "").strip()

        if not titulo:
            flash("Título é obrigatório.", "danger")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
        if not conclusao:
            flash("Conclusão estratégica é obrigatória (campo NOT NULL do relatório).", "danger")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        rel = analise.relatorio  # uselist=False
        if rel is None:
            rel = RelatorioAnalise(id_analise=id_analise, titulo=titulo, conclusao_estrategica=conclusao)
            db.session.add(rel)

        # Sempre atualizamos o conteúdo a partir do form (rascunho ou publicação).
        rel.titulo = titulo
        rel.resumo_executivo = resumo
        rel.pontos_positivos = pontos_pos
        rel.pontos_de_alerta = pontos_alerta
        rel.recomendacoes = recomendacoes
        rel.conclusao_estrategica = conclusao

        agora = datetime.now(timezone.utc)

        if acao == "publicar":
            # Pré-condições estritas: só publica análise que passou pelo ETL.
            if analise.status_analise not in ("EM_ANALISE", "CONCLUIDO"):
                flash(
                    "Só é possível publicar análises em EM_ANALISE ou re-publicar análises já CONCLUIDAS. "
                    f"Status atual: {analise.status_analise}.", "danger",
                )
                db.session.rollback()
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
            # O cliente vê apenas Resumo Executivo + Conclusão (os demais campos
            # viraram semáforos automáticos). Logo, publicar exige só esses dois —
            # ambos preenchíveis pela geração por IA, sem digitação manual.
            faltando = [n for n, v in [
                ("resumo executivo", resumo),
            ] if not v]
            if faltando:
                flash(f"Para publicar é necessário preencher: {', '.join(faltando)}.", "danger")
                db.session.rollback()
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

            rel.publicado = True
            rel.data_publicacao = agora
            analise.status_analise = "CONCLUIDO"
            analise.data_conclusao = agora
            # NÃO disparamos notificação/e-mail aqui: efeitos colaterais externos
            # (WebSocket + SMTP) só acontecem APÓS o commit ter sucesso, para não
            # avisar o cliente de uma publicação que pode sofrer rollback.

        elif acao == "despublicar":
            if analise.status_analise != "CONCLUIDO":
                flash(
                    "Só é possível despublicar análises em CONCLUIDO. "
                    f"Status atual: {analise.status_analise}.", "danger",
                )
                db.session.rollback()
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))
            rel.publicado = False
            rel.data_publicacao = None
            analise.status_analise = "EM_ANALISE"
            analise.data_conclusao = None

        else:  # rascunho: NÃO mexe em publicado nem em status_analise
            pass

        analise.data_atualizacao = agora

        try:
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.warning("Falha ao salvar relatório da análise %s: %s", id_analise, e.orig if e.orig else e)
            flash("Não foi possível salvar o relatório. Verifique os dados e tente novamente.", "danger")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        # Dispatch-after-commit: só agora que a transação foi confirmada disparamos
        # os efeitos colaterais externos (sininho em tempo real + e-mail ao cliente).
        if acao == "publicar":
            ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
            link_interno = url_for("cliente.analise", id_analise=analise.id_analise)
            try:
                notificar_clientes_empresa(
                    analise.id_empresa,
                    f"Seu Relatório Estratégico de {ref} já está disponível! Clique para ver.",
                    link_interno,
                )
                db.session.commit()  # persiste as notificações criadas
                email_analise_publicada(
                    analise,
                    url_for("cliente.analise", id_analise=analise.id_analise, _external=True),
                )
            except Exception as e:  # noqa: BLE001 — efeito colateral nunca derruba o fluxo
                db.session.rollback()
                current_app.logger.warning("Falha ao notificar publicação da análise %s: %s", id_analise, e)

        msg_acao = {"rascunho": "Rascunho salvo.", "publicar": "Devolutiva publicada para o cliente.",
                    "despublicar": "Devolutiva despublicada (análise voltou a EM_ANALISE)."}[acao]
        flash(msg_acao, "success")
        return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

    return render_template("admin/relatorio_editor.html", analise=analise)


@admin_bp.route("/analise/<int:id_analise>/processar", methods=["POST"])
@admin_required
def processar_analise(id_analise):
    # Lock pessimista na linha (SELECT ... FOR UPDATE no Postgres; no-op no SQLite):
    # serializa cliques/abas concorrentes na MESMA análise, evitando duas execuções
    # simultâneas do ETL gravando indicadores/Curva ABC em paralelo (race condition).
    analise = db.session.get(Analise, id_analise, with_for_update=True)
    if not analise:
        flash("Análise não encontrada.", "danger")
        return redirect(url_for("admin.dashboard"))

    # 1/2. Confirma no banco que existem AMBOS os uploads (VENDAS e COMPRAS).
    upload_vendas = _buscar_upload(id_analise, "VENDAS")
    upload_compras = _buscar_upload(id_analise, "COMPRAS")
    if not upload_vendas or not upload_compras:
        flash("Ambos os relatórios (VENDAS e COMPRAS) precisam ser enviados antes de processar.", "warning")
        return redirect(url_for("admin.dashboard"))

    # 3. Os dois uploads existem: garante RELATORIO_RECEBIDO antes de processar.
    if analise.status_analise == "AGUARDANDO_RELATORIO":
        analise.status_analise = "RELATORIO_RECEBIDO"
        db.session.commit()

    # 4. ADMIN clicou "Processar agora": entra em EM_ANALISE e dispara o ETL.
    analise.status_analise = "EM_ANALISE"
    db.session.commit()

    caminho_vendas = _resolver_caminho(upload_vendas)
    caminho_compras = _resolver_caminho(upload_compras)

    agora = datetime.now(timezone.utc)
    resultado = processar_arquivos_analise(caminho_vendas, caminho_compras, id_analise, db.session)

    if resultado.sucesso:
        # 5/6. Sucesso: marca uploads como PROCESSADO e MANTÉM EM_ANALISE.
        #      CONCLUIDO ocorre só na publicação da devolutiva estratégica.
        upload_vendas.status_processamento = "PROCESSADO"
        upload_vendas.data_processamento = agora
        upload_vendas.mensagem_erro = None
        upload_compras.status_processamento = "PROCESSADO"
        upload_compras.data_processamento = agora
        upload_compras.mensagem_erro = None
        db.session.commit()

        # Dispatch-after-commit: notifica o cliente só após o ETL ter sido
        # persistido com sucesso (sininho); nunca derruba o resultado.
        ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
        try:
            notificar_clientes_empresa(
                analise.id_empresa,
                f"Recebemos e processamos seus relatórios de {ref}. Sua devolutiva está em preparação.",
                url_for("cliente.dashboard"),
            )
            db.session.commit()
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.warning("Falha ao notificar processamento da análise %s: %s", id_analise, e)
        return render_template("admin/processar_resultado.html", analise=analise, resultado=resultado)

    # 7. Erro: descarta qualquer escrita parcial, marca uploads como ERRO e
    #    devolve a análise para RELATORIO_RECEBIDO (permite re-upload e novo processamento).
    db.session.rollback()
    analise = db.session.get(Analise, id_analise)
    upload_vendas = _buscar_upload(id_analise, "VENDAS")
    upload_compras = _buscar_upload(id_analise, "COMPRAS")
    analise.status_analise = "RELATORIO_RECEBIDO"
    for up in (upload_vendas, upload_compras):
        if up:
            up.status_processamento = "ERRO"
            up.mensagem_erro = resultado.mensagem
    db.session.commit()
    flash(f"Falha no processamento: {resultado.mensagem}", "danger")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/analise/deletar/<int:id_analise>", methods=["POST"])
@admin_required
def analise_deletar(id_analise):
    """
    Exclui uma análise e TUDO que depende dela.

    Como os relationships em models.py não têm cascade configurado (DER V4
    imutável + controle explícito do que sai do banco), removemos os filhos
    em ordem antes da Analise:

      1) UploadRelatorio  → também apaga os .xlsx/.csv físicos (best-effort);
      2) IndicadorAnalise (1:1);
      3) RelatorioAnalise (1:1);
      4) Analise.

    Usado primariamente para LIMPAR REGISTROS DE TESTE. Aceita qualquer
    status, inclusive CONCLUIDO publicado — a confirmação no JS é a
    salvaguarda contra acidente. POST-only para impedir deleção via
    crawler/preload de link.
    """
    analise = db.session.get(Analise, id_analise)
    if not analise:
        flash("Análise não encontrada.", "danger")
        return redirect(url_for("admin.analises"))

    # Snapshot para a mensagem antes de o objeto sair da sessão
    titulo_emp = analise.empresa.nome_fantasia or analise.empresa.razao_social
    id_str = f"#{analise.id_analise}"

    # Remove os ARQUIVOS FÍSICOS dos uploads antes de apagar os registros — a
    # remoção das linhas-filhas (uploads, indicador, relatório e Curva ABC) é
    # automática via cascade="all, delete-orphan" declarado em models.py.
    for up in analise.uploads:
        storage.remover(up.caminho_arquivo)  # best-effort (ver storage.py)

    db.session.delete(analise)  # cascata ORM apaga todos os filhos na ordem certa

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.warning("Falha ao excluir análise %s: %s", id_analise, e.orig if e.orig else e)
        flash(
            f"Não foi possível excluir a análise {id_str}: ainda há dados vinculados.",
            "danger",
        )
        return redirect(url_for("admin.analises"))

    flash(f"Análise {id_str} ({titulo_emp}) excluída com sucesso.", "success")
    return redirect(url_for("admin.analises"))


# =====================================================================
# Suporte: painel central de chamados (consultor)
# =====================================================================
@admin_bp.route("/tickets")
@admin_required
def tickets():
    """
    Painel central de suporte: todos os chamados de todas as empresas,
    agrupados por status (ABERTO / EM_ATENDIMENTO / RESOLVIDO).
    """
    todos = db.session.execute(
        select(ChamadoSuporte)
        .order_by(ChamadoSuporte.data_abertura.desc(), ChamadoSuporte.id_chamado.desc())
    ).scalars().all()

    grupos = {s: [] for s in STATUS_CHAMADO}
    for c in todos:
        grupos.setdefault(c.status_chamado, []).append(c)

    return render_template(
        "admin/tickets.html",
        grupos=grupos, categorias=CATEGORIAS_TICKET, total=len(todos),
    )


@admin_bp.route("/tickets/<int:id_chamado>", methods=["GET", "POST"])
@admin_required
def ticket_detalhe(id_chamado):
    """
    Conversa de um chamado. Ações (campo `acao`):
      - responder      → adiciona mensagem do consultor (+ EM_ATENDIMENTO se ABERTO)
                         e notifica os clientes da empresa.
      - alterar_status → muda status_chamado (ABERTO/EM_ATENDIMENTO/RESOLVIDO).
    """
    chamado = db.session.get(ChamadoSuporte, id_chamado)
    if chamado is None:
        flash("Chamado não encontrado.", "danger")
        return redirect(url_for("admin.tickets"))

    if request.method == "POST":
        acao = (request.form.get("acao") or "").strip().lower()

        if acao == "responder":
            texto = (request.form.get("mensagem") or "").strip()
            if not texto:
                flash("Digite uma resposta.", "warning")
                return redirect(url_for("admin.ticket_detalhe", id_chamado=id_chamado))
            db.session.add(MensagemSuporte(
                id_chamado=id_chamado,
                id_usuario_remetente=current_user.id_usuario,
                mensagem=texto,
            ))
            if chamado.status_chamado == "ABERTO":
                chamado.status_chamado = "EM_ATENDIMENTO"
            chamado.data_atualizacao = datetime.now(timezone.utc)
            notificar_clientes_empresa(
                chamado.id_empresa,
                f"A equipe NEXO respondeu seu chamado “{chamado.assunto}”.",
                url_for("cliente.ticket_detalhe", id_chamado=id_chamado),
            )
            db.session.commit()
            flash("Resposta enviada ao cliente.", "success")

        elif acao == "alterar_status":
            novo = (request.form.get("status_chamado") or "").strip().upper()
            if novo not in STATUS_CHAMADO:
                flash("Status inválido.", "danger")
                return redirect(url_for("admin.ticket_detalhe", id_chamado=id_chamado))
            chamado.status_chamado = novo
            chamado.data_atualizacao = datetime.now(timezone.utc)
            if novo == "RESOLVIDO":
                chamado.data_fechamento = datetime.now(timezone.utc)
                notificar_clientes_empresa(
                    chamado.id_empresa,
                    f"Seu chamado “{chamado.assunto}” foi marcado como resolvido.",
                    url_for("cliente.ticket_detalhe", id_chamado=id_chamado),
                )
                email_ticket_resolvido(
                    chamado,
                    url_for("cliente.ticket_detalhe", id_chamado=id_chamado, _external=True),
                )
            else:
                chamado.data_fechamento = None
            db.session.commit()
            flash("Status do chamado atualizado.", "success")
        else:
            flash("Ação inválida.", "danger")

        return redirect(url_for("admin.ticket_detalhe", id_chamado=id_chamado))

    return render_template(
        "admin/ticket_detalhe.html",
        chamado=chamado, categorias=CATEGORIAS_TICKET, status_opcoes=STATUS_CHAMADO,
    )


# =====================================================================
# CMS — Base de Conhecimento (Guia) gerenciável pelo Admin
# =====================================================================
_GUIA_IMG_EXT = {"png", "jpg", "jpeg", "webp", "gif"}


def _categorias_existentes():
    """Lista de categorias já usadas (para o datalist do formulário)."""
    return [c for (c,) in db.session.execute(
        select(GuiaTopico.categoria).distinct().order_by(GuiaTopico.categoria)
    ).all()]


def _salvar_imagem_guia(arquivo):
    """
    Salva com segurança a imagem enviada em static/uploads/guia/ e devolve o
    caminho RELATIVO ao static (ex.: 'uploads/guia/abc123.png'), ou None.
    Valida extensão; nomeia pelo hash do conteúdo (evita colisão e path traversal).
    """
    if not arquivo or not arquivo.filename:
        return None
    ext = os.path.splitext(secure_filename(arquivo.filename))[1].lower().lstrip(".")
    if ext not in _GUIA_IMG_EXT:
        flash(f"Imagem .{ext} não suportada. Use: {sorted(_GUIA_IMG_EXT)}.", "warning")
        return None
    conteudo = arquivo.read()
    if not conteudo:
        return None
    destino_dir = Path(current_app.static_folder) / "uploads" / "guia"
    destino_dir.mkdir(parents=True, exist_ok=True)
    nome = f"{hashlib.sha256(conteudo).hexdigest()[:16]}.{ext}"
    (destino_dir / nome).write_bytes(conteudo)
    return f"uploads/guia/{nome}"


@admin_bp.route("/guia")
@admin_required
def guia():
    """Listagem (CMS) de todas as soluções da Base de Conhecimento."""
    topicos = db.session.execute(
        select(GuiaTopico).order_by(GuiaTopico.categoria, GuiaTopico.id)
    ).scalars().all()
    return render_template("admin/guia.html", topicos=topicos, total=len(topicos))


@admin_bp.route("/guia/novo", methods=["GET", "POST"])
@admin_required
def guia_novo():
    """Cria um novo tópico da Base de Conhecimento."""
    if request.method == "POST":
        categoria = (request.form.get("categoria") or "").strip()
        pergunta = (request.form.get("pergunta") or "").strip()
        resposta = (request.form.get("resposta") or "").strip()
        if not (categoria and pergunta and resposta):
            flash("Categoria, pergunta e resposta são obrigatórias.", "danger")
            return redirect(url_for("admin.guia_novo"))

        topico = GuiaTopico(categoria=categoria, pergunta=pergunta, resposta=resposta)
        img = _salvar_imagem_guia(request.files.get("imagem"))
        if img:
            topico.imagem_url = img
        db.session.add(topico)
        db.session.commit()
        flash("Tópico criado na Base de Conhecimento.", "success")
        return redirect(url_for("admin.guia"))

    return render_template(
        "admin/guia_form.html", topico=None, categorias=_categorias_existentes(),
    )


@admin_bp.route("/guia/editar/<int:id>", methods=["GET", "POST"])
@admin_required
def guia_editar(id):
    """Edita um tópico existente (conteúdo e/ou imagem)."""
    topico = db.session.get(GuiaTopico, id)
    if topico is None:
        flash("Tópico não encontrado.", "danger")
        return redirect(url_for("admin.guia"))

    if request.method == "POST":
        categoria = (request.form.get("categoria") or "").strip()
        pergunta = (request.form.get("pergunta") or "").strip()
        resposta = (request.form.get("resposta") or "").strip()
        if not (categoria and pergunta and resposta):
            flash("Categoria, pergunta e resposta são obrigatórias.", "danger")
            return redirect(url_for("admin.guia_editar", id=id))

        topico.categoria = categoria
        topico.pergunta = pergunta
        topico.resposta = resposta
        topico.ativo = bool(request.form.get("ativo"))
        if request.form.get("remover_imagem"):
            topico.imagem_url = None
        img = _salvar_imagem_guia(request.files.get("imagem"))
        if img:
            topico.imagem_url = img
        topico.data_atualizacao = datetime.now(timezone.utc)
        db.session.commit()
        flash("Tópico atualizado.", "success")
        return redirect(url_for("admin.guia"))

    return render_template(
        "admin/guia_form.html", topico=topico, categorias=_categorias_existentes(),
    )


@admin_bp.route("/guia/<int:id>/deletar", methods=["POST"])
@admin_required
def guia_deletar(id):
    """Remove um tópico da Base de Conhecimento (POST-only)."""
    topico = db.session.get(GuiaTopico, id)
    if topico is None:
        flash("Tópico não encontrado.", "danger")
        return redirect(url_for("admin.guia"))
    db.session.delete(topico)
    db.session.commit()
    flash("Tópico removido da Base de Conhecimento.", "success")
    return redirect(url_for("admin.guia"))
