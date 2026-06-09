import os
import hashlib
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Blueprint, render_template, redirect, url_for, flash, current_app, request, abort
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import select, func

from extensions import db
from models import Analise, UploadRelatorio, Empresa, Plano, Segmento, Usuario, RelatorioAnalise
from etl_processor import processar_arquivos_analise
from sqlalchemy.exc import IntegrityError

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

    return render_template(
        "admin/dashboard.html",
        stats=stats,
        analises=analises,
        dashboard_data=dashboard_data,
    )


@admin_bp.route("/empresas")
@admin_required
def empresas():
    """Listagem da carteira de empresas clientes."""
    rows = db.session.execute(
        select(Empresa).order_by(Empresa.razao_social.asc())
    ).scalars().all()
    return render_template("admin/empresas.html", empresas=rows)


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
        select(Empresa).order_by(Empresa.razao_social.asc())
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
        select(Empresa).order_by(Empresa.razao_social.asc())
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
            flash(f"Não foi possível criar a análise: {e.orig if e.orig else e}", "danger")
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
        novo_user.set_senha(form["usuario_senha"])

        try:
            db.session.add(nova_emp)
            db.session.flush()
            novo_user.id_empresa = nova_emp.id_empresa
            db.session.add(novo_user)
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            msg = str(e.orig) if e.orig else str(e)
            flash(f"Não foi possível salvar (CNPJ ou e-mail já cadastrados, ou FK inválida): {msg}", "danger")
            return redirect(url_for("admin.empresa_nova"))

        flash(f"Empresa '{nova_emp.razao_social}' cadastrada com usuário cliente vinculado.", "success")
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
            flash(f"Falha ao salvar alterações: {e.orig if e.orig else e}", "danger")
            return redirect(url_for("admin.empresa_editar", id_empresa=id_empresa))

        flash("Empresa atualizada com sucesso.", "success")
        return redirect(url_for("admin.empresas"))

    return render_template("admin/empresa_form.html", empresa=empresa, planos=planos, segmentos=segmentos)


def _buscar_upload(id_analise: int, tipo: str):
    """Consulta o upload diretamente no banco (não confia em coleção carregada em memória)."""
    return db.session.execute(
        select(UploadRelatorio).where(
            UploadRelatorio.id_analise == id_analise,
            UploadRelatorio.tipo_relatorio == tipo,
        )
    ).scalar_one_or_none()


def _resolver_caminho(upload: UploadRelatorio) -> str:
    """Caminho absoluto do arquivo salvo. Usa caminho_arquivo; relativiza ao UPLOAD_FOLDER se preciso."""
    caminho = upload.caminho_arquivo
    if not os.path.isabs(caminho) and not os.path.exists(caminho):
        caminho = os.path.join(current_app.config["UPLOAD_FOLDER"], os.path.basename(caminho))
    return caminho


# VULNERABILIDADE CORRIGIDA: Removido o método "GET". 
# Agora a rota aceita exclusivamente submissões via POST protegidas pelo sistema.
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

        upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
        upload_folder.mkdir(parents=True, exist_ok=True)
        nome_final = f"analise_{id_analise}_{tipo}_{sha[:8]}.{ext}"
        caminho_final = upload_folder / nome_final

        # UNIQUE(id_analise, tipo_relatorio) — re-upload substitui o anterior.
        # ATENÇÃO: só removemos o arquivo físico antigo se o caminho NOVO for diferente,
        # senão um re-upload com o mesmo conteúdo (mesmo SHA → mesmo nome) deletaria
        # o arquivo que acabamos de gravar.
        existente = _buscar_upload(id_analise, tipo)
        if existente and existente.caminho_arquivo and existente.caminho_arquivo != str(caminho_final):
            try:
                if os.path.exists(existente.caminho_arquivo):
                    os.remove(existente.caminho_arquivo)
            except OSError:
                pass  # remoção física é best-effort; o registro novo é o que vale
        if existente:
            db.session.delete(existente)
            db.session.flush()

        # Agora escreve o novo arquivo (depois de qualquer remoção física do antigo).
        caminho_final.write_bytes(conteudo)

        db.session.add(UploadRelatorio(
            id_analise=id_analise,
            id_usuario_admin=current_user.id_usuario,
            tipo_relatorio=tipo,
            nome_arquivo_original=nome_seguro,
            caminho_arquivo=str(caminho_final),
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
            faltando = [n for n, v in [
                ("resumo executivo", resumo),
                ("pontos positivos", pontos_pos),
                ("pontos de alerta", pontos_alerta),
                ("recomendações", recomendacoes),
            ] if not v]
            if faltando:
                flash(f"Para publicar é necessário preencher: {', '.join(faltando)}.", "danger")
                db.session.rollback()
                return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

            rel.publicado = True
            rel.data_publicacao = agora
            analise.status_analise = "CONCLUIDO"
            analise.data_conclusao = agora

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
            flash(f"Falha ao salvar o relatório: {e.orig if e.orig else e}", "danger")
            return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

        msg_acao = {"rascunho": "Rascunho salvo.", "publicar": "Devolutiva publicada para o cliente.",
                    "despublicar": "Devolutiva despublicada (análise voltou a EM_ANALISE)."}[acao]
        flash(msg_acao, "success")
        return redirect(url_for("admin.analise_relatorio", id_analise=id_analise))

    return render_template("admin/relatorio_editor.html", analise=analise)


@admin_bp.route("/analise/<int:id_analise>/processar", methods=["POST"])
@admin_required
def processar_analise(id_analise):
    analise = db.session.get(Analise, id_analise)
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

    # CORREÇÃO DE DEPRECIAÇÃO: datetime.utcnow() foi descontinuado. Usando fuso UTC explícito.
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

    # 1) Uploads + arquivos físicos
    uploads = db.session.execute(
        select(UploadRelatorio).where(UploadRelatorio.id_analise == id_analise)
    ).scalars().all()
    for up in uploads:
        try:
            caminho = _resolver_caminho(up)
            if caminho and os.path.exists(caminho):
                os.remove(caminho)
        except OSError:
            pass  # remoção física é best-effort; o registro segue sendo deletado
        db.session.delete(up)

    # 2) IndicadorAnalise (1:1) — pode não existir se o ETL nunca rodou
    if analise.indicador is not None:
        db.session.delete(analise.indicador)

    # 3) RelatorioAnalise (1:1) — pode não existir se nunca houve devolutiva
    if analise.relatorio is not None:
        db.session.delete(analise.relatorio)

    # 4) Analise (pai)
    db.session.delete(analise)

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        flash(
            f"Não foi possível excluir a análise {id_str}: {e.orig if e.orig else e}",
            "danger",
        )
        return redirect(url_for("admin.analises"))

    flash(f"Análise {id_str} ({titulo_emp}) excluída com sucesso.", "success")
    return redirect(url_for("admin.analises"))
