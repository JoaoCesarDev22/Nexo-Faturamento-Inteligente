"""
NEXO - Faturamento Inteligente | Modelos SQLAlchemy
====================================================
Apenas as 8 tabelas em uso real no MVP do PI2:

    plano, segmento, empresa, usuario,
    analise, upload_relatorio, indicador_analise, relatorio_analise.

As 4 tabelas FORA DO ESCOPO PI2 (chamado_suporte, mensagem_suporte,
avaliacao_analise, fatura_cobranca) existem no schema SQL como
documentação do modelo lógico completo, mas NÃO têm Model aqui.
Quando voltarem ao escopo pós-PI3, adicione as classes correspondentes.

Estilo: SQLAlchemy 2.x com `Mapped` e `mapped_column` (mais moderno
e type-safe que o estilo Column antigo).
"""

from datetime import datetime, date
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    String, Integer, Boolean, Date, DateTime, Numeric, Text,
    ForeignKey, CheckConstraint, UniqueConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db


# =====================================================================
# 1. Plano
# =====================================================================
class Plano(db.Model):
    __tablename__ = "plano"

    id_plano: Mapped[int] = mapped_column(primary_key=True)
    nome_plano: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    descricao: Mapped[Optional[str]] = mapped_column(Text)
    valor_mensal: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    qtd_analises_mes: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo_analise_permitida: Mapped[str] = mapped_column(String, nullable=False)
    nivel_entrega_analise: Mapped[str] = mapped_column(String, nullable=False)
    nivel_dashboard: Mapped[str] = mapped_column(String, nullable=False)
    nivel_atendimento: Mapped[str] = mapped_column(String, nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint("nome_plano IN ('BRONZE', 'PRATA', 'OURO')"),
        CheckConstraint("tipo_analise_permitida IN ('MENSAL', 'QUINZENAL')"),
        CheckConstraint("nivel_entrega_analise IN ('BASICA', 'COMPLETA', 'PREMIUM')"),
        CheckConstraint("nivel_dashboard IN ('RESUMIDO', 'GERENCIAL', 'COMPLETO')"),
        CheckConstraint("nivel_atendimento IN ('BAIXO', 'MEDIO', 'ALTO')"),
    )


# =====================================================================
# 2. Segmento
# =====================================================================
class Segmento(db.Model):
    __tablename__ = "segmento"

    id_segmento: Mapped[int] = mapped_column(primary_key=True)
    nome_segmento: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    descricao: Mapped[Optional[str]] = mapped_column(Text)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)


# =====================================================================
# 3. Empresa
# =====================================================================
class Empresa(db.Model):
    __tablename__ = "empresa"

    id_empresa: Mapped[int] = mapped_column(primary_key=True)
    id_segmento: Mapped[int] = mapped_column(ForeignKey("segmento.id_segmento"), nullable=False)
    id_plano_atual: Mapped[int] = mapped_column(ForeignKey("plano.id_plano"), nullable=False)
    cnpj: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    razao_social: Mapped[str] = mapped_column(String, nullable=False)
    nome_fantasia: Mapped[Optional[str]] = mapped_column(String)
    email_contato: Mapped[str] = mapped_column(String, nullable=False)
    telefone_contato: Mapped[Optional[str]] = mapped_column(String)
    data_contratacao: Mapped[date] = mapped_column(Date, nullable=False)
    faturamento_base_mensal: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    status_conta: Mapped[str] = mapped_column(String, nullable=False, default="ATIVA")
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # Soft delete (Lixeira): NULL = ativa; preenchido = movida para a lixeira.
    # As listagens normais filtram deletado_em IS NULL; a tela /admin/lixeira
    # lista o complemento. A exclusão física só ocorre via "excluir-permanente".
    deletado_em: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Relacionamentos
    segmento = relationship("Segmento")
    plano_atual = relationship("Plano")
    usuarios = relationship("Usuario", back_populates="empresa")
    analises = relationship("Analise", back_populates="empresa")

    __table_args__ = (
        CheckConstraint("status_conta IN ('ATIVA', 'SUSPENSA', 'CANCELADA')"),
    )


# =====================================================================
# 4. Usuario
# =====================================================================
class Usuario(db.Model, UserMixin):
    """
    Usuário do sistema. UserMixin do Flask-Login fornece `is_authenticated`,
    `is_active`, `get_id()` etc., evitando boilerplate.
    """
    __tablename__ = "usuario"

    id_usuario: Mapped[int] = mapped_column(primary_key=True)
    id_empresa: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id_empresa"))
    nome: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    senha_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Primeiro acesso: novos clientes (criados via ORM) nascem com True e são
    # obrigados a trocar a senha temporária no 1º login. server_default="false"
    # garante que usuários JÁ existentes não sejam disruptados pela migração.
    primeiro_acesso: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="false"
    )
    ultimo_acesso: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)

    empresa = relationship("Empresa", back_populates="usuarios")

    __table_args__ = (
        CheckConstraint("role IN ('ADMIN', 'CLIENTE')"),
        CheckConstraint(
            "(role = 'CLIENTE' AND id_empresa IS NOT NULL) OR "
            "(role = 'ADMIN'   AND id_empresa IS NULL)",
            name="ck_usuario_role_empresa"
        ),
    )

    # --- Override do Flask-Login: usa nossa PK customizada ---
    def get_id(self) -> str:
        return str(self.id_usuario)

    @property
    def is_active(self) -> bool:
        return self.ativo

    # --- Helpers de senha ---
    def set_senha(self, senha_plain: str) -> None:
        self.senha_hash = generate_password_hash(senha_plain)

    def check_senha(self, senha_plain: str) -> bool:
        return check_password_hash(self.senha_hash, senha_plain)

    # --- Helpers de role ---
    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    @property
    def is_cliente(self) -> bool:
        return self.role == "CLIENTE"


# =====================================================================
# 5. Analise
# =====================================================================
class Analise(db.Model):
    __tablename__ = "analise"

    id_analise: Mapped[int] = mapped_column(primary_key=True)
    id_empresa: Mapped[int] = mapped_column(ForeignKey("empresa.id_empresa"), nullable=False)
    id_plano_referencia: Mapped[int] = mapped_column(ForeignKey("plano.id_plano"), nullable=False)
    id_usuario_admin_responsavel: Mapped[int] = mapped_column(
        ForeignKey("usuario.id_usuario"), nullable=False
    )
    periodo_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    periodo_fim: Mapped[date] = mapped_column(Date, nullable=False)
    mes_referencia: Mapped[int] = mapped_column(Integer, nullable=False)
    ano_referencia: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo_analise: Mapped[str] = mapped_column(String, nullable=False)
    quinzena_referencia: Mapped[Optional[int]] = mapped_column(Integer)
    status_analise: Mapped[str] = mapped_column(
        String, nullable=False, default="AGUARDANDO_RELATORIO"
    )
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_conclusao: Mapped[Optional[datetime]] = mapped_column(DateTime)

    empresa = relationship("Empresa", back_populates="analises")
    plano_referencia = relationship("Plano")
    admin_responsavel = relationship("Usuario")
    # cascade="all, delete-orphan" no nível do ORM: ao deletar uma Analise, o
    # SQLAlchemy remove automaticamente os filhos (uploads, indicador, relatório
    # e Curva ABC) na ordem correta — sem precisar de ON DELETE CASCADE no banco
    # (migration-free) e mantendo a rede de proteção das FKs intacta. Estes
    # registros não têm sentido sem a análise-mãe (ownership forte).
    uploads = relationship(
        "UploadRelatorio", back_populates="analise",
        cascade="all, delete-orphan",
    )
    indicador = relationship(
        "IndicadorAnalise", back_populates="analise", uselist=False,
        cascade="all, delete-orphan",
    )
    relatorio = relationship(
        "RelatorioAnalise", back_populates="analise", uselist=False,
        cascade="all, delete-orphan",
    )
    curva_abc = relationship(
        "ProdutoCurvaABC",
        back_populates="analise",
        order_by="ProdutoCurvaABC.posicao_ranking",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("mes_referencia BETWEEN 1 AND 12"),
        CheckConstraint("tipo_analise IN ('MENSAL', 'QUINZENAL')"),
        CheckConstraint(
            "status_analise IN ('AGUARDANDO_RELATORIO', 'RELATORIO_RECEBIDO', "
            "'EM_ANALISE', 'CONCLUIDO')"
        ),
        CheckConstraint(
            "(tipo_analise = 'MENSAL'    AND quinzena_referencia IS NULL) OR "
            "(tipo_analise = 'QUINZENAL' AND quinzena_referencia IS NOT NULL "
            "                            AND quinzena_referencia IN (1, 2))",
            name="ck_analise_tipo_quinzena"
        ),
    )


# =====================================================================
# 6. UploadRelatorio
# =====================================================================
class UploadRelatorio(db.Model):
    __tablename__ = "upload_relatorio"

    id_upload: Mapped[int] = mapped_column(primary_key=True)
    id_analise: Mapped[int] = mapped_column(ForeignKey("analise.id_analise"), nullable=False)
    # Quem enviou o arquivo. NULL = enviado pelo PRÓPRIO CLIENTE (self-service
    # onboarding) e ainda aguardando homologação do consultor. Preenchido com o
    # id do ADMIN quando o upload é feito/homologado pelo time NEXO.
    id_usuario_admin: Mapped[Optional[int]] = mapped_column(ForeignKey("usuario.id_usuario"))
    tipo_relatorio: Mapped[str] = mapped_column(String, nullable=False)
    nome_arquivo_original: Mapped[str] = mapped_column(String, nullable=False)
    caminho_arquivo: Mapped[str] = mapped_column(String, nullable=False)
    extensao_arquivo: Mapped[str] = mapped_column(String, nullable=False)
    tamanho_arquivo: Mapped[Optional[int]] = mapped_column(Integer)
    hash_arquivo: Mapped[Optional[str]] = mapped_column(String)
    data_upload: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    status_processamento: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDENTE"
    )
    data_processamento: Mapped[Optional[datetime]] = mapped_column(DateTime)
    mensagem_erro: Mapped[Optional[str]] = mapped_column(Text)

    analise = relationship("Analise", back_populates="uploads")

    __table_args__ = (
        CheckConstraint("tipo_relatorio IN ('VENDAS', 'COMPRAS')"),
        CheckConstraint("extensao_arquivo IN ('CSV', 'XLSX', 'XLS')"),
        CheckConstraint("status_processamento IN ('PENDENTE', 'PROCESSADO', 'ERRO')"),
        UniqueConstraint("id_analise", "tipo_relatorio", name="uq_upload_analise_tipo"),
    )

    @property
    def enviado_por_cliente(self) -> bool:
        """True quando o arquivo foi anexado pelo próprio cliente (sem admin),
        aguardando homologação do consultor."""
        return self.id_usuario_admin is None


# =====================================================================
# 7. IndicadorAnalise
# =====================================================================
class IndicadorAnalise(db.Model):
    __tablename__ = "indicador_analise"

    id_indicador: Mapped[int] = mapped_column(primary_key=True)
    id_analise: Mapped[int] = mapped_column(
        ForeignKey("analise.id_analise"), nullable=False, unique=True
    )
    faturamento_total: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    total_comprado: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    saldo_estimado_compras_vendas: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    produto_mais_vendido_nome: Mapped[Optional[str]] = mapped_column(String)
    produto_mais_vendido_quantidade: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    produto_maior_faturamento_nome: Mapped[Optional[str]] = mapped_column(String)
    produto_maior_faturamento_valor: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    produto_maior_saldo_parado_nome: Mapped[Optional[str]] = mapped_column(String)
    saldo_estimado_parado: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    # Período REAL coberto pela base, lido do cabeçalho do relatório do PDV
    # (via pandas no ETL). Pode ser nulo se o relatório não declarar período.
    periodo_base_inicio: Mapped[Optional[date]] = mapped_column(Date)
    periodo_base_fim: Mapped[Optional[date]] = mapped_column(Date)
    versao_processamento: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    data_geracao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    analise = relationship("Analise", back_populates="indicador")


# =====================================================================
# 8. RelatorioAnalise
# =====================================================================
class RelatorioAnalise(db.Model):
    __tablename__ = "relatorio_analise"

    id_relatorio: Mapped[int] = mapped_column(primary_key=True)
    id_analise: Mapped[int] = mapped_column(
        ForeignKey("analise.id_analise"), nullable=False, unique=True
    )
    titulo: Mapped[str] = mapped_column(String, nullable=False)
    resumo_executivo: Mapped[Optional[str]] = mapped_column(Text)
    pontos_positivos: Mapped[Optional[str]] = mapped_column(Text)
    pontos_de_alerta: Mapped[Optional[str]] = mapped_column(Text)
    recomendacoes: Mapped[Optional[str]] = mapped_column(Text)
    # NOT NULL: conclusao_estrategica é o produto entregue ao cliente.
    conclusao_estrategica: Mapped[str] = mapped_column(Text, nullable=False)
    publicado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_publicacao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    # True quando resumo/conclusão foram gerados pelo motor de IA (Claude).
    # Usado para exibir, com honestidade, o selo "Gerado por IA Nexo" ao cliente.
    # server_default garante a migração segura sobre linhas já existentes.
    gerado_por_ia: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    analise = relationship("Analise", back_populates="relatorio")


# =====================================================================
# 9. ProdutoCurvaABC  (embasamento metodológico — Princípio de Pareto)
# =====================================================================
class ProdutoCurvaABC(db.Model):
    """
    Ranking de produtos por faturamento de uma análise, classificados pela
    Curva ABC (Princípio de Pareto / regra 80-20):

        Classe A (Estratégicos):  produtos até 80% do faturamento acumulado.
        Classe B (Táticos):       de 80% a 95% do faturamento acumulado.
        Classe C (Operacionais):  os ~5% restantes (cauda longa).

    Diferente de indicador_analise (KPIs consolidados 1:1 com a análise), aqui
    persistimos uma LINHA POR PRODUTO do topo do ranking (top-N), o que viabiliza
    o gráfico de barras horizontais coloridas por classe no painel do cliente.

    Mantém a regra inegociável do ETL: NÃO há lucro/margem/CMV — apenas
    faturamento agregado por produto, que é dado bruto honesto do PDV.
    """
    __tablename__ = "produto_curva_abc"

    id_produto_abc: Mapped[int] = mapped_column(primary_key=True)
    id_analise: Mapped[int] = mapped_column(ForeignKey("analise.id_analise"), nullable=False)
    posicao_ranking: Mapped[int] = mapped_column(Integer, nullable=False)
    produto_nome: Mapped[str] = mapped_column(String, nullable=False)
    faturamento: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    quantidade: Mapped[Optional[Decimal]] = mapped_column(Numeric)
    percentual_individual: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    percentual_acumulado: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    classe_abc: Mapped[str] = mapped_column(String, nullable=False)
    data_geracao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    analise = relationship("Analise", back_populates="curva_abc")

    __table_args__ = (
        CheckConstraint("classe_abc IN ('A', 'B', 'C')"),
        UniqueConstraint("id_analise", "posicao_ranking", name="uq_abc_analise_posicao"),
        Index("ix_abc_analise", "id_analise"),
    )


# =====================================================================
# 10. ChamadoSuporte  (REATIVAÇÃO da tabela documentada chamado_suporte)
# =====================================================================
class ChamadoSuporte(db.Model):
    """
    Ticket de suporte aberto por um usuário CLIENTE para a equipe NEXO.
    Reativa a tabela `chamado_suporte` que já existia no DER como documentação
    (pós-PI3), adaptada ao escopo atual:
      - adicionado `categoria` (Financeiro / Dúvida Técnica / Erro de Integração);
      - `status_chamado` simplificado para 3 estados (ABERTO / EM_ATENDIMENTO /
        RESOLVIDO);
      - `prioridade` mantido (documentado), default MEDIA — não exposto na UI.
    O campo `assunto` é o título do ticket.
    """
    __tablename__ = "chamado_suporte"

    id_chamado: Mapped[int] = mapped_column(primary_key=True)
    id_empresa: Mapped[int] = mapped_column(ForeignKey("empresa.id_empresa"), nullable=False)
    id_usuario_cliente: Mapped[int] = mapped_column(ForeignKey("usuario.id_usuario"), nullable=False)
    assunto: Mapped[str] = mapped_column(String, nullable=False)
    categoria: Mapped[str] = mapped_column(String, nullable=False)
    status_chamado: Mapped[str] = mapped_column(String, nullable=False, default="ABERTO")
    prioridade: Mapped[str] = mapped_column(String, nullable=False, default="MEDIA")
    data_abertura: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)
    data_fechamento: Mapped[Optional[datetime]] = mapped_column(DateTime)

    empresa = relationship("Empresa")
    usuario_cliente = relationship("Usuario")
    mensagens = relationship(
        "MensagemSuporte",
        back_populates="chamado",
        order_by="MensagemSuporte.data_envio",
    )

    __table_args__ = (
        CheckConstraint(
            "categoria IN ('FINANCEIRO', 'DUVIDA_TECNICA', 'ERRO_INTEGRACAO')"
        ),
        CheckConstraint(
            "status_chamado IN ('ABERTO', 'EM_ATENDIMENTO', 'RESOLVIDO')"
        ),
        CheckConstraint("prioridade IN ('BAIXA', 'MEDIA', 'ALTA')"),
        Index("ix_chamado_empresa", "id_empresa"),
    )


# =====================================================================
# 11. MensagemSuporte  (REATIVAÇÃO da tabela documentada mensagem_suporte)
# =====================================================================
class MensagemSuporte(db.Model):
    """Mensagem dentro de um chamado (conversa cliente ↔ consultor)."""
    __tablename__ = "mensagem_suporte"

    id_mensagem: Mapped[int] = mapped_column(primary_key=True)
    id_chamado: Mapped[int] = mapped_column(ForeignKey("chamado_suporte.id_chamado"), nullable=False)
    id_usuario_remetente: Mapped[int] = mapped_column(ForeignKey("usuario.id_usuario"), nullable=False)
    mensagem: Mapped[str] = mapped_column(Text, nullable=False)
    data_envio: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    lida: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    chamado = relationship("ChamadoSuporte", back_populates="mensagens")
    remetente = relationship("Usuario")

    __table_args__ = (
        Index("ix_mensagem_chamado", "id_chamado"),
    )


# =====================================================================
# 12. Notificacao  (sininho da navbar — engajamento)
# =====================================================================
class Notificacao(db.Model):
    """
    Notificação dirigida a UM usuário (cliente ou admin). Alimenta o sininho
    da navbar. Gerada por gatilhos do sistema (upload de cliente, homologação,
    publicação de análise, resposta em ticket).
    """
    __tablename__ = "notificacao"

    id_notificacao: Mapped[int] = mapped_column(primary_key=True)
    id_usuario: Mapped[int] = mapped_column(ForeignKey("usuario.id_usuario"), nullable=False)
    texto: Mapped[str] = mapped_column(String, nullable=False)
    link_destino: Mapped[Optional[str]] = mapped_column(String)
    lida: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    usuario = relationship("Usuario")

    __table_args__ = (
        Index("ix_notificacao_usuario", "id_usuario"),
    )


# =====================================================================
# 13. GuiaTopico  (CMS — Base de Conhecimento do Guia / NexoBot)
# =====================================================================
class GuiaTopico(db.Model):
    """
    Tópico da Base de Conhecimento, gerenciável pelo ADMIN (CMS). Fonte ÚNICA
    de verdade consumida pela aba "Guia" do cliente E pelo NexoBot (DRY).
      - categoria: agrupador (ex.: "Upload", "Senhas", "PDV");
      - pergunta:  título da dúvida (vira o cabeçalho do accordion);
      - resposta:  texto livre (passo a passo, quebras de linha preservadas);
      - imagem_url: caminho relativo em static/ (ex.: 'uploads/guia/x.png'), opcional.
    """
    __tablename__ = "guia_topico"

    id: Mapped[int] = mapped_column(primary_key=True)
    categoria: Mapped[str] = mapped_column(String, nullable=False)
    pergunta: Mapped[str] = mapped_column(String, nullable=False)
    resposta: Mapped[str] = mapped_column(Text, nullable=False)
    imagem_url: Mapped[Optional[str]] = mapped_column(String)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    data_criacao: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    data_atualizacao: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_guia_topico_categoria", "categoria"),
    )
