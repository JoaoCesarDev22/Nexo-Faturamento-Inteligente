# NEXO — Faturamento Inteligente

**Plataforma de Engenharia de Dados e Business Intelligence para o varejo de micro e pequenas empresas.**

| | |
|---|---|
| **Stack** | Python · Flask · SQLAlchemy 2.x · PostgreSQL (Supabase) / SQLite · Pandas · NumPy · Socket.IO · Bootstrap 5 · Plotly.js · Claude (Anthropic) |
| **Status** | Produção (TCC) — multi-tenant, com segurança hardened, tempo real e IA |

O **NEXO** diagnostica descasamentos operacionais e financeiros no varejo a partir dos dados brutos exportados pelo PDV do cliente. Em vez de competir com sistemas de ERP ou contabilidade, a plataforma entrega uma camada de **inteligência sobre dados que o lojista já possui**, mas raramente consegue interpretar de forma estratégica.

O projeto nasceu como MVP acadêmico (Projeto Integrador) e evoluiu, ao longo do desenvolvimento, para uma aplicação com arquitetura de nível de produção: migração para Postgres/Supabase, motor de ETL hardened contra *chaos engineering*, segurança (CSRF, rate limiting, anti open-redirect, revalidação de sessão), tempo real via WebSocket, devolutiva gerada por IA com *fail-safe* determinístico, exportação executiva em PDF e um assistente de suporte (NexoBot).

---

## 📊 Conceito de Negócio & Governança de Dados

O NEXO **não é um sistema contábil**. Não calcula Lucro Real, Margem Líquida, CMV nem Estoque Físico Real — esses conceitos exigem reconciliação fiscal e contagens físicas que estão fora do escopo da plataforma. Essa é uma regra de domínio **inegociável**, reforçada em todas as camadas (ETL, IA, semáforos): o sistema nunca exibe lucro, margem ou rentabilidade.

O indicador central da plataforma é o **Indicador de Pressão de Estoque**:

```
Indicador de Pressão de Estoque = Total Comprado − Faturamento Total
```

Esse saldo estimado revela o **descasamento financeiro/operacional** entre compras e vendas do período analisado: quando positivo, indica que entrou mais mercadoria do que saiu (pressão de estoque crescente); quando negativo, o oposto. Trata-se de um sinal estratégico para a consultoria, não de um número contábil auditável.

### 🔐 Governança de Dados & Privacidade

- **Sem parsing linha a linha para o banco.** O motor de ETL lê os relatórios brutos do PDV com Pandas, mas **nunca grava transações item a item** (vendas individuais, notas de compra) nas tabelas do sistema — apenas os **KPIs agregados** (`indicador_analise`) e o ranking consolidado da Curva ABC (`produto_curva_abc`).
- **Arquivos brutos** ficam retidos via `storage.py` (hoje no filesystem do servidor) apenas para permitir reprocessamento/auditoria do upload original — não são lidos novamente linha a linha pela aplicação fora do pipeline do ETL.
- **Minimização de dados (LGPD):** o banco permanece enxuto, com apenas o necessário para o diagnóstico estratégico.

---

## 🧭 Arquitetura Geral

Aplicação Flask multi-tenant (Admin/Consultoria × Cliente/Empresa) organizada em *blueprints*, com persistência em PostgreSQL (Supabase) ou SQLite, tempo real via Socket.IO e dois pipelines assíncronos (IA e e-mail) que nunca bloqueiam a requisição HTTP.

```
Cliente (browser)
   │  HTTP + WebSocket (cookie de sessão)
   ▼
Flask App Factory (app.py)
   ├── auth        — login, recuperação de senha, ativação de conta, 1º acesso
   ├── admin       — empresas, análises, ETL, lixeira, suporte, CMS do guia
   ├── cliente     — upload, histórico, análise publicada, suporte
   ├── api         — endpoints auxiliares (ex.: dados para os gráficos Plotly)
   └── notificacoes— sininho (marcar lidas / abrir)
          │
          ├── etl_processor.py   → Pandas/NumPy: leitura adaptativa + KPIs + Curva ABC
          ├── insights.py        → Semáforos 5W2H derivados dos KPIs
          ├── ai_devolutiva.py   → Claude (Anthropic) com fallback determinístico
          ├── pdf_export.py      → ReportLab: PDF executivo da análise publicada
          ├── suporte_bot.py     → NexoBot (Hugging Face + fallback por keywords)
          ├── realtime.py        → Salas Socket.IO por usuário (user_<id>)
          ├── notifications.py / emails.py → disparados SEMPRE depois do commit
          └── storage.py         → único ponto de I/O de arquivo (swap futuro p/ S3)
          │
          ▼
   SQLAlchemy 2.x (Mapped/mapped_column) — Flask-Migrate/Alembic
          │
          ▼
   PostgreSQL/Supabase (produção) — pooled (6543) p/ runtime, direto (5432) p/ migrações
   ou SQLite (dev local rápido)
```

Para o detalhamento completo da arquitetura (incluindo diagrama Mermaid do fluxo de dados e o DER com as 13 entidades), ver **[`docs/RELATORIO_TECNICO.md`](docs/RELATORIO_TECNICO.md)**.

---

## ✅ Funcionalidades Principais

**Núcleo de negócio**
- Cadastro multi-empresa/multi-usuário (Admin/Consultoria gerencia N empresas-cliente).
- Upload de relatórios VENDAS + COMPRAS (`.csv`/`.xlsx`) por análise, com motor de ETL resiliente (ver seção dedicada abaixo).
- Indicador de Pressão de Estoque, Curva ABC (Pareto) vetorizada com NumPy, semáforos executivos (5W2H).
- Devolutiva consultiva: gerada por IA (Claude) a partir dos KPIs reais, com fallback determinístico local quando não há `ANTHROPIC_API_KEY` — o fluxo **nunca quebra** por falta de IA.
- Exportação da análise publicada em **PDF executivo** (ReportLab).
- Fluxo de publicação com rascunho → homologação → publicação, visível ao cliente apenas após publicado.

**Operação e ciclo de vida**
- **Sistema de Lixeira (Soft Delete + Hard Delete):** exclusão de empresa é reversível (`deletado_em`) até a exclusão permanente explícita, que então **cascade-deleta** análises, uploads, indicadores, relatórios e Curva ABC via ORM (`cascade="all, delete-orphan"`).
- Primeiro acesso com senha temporária + ativação de conta por token assinado (`itsdangerous`, expira em 15 min).
- Recuperação de senha por e-mail (mesmo mecanismo de token assinado).

**Tempo real e suporte**
- Notificações via WebSocket (Socket.IO) em sala exclusiva por usuário — sininho da navbar, sem broadcast aberto.
- **NexoBot:** assistente de suporte com base de conhecimento dinâmica (CMS `guia_topico`, editável pelo Admin), IA opcional via Hugging Face com fallback determinístico por palavras-chave — nunca fica mudo, e auditoria de saída bloqueia respostas vazias/fora de escopo.
- Central de chamados de suporte (Admin ↔ Cliente).

**Segurança (hardened)**
- CSRF global (Flask-WTF) em todas as rotas mutáveis.
- Rate limiting (Flask-Limiter) em login e recuperação de senha.
- Proteção anti open-redirect em todos os redirecionamentos que aceitam parâmetro externo (`next`, `referrer`, `link_destino`).
- Sessão amarrada ao `BOOT_ID` do processo (restart do servidor invalida sessões antigas) + revalidação de `is_active` a cada request (usuário desativado é deslogado imediatamente).
- Origens de WebSocket restritas (`SOCKETIO_CORS_ORIGINS`) — mitiga Cross-Site WebSocket Hijacking.
- Debug do Werkzeug bloqueado fora de `development` (recusa explícita a subir com `DEBUG=True` em produção).
- Erros de banco nunca vazam para o usuário (mensagens genéricas + log interno via `current_app.logger`).
- Lock otimista (`with_for_update`) no reprocessamento de uma análise, evitando corrida entre dois disparos simultâneos do ETL.
- Notificações/e-mails sempre disparados **depois** do `commit()` (dispatch-after-commit) — nunca há notificação fantasma de uma transação que sofreu rollback.

---

## 🛠️ Motor de ETL — Resiliência e Cobertura

O motor (`etl_processor.py`) foi submetido a uma bateria de *chaos engineering* com layouts reais de PDV/ERP brasileiros (encodings legados, cabeçalhos quebrados, planilhas com abas de capa, separadores variados, colunas deslocadas, linhas irregulares) e segue um contrato de robustez explícito:

> **Para qualquer arquivo recebido, o motor garante um de dois desfechos — nunca um terceiro:** sucesso honesto (extrai e consolida os KPIs reais) ou recusa acionável (diagnóstico claro do que faltou). **Jamais** uma queda do servidor ou uma análise calculada sobre dados inventados.

Destaques técnicos:
- Leitura adaptativa: múltiplos *encodings* (`utf-8-sig`, `cp1252`, `latin-1`), múltiplos separadores (`;`, `,`, `\t`, `|`), `.xlsx` multi-aba (escolhe a aba com mais dados) e `.xls`.
- Mapeamento de colunas por *aliases* (independe de nome exato/posição), com resolução determinística do conflito `id_produto` × nome do produto.
- **Derivação honesta** de totais ausentes (ex.: `quantidade × preço unitário`) — uma operação aritmética sobre dados reais, nunca fabricação de coluna.
- Curva ABC vetorizada com NumPy (`np.select`), sem `iterrows()` no caminho de cálculo.
- Rede de segurança final em `processar_arquivos_analise`: qualquer exceção inesperada é capturada e convertida em falha segura, isolada da requisição web.

Cobertura validada (6 camadas de falha, 8 layouts reais, 11+ cenários adversariais) e as decisões deliberadas de escopo (o que **não** foi implementado e por quê) estão documentadas em **[`docs/RELATORIO_COBERTURA_ETL.md`](docs/RELATORIO_COBERTURA_ETL.md)**.

---

## 🗂️ Estrutura do Projeto

```text
nexo_mvp/
├── docs/                    # Relatório técnico + relatório de cobertura/resiliência do ETL (Mermaid)
├── migrations/              # Versionamento de schema (Flask-Migrate/Alembic)
├── blueprints/               # Rotas Flask por contexto
│   ├── auth.py               #   login, ativação de conta, recuperação de senha
│   ├── admin.py               #   empresas, análises, ETL, lixeira, suporte, CMS
│   ├── cliente.py             #   upload, histórico, análise publicada
│   ├── api.py                 #   endpoints auxiliares (gráficos)
│   └── notificacoes.py         #   sininho (marcar lidas / abrir)
├── templates/                # Views Bootstrap 5, isoladas por perfil (admin/cliente)
├── app.py                    # Application Factory + entrypoint (socketio.run)
├── config.py                 # Configuração por ambiente (Config / Dev / Production)
├── extensions.py             # Instâncias compartilhadas (db, login, mail, socketio, csrf, limiter)
├── models.py                 # 13 modelos SQLAlchemy 2.x (Mapped/mapped_column)
├── etl_processor.py          # Motor de ETL (leitura adaptativa → sanitização → KPIs → Curva ABC)
├── insights.py                # Semáforos executivos 5W2H
├── ai_devolutiva.py            # Devolutiva por IA (Claude) + fallback determinístico
├── suporte_bot.py              # NexoBot (Hugging Face + fallback por keywords)
├── pdf_export.py               # Exportação executiva em PDF (ReportLab)
├── realtime.py                 # Salas Socket.IO por usuário
├── notifications.py / emails.py # Disparo de notificações/e-mails (dispatch-after-commit)
├── storage.py                  # Ponto único de I/O de arquivo (uploads)
├── test_etl.py                 # Homologação isolada do motor de ETL
├── init_db.py                  # Cria as tabelas do zero (uso: dev local com SQLite)
├── seed.py                     # Popula planos, segmentos e o usuário admin master
├── seed_guia.py                  # Popula a base de conhecimento do NexoBot/Guia
├── criar_cliente_demo.py         # Cria/reseta um cliente de demonstração (fluxo 1º acesso)
├── requirements.txt            # Dependências do ecossistema Python
├── .env.example                 # Template de variáveis de ambiente
└── .gitignore                   # Filtros de proteção (uploads, dados, .env, .venv)
```

---

## 🧰 Stack Tecnológica

| Camada | Tecnologia |
|---|---|
| Web framework | Flask 3.x (Application Factory + Blueprints) |
| ORM / Migrações | SQLAlchemy 2.x (`Mapped`/`mapped_column`) + Flask-Migrate/Alembic |
| Banco de dados | PostgreSQL via Supabase (produção/homologação) ou SQLite (dev local) |
| Processamento de dados | Pandas + NumPy (ETL vetorizado) |
| Autenticação | Flask-Login + `werkzeug.security` (hash de senha) + `itsdangerous` (tokens assinados) |
| Segurança | Flask-WTF (CSRF) + Flask-Limiter (rate limiting) |
| Tempo real | Flask-SocketIO (`async_mode="threading"`) + `simple-websocket` |
| E-mail | Flask-Mail (SMTP) |
| PDF | ReportLab (platypus) |
| IA — devolutiva | SDK oficial `anthropic` (Claude) |
| IA — suporte | Hugging Face Inference API (opcional) |
| Front-end | Bootstrap 5 + Plotly.js (via CDN) |

Lista completa e versões exatas em [`requirements.txt`](requirements.txt).

---

## 🚀 Guia de Instalação e Execução (passo a passo)

Este guia cobre a configuração do zero em **qualquer computador** (Windows/Linux/macOS). Compatível com **PowerShell** e **Bash** — apenas a ativação do `venv` diverge por SO.

### 1. Pré-requisitos

- **Python 3.12+** instalado e no `PATH`.
- **Git**.
- Uma conta **Supabase** (gratuita) se for usar PostgreSQL em nuvem — ou pule essa parte e use **SQLite local** para rodar o sistema rapidamente sem depender de nuvem.

### 2. Clonar o repositório

```bash
git clone <url-do-repositorio>
cd nexo_mvp
```

### 3. Criar e ativar o ambiente virtual

```bash
python -m venv .venv
```

```powershell
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

### 4. Instalar as dependências

```bash
pip install -r requirements.txt
```

### 5. Configurar as variáveis de ambiente

```bash
cp .env.example .env
```

Abra o `.env` e preencha pelo menos:
- `SECRET_KEY` — string aleatória forte (`python -c "import secrets; print(secrets.token_hex(32))"`).
- `ADMIN_EMAIL` / `ADMIN_NOME` / `ADMIN_SENHA` — credencial inicial do administrador master.

Todo o resto é **opcional com fail-safe**: sem `ANTHROPIC_API_KEY` a devolutiva usa o gerador local; sem `HF_API_TOKEN` o NexoBot usa o fallback por palavras-chave; sem `MAIL_USERNAME`/`MAIL_PASSWORD` o envio de e-mail é apenas suprimido (logado, sem quebrar o fluxo). Veja a tabela completa de variáveis abaixo.

### 6. Escolher e preparar o banco de dados

**Opção A — SQLite local (mais rápido para testar)**

Deixe `DATABASE_URL` comentado/ausente no `.env` (ou aponte para `sqlite:///instance/nexo_mvp.db`) e rode:

```bash
python init_db.py
```

Isso cria o schema do zero localmente (não use em produção — é destrutivo).

**Opção B — PostgreSQL/Supabase (recomendado para produção/homologação)**

1. Crie um projeto no [Supabase](https://supabase.com) e copie a *Connection String* **pooled** (porta `6543`) para `DATABASE_URL` e a **direta** (porta `5432`) para `DIRECT_URL` no `.env`.
2. Aplique as migrações usando a conexão **direta** (DDL não passa pelo PgBouncer):

```bash
# Windows (PowerShell)
$env:NEXO_DB_DIRECT="1"; flask db upgrade
```

```bash
# Linux / macOS
NEXO_DB_DIRECT=1 flask db upgrade
```

### 7. Popular dados iniciais (seed)

```bash
python seed.py
```

Cria os planos, segmentos e o usuário ADMIN master a partir das credenciais do `.env`. É idempotente — pode rodar de novo sem duplicar.

Para popular a base de conhecimento do NexoBot/Guia (use `NEXO_DB_DIRECT=1` se estiver em Postgres):

```bash
python seed_guia.py
```

*(Opcional)* para criar um cliente de demonstração com o fluxo de primeiro acesso já armado:

```bash
python criar_cliente_demo.py
```

### 8. (Opcional) Homologar o motor de ETL isoladamente

Valida parsing, sanitização e cálculo de KPIs contra arquivos reais — sem subir o Flask:

```bash
python test_etl.py .\dados\Vendas_2025.xlsx .\dados\Compras_2025.xlsx 1
```

### 9. Subir o servidor

```bash
python app.py
```

Acesse `http://127.0.0.1:5000` e autentique-se com o `ADMIN_EMAIL`/`ADMIN_SENHA` definidos no `.env`. O servidor sobe com suporte a WebSocket (`socketio.run`) — para produção, sirva por trás de um servidor compatível com WebSocket (ex.: gunicorn com worker `eventlet`/`gevent`, nunca o servidor de desenvolvimento do Werkzeug).

> Variáveis adicionais de runtime: `HOST` (default `127.0.0.1`) e `PORT` (default `5000`) sobrescrevem o bind do servidor; `FLASK_ENV` controla `development`/`production` (em `production`, `DEBUG` é forçado a `False` e o app se recusa a subir com debugger ativo).

---

## ⚙️ Variáveis de Ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `FLASK_ENV` | Não (default `development`) | `development` ou `production`. |
| `SECRET_KEY` | **Sim em produção** | Chave de assinatura de sessão/CSRF/tokens. |
| `DATABASE_URL` | Não (default SQLite local) | Conexão de runtime (pooled, 6543, se Supabase). |
| `DIRECT_URL` | Só para migrações em Postgres | Conexão direta (5432) usada por `flask db upgrade`. |
| `NEXO_DB_DIRECT` | Não | `1` para forçar o uso da conexão direta (DDL/migrações). |
| `ADMIN_EMAIL` / `ADMIN_NOME` / `ADMIN_SENHA` | Sim (lidas pelo `seed.py`) | Credencial do administrador master inicial. |
| `UPLOAD_FOLDER` / `MAX_CONTENT_LENGTH_MB` | Não | Diretório e limite de tamanho dos uploads. |
| `SOCKETIO_CORS_ORIGINS` | Não | Origens permitidas no handshake WebSocket (CSV). |
| `RATELIMIT_STORAGE_URI` | Não (default `memory://`) | Backend do rate limiting; use Redis em multi-instância. |
| `MAIL_SERVER` / `MAIL_PORT` / `MAIL_USE_TLS` / `MAIL_USE_SSL` / `MAIL_USERNAME` / `MAIL_PASSWORD` / `MAIL_DEFAULT_SENDER` | Não (fail-safe) | SMTP para e-mails de ativação/recuperação/publicação. Sem credenciais, o envio é suprimido. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Não (fail-safe) | Geração da devolutiva por IA. Sem chave, usa o fallback determinístico. |
| `HF_API_TOKEN` / `HF_MODEL` | Não (fail-safe) | NexoBot via Hugging Face. Sem token, usa o fallback por palavras-chave. |
| `HOST` / `PORT` | Não | Bind do servidor (`python app.py`). |

Template completo em [`.env.example`](.env.example).

---

## 🏛️ Matriz de Decisões de Engenharia

| Componente técnico | Escolha arquitetural | Justificativa de engenharia |
|---|---|---|
| **Padrão web** | Application Factory | Múltiplas instâncias por ambiente, testes isolados, sem imports circulares entre `app`, `models` e `extensions`. |
| **ORM** | SQLAlchemy 2.x com `Mapped`/`mapped_column` | Sintaxe moderna com *type hints* nativos; valida tipos em desenvolvimento e é a API recomendada atual. |
| **Banco** | PostgreSQL (Supabase) em produção, SQLite em dev | Postgres dá concorrência real e suporta múltiplas instâncias; SQLite permite rodar o projeto sem nenhuma dependência externa. |
| **Pool de conexão** | `pool_pre_ping=True` + `pool_recycle=280` | O PgBouncer do Supabase (modo *transaction pooling*) fecha conexões ociosas; a validação prévia e o reciclo evitam erros de conexão "morta". |
| **Exclusão em cascata** | Cascade no **ORM** (`cascade="all, delete-orphan"`), não `ON DELETE CASCADE` no banco | Evita depender de `passive_deletes` (que remove a rede de segurança das FKs) e de uma migração de schema de última hora; o controle de exclusão fica explícito no código Python. |
| **Lixeira (exclusão de empresa)** | Soft Delete (`deletado_em`) + Hard Delete explícito | Unifica reversibilidade operacional com a necessidade ocasional de expurgo definitivo, sem dois sistemas paralelos. |
| **Segurança de sessão** | Cookie amarrado a `BOOT_ID` + revalidação de `is_active` por request | Garante que reiniciar o servidor invalida sessões antigas e que desativar um usuário o desloga imediatamente, sem esperar expiração de cookie. |
| **CSRF/Rate limit** | Flask-WTF + Flask-Limiter | Mitigam CSRF em todas as rotas mutáveis e *brute force* em login/recuperação de senha, com mudança mínima de código (decorators). |
| **Tempo real** | Socket.IO com `cors_allowed_origins` restrito + sala por usuário | Mitiga Cross-Site WebSocket Hijacking e garante que notificações nunca sejam recebidas por outro usuário (sem broadcast aberto). |
| **Disparo de notificação/e-mail** | Sempre **depois** do `commit()` (dispatch-after-commit) | Evita notificações fantasmas referentes a uma transação que sofreu rollback. |
| **Curva ABC** | Vetorizada com NumPy (`np.select`) | Performance em catálogos grandes, sem `iterrows()` no caminho crítico de cálculo. |
| **Resiliência do ETL** | Contrato de robustez (sucesso honesto **ou** recusa acionável, nunca fabricação de dado) | Evita o pior cenário de produção: uma análise "bem-sucedida" calculada sobre dados inventados. Ver [`docs/RELATORIO_COBERTURA_ETL.md`](docs/RELATORIO_COBERTURA_ETL.md). |
| **Armazenamento de upload** | Abstração única em `storage.py` | Ponto de troca único para migrar do filesystem local para *object storage* (Supabase Storage/S3) sem tocar nas rotas. |
| **IA (devolutiva e suporte)** | Sempre com *fallback* determinístico local | O sistema nunca depende de uma API externa para funcionar — IA é um realce, não um requisito. |
| **Persistência do ETL** | Apenas KPIs e ranking consolidados via ORM | Mantém o banco enxuto, alinhado à LGPD; dados transacionais brutos não são parseados linha a linha para tabelas do sistema. |

---

## 📚 Documentação Complementar

- **[`docs/RELATORIO_TECNICO.md`](docs/RELATORIO_TECNICO.md)** — relatório técnico completo (arquitetura, tecnologias, funcionalidades) com diagrama Mermaid de arquitetura/fluxo de dados e DER das 13 entidades. Referência principal para a defesa do TCC.
- **[`docs/RELATORIO_COBERTURA_ETL.md`](docs/RELATORIO_COBERTURA_ETL.md)** — contrato de robustez do motor de ETL, taxonomia de falhas em 6 camadas, matriz de testes (layouts reais + *chaos engineering*) e decisões deliberadas de escopo.

---

## 📄 Licença

Projeto acadêmico — direitos reservados aos autores.
