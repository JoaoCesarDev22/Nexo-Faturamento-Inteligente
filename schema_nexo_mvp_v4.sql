-- =====================================================================
-- NEXO - Faturamento Inteligente | Schema V4 do MVP (SQLite)
-- =====================================================================
-- Versão consolidada para o PI2 (entrega 18/06).
--
-- Mudanças da V3 para a V4:
--   1. Removidos os 3 KPIs inviáveis em indicador_analise:
--      ticket_medio, dia_pico_vendas, horario_pico_vendas.
--      Motivo: relatórios reais do PDV do cliente piloto são agregados
--      por período, sem transação item a item, inviabilizando cálculo.
--   2. Removido link_dashboard_powerbi de analise.
--      Motivo: Power BI Embedded foi descartado em favor de Plotly.js
--      renderizado nativamente no Flask.
--   3. CHECK consolidado em analise (tipo_analise + quinzena_referencia)
--      em uma única cláusula, mais robusta contra three-valued logic do SQL.
--   4. CHECK em usuario alinhado ao DER: ADMIN obriga id_empresa NULL.
--   5. Comentário explícito nas 4 tabelas FORA DO ESCOPO PI2:
--      chamado_suporte, mensagem_suporte, avaliacao_analise, fatura_cobranca.
--      Permanecem no DDL como documentação do modelo lógico completo,
--      mas não terão Model SQLAlchemy nem rotas implementadas no PI2.
--   6. Tipos monetários padronizados em NUMERIC (SQLite ignora o detalhe
--      de precisão; NUMERIC é a convenção mais comum).
--   7. Tipo INTEGER PRIMARY KEY sem AUTOINCREMENT (SQLite já auto-incrementa
--      com ROWID; AUTOINCREMENT só agrega custo se você precisa evitar
--      reuso de IDs deletados, o que não é o caso aqui).
--
-- IMPORTANTE — sobre PRAGMA foreign_keys:
--   No SQLite, foreign keys vêm DESLIGADAS por padrão. Este PRAGMA precisa
--   ser executado em TODA conexão nova. Escrevê-lo aqui no topo do .sql
--   funciona apenas na sessão que cria as tabelas. Na aplicação Flask,
--   é OBRIGATÓRIO configurar um event listener no SQLAlchemy para forçar
--   o PRAGMA em cada conexão (ver app.py).
-- =====================================================================

PRAGMA foreign_keys = ON;


-- =====================================================================
-- 1. plano
-- =====================================================================
CREATE TABLE plano (
    id_plano INTEGER PRIMARY KEY,
    nome_plano TEXT NOT NULL UNIQUE
        CHECK(nome_plano IN ('BRONZE', 'PRATA', 'OURO')),
    descricao TEXT,
    valor_mensal NUMERIC NOT NULL,
    qtd_analises_mes INTEGER NOT NULL,
    tipo_analise_permitida TEXT NOT NULL
        CHECK(tipo_analise_permitida IN ('MENSAL', 'QUINZENAL')),
    nivel_entrega_analise TEXT NOT NULL
        CHECK(nivel_entrega_analise IN ('BASICA', 'COMPLETA', 'PREMIUM')),
    nivel_dashboard TEXT NOT NULL
        CHECK(nivel_dashboard IN ('RESUMIDO', 'GERENCIAL', 'COMPLETO')),
    nivel_atendimento TEXT NOT NULL
        CHECK(nivel_atendimento IN ('BAIXO', 'MEDIO', 'ALTO')),
    ativo BOOLEAN NOT NULL DEFAULT 1,
    data_criacao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    data_atualizacao DATETIME
);


-- =====================================================================
-- 2. segmento
-- =====================================================================
CREATE TABLE segmento (
    id_segmento INTEGER PRIMARY KEY,
    nome_segmento TEXT NOT NULL UNIQUE,
    descricao TEXT,
    ativo BOOLEAN NOT NULL DEFAULT 1,
    data_criacao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    data_atualizacao DATETIME
);


-- =====================================================================
-- 3. empresa
-- =====================================================================
CREATE TABLE empresa (
    id_empresa INTEGER PRIMARY KEY,
    id_segmento INTEGER NOT NULL,
    id_plano_atual INTEGER NOT NULL,
    cnpj TEXT NOT NULL UNIQUE,
    razao_social TEXT NOT NULL,
    nome_fantasia TEXT,
    email_contato TEXT NOT NULL,
    telefone_contato TEXT,
    data_contratacao DATE NOT NULL,
    faturamento_base_mensal NUMERIC,
    status_conta TEXT NOT NULL DEFAULT 'ATIVA'
        CHECK(status_conta IN ('ATIVA', 'SUSPENSA', 'CANCELADA')),
    data_criacao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    data_atualizacao DATETIME,
    FOREIGN KEY (id_segmento) REFERENCES segmento(id_segmento),
    FOREIGN KEY (id_plano_atual) REFERENCES plano(id_plano)
);


-- =====================================================================
-- 4. usuario
-- =====================================================================
-- Regra de role: CLIENTE precisa ter empresa; ADMIN nunca tem empresa.
-- Reforça a separação de domínios: consultor NEXO vs operador do cliente.
CREATE TABLE usuario (
    id_usuario INTEGER PRIMARY KEY,
    id_empresa INTEGER,
    nome TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    senha_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('ADMIN', 'CLIENTE')),
    ativo BOOLEAN NOT NULL DEFAULT 1,
    ultimo_acesso DATETIME,
    data_criacao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    data_atualizacao DATETIME,
    CHECK (
        (role = 'CLIENTE' AND id_empresa IS NOT NULL) OR
        (role = 'ADMIN'   AND id_empresa IS NULL)
    ),
    FOREIGN KEY (id_empresa) REFERENCES empresa(id_empresa)
);


-- =====================================================================
-- 5. analise
-- =====================================================================
-- Transições válidas de status_analise (enforçadas pela aplicação, não pelo banco):
--   AGUARDANDO_RELATORIO  → criada, sem uploads
--   RELATORIO_RECEBIDO    → existem ambos os uploads (VENDAS e COMPRAS),
--                           processamento ainda não disparado
--   EM_ANALISE            → admin clicou "Processar agora", KPIs gerados,
--                           devolutiva em rascunho
--   CONCLUIDO             → admin clicou "Publicar para cliente"
CREATE TABLE analise (
    id_analise INTEGER PRIMARY KEY,
    id_empresa INTEGER NOT NULL,
    id_plano_referencia INTEGER NOT NULL,
    id_usuario_admin_responsavel INTEGER NOT NULL,
    periodo_inicio DATE NOT NULL,
    periodo_fim DATE NOT NULL,
    mes_referencia INTEGER NOT NULL CHECK(mes_referencia BETWEEN 1 AND 12),
    ano_referencia INTEGER NOT NULL,
    tipo_analise TEXT NOT NULL CHECK(tipo_analise IN ('MENSAL', 'QUINZENAL')),
    quinzena_referencia INTEGER,
    status_analise TEXT NOT NULL DEFAULT 'AGUARDANDO_RELATORIO'
        CHECK(status_analise IN (
            'AGUARDANDO_RELATORIO',
            'RELATORIO_RECEBIDO',
            'EM_ANALISE',
            'CONCLUIDO'
        )),
    data_criacao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    data_atualizacao DATETIME,
    data_conclusao DATETIME,
    -- Regra cruzada robusta contra three-valued logic:
    -- MENSAL exige quinzena_referencia NULL;
    -- QUINZENAL exige quinzena_referencia IS NOT NULL E IN (1,2).
    CHECK (
        (tipo_analise = 'MENSAL'    AND quinzena_referencia IS NULL) OR
        (tipo_analise = 'QUINZENAL' AND quinzena_referencia IS NOT NULL
                                    AND quinzena_referencia IN (1, 2))
    ),
    FOREIGN KEY (id_empresa) REFERENCES empresa(id_empresa),
    FOREIGN KEY (id_plano_referencia) REFERENCES plano(id_plano),
    FOREIGN KEY (id_usuario_admin_responsavel) REFERENCES usuario(id_usuario)
);


-- =====================================================================
-- 6. upload_relatorio
-- =====================================================================
CREATE TABLE upload_relatorio (
    id_upload INTEGER PRIMARY KEY,
    id_analise INTEGER NOT NULL,
    id_usuario_admin INTEGER NOT NULL,
    tipo_relatorio TEXT NOT NULL
        CHECK(tipo_relatorio IN ('VENDAS', 'COMPRAS')),
    nome_arquivo_original TEXT NOT NULL,
    caminho_arquivo TEXT NOT NULL,
    extensao_arquivo TEXT NOT NULL
        CHECK(extensao_arquivo IN ('CSV', 'XLSX', 'XLS')),
    tamanho_arquivo INTEGER,
    hash_arquivo TEXT,
    data_upload DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status_processamento TEXT NOT NULL DEFAULT 'PENDENTE'
        CHECK(status_processamento IN ('PENDENTE', 'PROCESSADO', 'ERRO')),
    data_processamento DATETIME,
    mensagem_erro TEXT,
    UNIQUE (id_analise, tipo_relatorio),
    FOREIGN KEY (id_analise) REFERENCES analise(id_analise),
    FOREIGN KEY (id_usuario_admin) REFERENCES usuario(id_usuario)
);


-- =====================================================================
-- 7. indicador_analise
-- =====================================================================
-- KPIs FINAIS consolidados da análise. Não armazena linhas processadas
-- de vendas/compras (decisão de MVP: DataFrames pandas vivem em memória
-- durante o ETL e são descartados ao fim).
--
-- KPIs REMOVIDOS nesta V4:
--   ticket_medio, dia_pico_vendas, horario_pico_vendas.
--   Motivo: relatório do PDV do cliente piloto é agregado por produto
--   no período, sem transação item a item com data/hora.
CREATE TABLE indicador_analise (
    id_indicador INTEGER PRIMARY KEY,
    id_analise INTEGER NOT NULL UNIQUE,
    faturamento_total NUMERIC,
    total_comprado NUMERIC,
    saldo_estimado_compras_vendas NUMERIC,
    produto_mais_vendido_nome TEXT,
    produto_mais_vendido_quantidade NUMERIC,
    produto_maior_faturamento_nome TEXT,
    produto_maior_faturamento_valor NUMERIC,
    produto_maior_saldo_parado_nome TEXT,
    saldo_estimado_parado NUMERIC,
    versao_processamento INTEGER NOT NULL DEFAULT 1,
    data_geracao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (id_analise) REFERENCES analise(id_analise)
);


-- =====================================================================
-- 8. relatorio_analise
-- =====================================================================
-- A devolutiva estratégica é o PRODUTO ENTREGUE. Conclusao_estrategica
-- é NOT NULL: não faz sentido publicar relatório sem conclusão.
CREATE TABLE relatorio_analise (
    id_relatorio INTEGER PRIMARY KEY,
    id_analise INTEGER NOT NULL UNIQUE,
    titulo TEXT NOT NULL,
    resumo_executivo TEXT,
    pontos_positivos TEXT,
    pontos_de_alerta TEXT,
    recomendacoes TEXT,
    conclusao_estrategica TEXT NOT NULL,
    publicado BOOLEAN NOT NULL DEFAULT 0,
    data_publicacao DATETIME,
    FOREIGN KEY (id_analise) REFERENCES analise(id_analise)
);


-- =====================================================================
-- 9. chamado_suporte
-- =====================================================================
-- ⚠️ FORA DO ESCOPO PI2 — implementação prevista para pós-PI3.
-- Mantida no DDL como documentação do modelo lógico completo.
-- No PI2: não terá Model SQLAlchemy nem rotas. Suporte ao cliente
-- será via link direto para WhatsApp da equipe NEXO.
CREATE TABLE chamado_suporte (
    id_chamado INTEGER PRIMARY KEY,
    id_empresa INTEGER NOT NULL,
    id_usuario_cliente INTEGER NOT NULL,
    assunto TEXT NOT NULL,
    status_chamado TEXT NOT NULL DEFAULT 'ABERTO'
        CHECK(status_chamado IN ('ABERTO', 'EM_ATENDIMENTO', 'RESPONDIDO', 'FECHADO')),
    prioridade TEXT NOT NULL DEFAULT 'MEDIA'
        CHECK(prioridade IN ('BAIXA', 'MEDIA', 'ALTA')),
    data_abertura DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    data_atualizacao DATETIME,
    data_fechamento DATETIME,
    FOREIGN KEY (id_empresa) REFERENCES empresa(id_empresa),
    FOREIGN KEY (id_usuario_cliente) REFERENCES usuario(id_usuario)
);


-- =====================================================================
-- 10. mensagem_suporte
-- =====================================================================
-- ⚠️ FORA DO ESCOPO PI2 — implementação prevista para pós-PI3.
CREATE TABLE mensagem_suporte (
    id_mensagem INTEGER PRIMARY KEY,
    id_chamado INTEGER NOT NULL,
    id_usuario_remetente INTEGER NOT NULL,
    mensagem TEXT NOT NULL,
    data_envio DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lida BOOLEAN NOT NULL DEFAULT 0,
    FOREIGN KEY (id_chamado) REFERENCES chamado_suporte(id_chamado),
    FOREIGN KEY (id_usuario_remetente) REFERENCES usuario(id_usuario)
);


-- =====================================================================
-- 11. avaliacao_analise
-- =====================================================================
-- ⚠️ FORA DO ESCOPO PI2 — implementação prevista para pós-PI3.
-- A imutabilidade da avaliação (1x por análise, sem edição) protegerá
-- a integridade do relatório publicado em versões futuras.
CREATE TABLE avaliacao_analise (
    id_avaliacao INTEGER PRIMARY KEY,
    id_analise INTEGER NOT NULL UNIQUE,
    id_usuario_cliente INTEGER NOT NULL,
    nota INTEGER NOT NULL CHECK(nota BETWEEN 1 AND 5),
    comentario TEXT,
    data_avaliacao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (id_analise) REFERENCES analise(id_analise),
    FOREIGN KEY (id_usuario_cliente) REFERENCES usuario(id_usuario)
);


-- =====================================================================
-- 12. fatura_cobranca
-- =====================================================================
-- ⚠️ FORA DO ESCOPO PI2 — implementação prevista para pós-PI3.
-- Cobrança comercial atual será controlada manualmente fora do sistema.
CREATE TABLE fatura_cobranca (
    id_fatura INTEGER PRIMARY KEY,
    id_empresa INTEGER NOT NULL,
    id_plano INTEGER NOT NULL,
    mes_referencia INTEGER NOT NULL CHECK(mes_referencia BETWEEN 1 AND 12),
    ano_referencia INTEGER NOT NULL,
    valor_cobrado NUMERIC NOT NULL,
    data_emissao DATE NOT NULL,
    data_vencimento DATE NOT NULL,
    data_pagamento DATE,
    status_fatura TEXT NOT NULL DEFAULT 'ABERTA'
        CHECK(status_fatura IN ('ABERTA', 'PAGA', 'VENCIDA', 'CANCELADA')),
    forma_pagamento TEXT
        CHECK(forma_pagamento IS NULL OR
              forma_pagamento IN ('PIX', 'CARTAO', 'BOLETO', 'OUTRO')),
    data_atualizacao DATETIME,
    UNIQUE (id_empresa, mes_referencia, ano_referencia),
    FOREIGN KEY (id_empresa) REFERENCES empresa(id_empresa),
    FOREIGN KEY (id_plano) REFERENCES plano(id_plano)
);


-- =====================================================================
-- ÍNDICES EM FOREIGN KEYS
-- =====================================================================
-- SQLite cria índice automático para PRIMARY KEY e UNIQUE,
-- mas NÃO cria para FOREIGN KEY. Sem estes, consultas como
-- "listar análises da empresa X" varrem a tabela inteira.

CREATE INDEX idx_empresa_segmento     ON empresa(id_segmento);
CREATE INDEX idx_empresa_plano_atual  ON empresa(id_plano_atual);

CREATE INDEX idx_usuario_empresa      ON usuario(id_empresa);

CREATE INDEX idx_analise_empresa      ON analise(id_empresa);
CREATE INDEX idx_analise_plano        ON analise(id_plano_referencia);
CREATE INDEX idx_analise_admin        ON analise(id_usuario_admin_responsavel);

CREATE INDEX idx_upload_analise       ON upload_relatorio(id_analise);
CREATE INDEX idx_upload_admin         ON upload_relatorio(id_usuario_admin);

CREATE INDEX idx_chamado_empresa      ON chamado_suporte(id_empresa);
CREATE INDEX idx_chamado_cliente      ON chamado_suporte(id_usuario_cliente);

CREATE INDEX idx_mensagem_chamado     ON mensagem_suporte(id_chamado);
CREATE INDEX idx_mensagem_remetente   ON mensagem_suporte(id_usuario_remetente);

CREATE INDEX idx_avaliacao_cliente    ON avaliacao_analise(id_usuario_cliente);

CREATE INDEX idx_fatura_empresa       ON fatura_cobranca(id_empresa);
CREATE INDEX idx_fatura_plano         ON fatura_cobranca(id_plano);
