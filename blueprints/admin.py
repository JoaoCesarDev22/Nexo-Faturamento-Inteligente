import os
from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, current_app
from sqlalchemy import select

from extensions import db
from models import Analise, UploadRelatorio
from etl_processor import processar_arquivos_analise

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/dashboard")
def dashboard():
    analises = Analise.query.all()
    return render_template("admin/dashboard.html", analises=analises)


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


@admin_bp.route("/analise/<int:id_analise>/processar", methods=["POST", "GET"])
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

    agora = datetime.utcnow()
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
