# NEXO — Faturamento Inteligente

**Plataforma de Engenharia de Dados e Business Intelligence para o varejo de micro e pequenas empresas.**

| | |
|---|---|
| **Stack** | Python · Flask · SQLAlchemy 2.x · SQLite · Pandas · Bootstrap 5 · Plotly.js |
| **Status** | MVP — Projeto Integrador 2 |

O **NEXO** diagnostica descasamentos operacionais e financeiros no varejo a partir dos dados brutos exportados pelo PDV do cliente. Em vez de competir com sistemas de ERP ou contabilidade, a plataforma entrega uma camada de **inteligência sobre dados que o lojista já possui**, mas raramente consegue interpretar de forma estratégica.

---

## 📊 Conceito de Negócio & Governança de Dados

O NEXO **não é um sistema contábil**. Não calcula Lucro Real, Margem Líquida, CMV nem Estoque Físico Real — esses conceitos exigem reconciliação fiscal e contagens físicas que estão fora do escopo da plataforma.

O indicador central da plataforma é o **Indicador de Pressão de Estoque**:

```
Indicador de Pressão de Estoque = Total Comprado − Faturamento Total
```

Esse saldo estimado revela o **descasamento financeiro/operacional** entre compras e vendas do período analisado: quando positivo, indica que entrou mais mercadoria do que saiu (pressão de estoque crescente); quando negativo, o oposto. Trata-se de um sinal estratégico para a consultoria, não de um número contábil auditável.

### 🔐 Governança de Dados & Privacidade

- **Processamento estritamente em memória.** Os relatórios brutos do PDV são lidos pelo Pandas como `DataFrame`s temporários que existem **apenas durante o processamento** da análise.
- **Nenhuma linha de relatório bruto é persistida.** A base de dados nunca recebe registros transacionais do cliente (vendas item a item, fornecedor por nota etc.).
- **Apenas os KPIs consolidados** são gravados na tabela `indicador_analise`, mantendo o banco enxuto e alinhado às boas práticas de minimização de dados (LGPD).

---

## 🛠️ Arquitetura do Motor de ETL

O motor foi desenhado para absorver a imprevisibilidade dos exports de PDVs do varejo brasileiro — encodings legados, cabeçalhos quebrados em múltiplas linhas, rodapés de totais e caracteres invisíveis.

| Camada | Responsabilidade |
|---|---|
| **Leitura adaptativa** | `.xlsx` lido nativamente via `openpyxl`; `.csv`/texto cai em um pipeline sequencial de encodings (`utf-8-sig` → `cp1252` → `utf-8` → `latin1`) e delimitadores (`;`, `,`, `\t`). |
| **Sanitização de cabeçalhos** | Remove BOM (`﻿`), espaço não separável (`\xa0`), quebras de linha e colapsa espaços duplicados. Normaliza acentos para mapear sinônimos de coluna entre exports diferentes. |
| **Conversão numérica defensiva** | Parser robusto para formatos monetários `R$ 1.234,56` (pt-BR) e `1,234.56` (en-US). Valores não conversíveis falham com diagnóstico explícito (nome da coluna, arquivo, contagem e amostras), em vez de virar zero silenciosamente. |
| **Validação semântica** | Colunas obrigatórias diferenciadas por tipo de relatório (VENDAS × COMPRAS). Ausência interrompe o pipeline com erro claro listando as colunas efetivamente encontradas após sanitização. |
| **Persistência mínima** | Apenas os KPIs finais são gravados em `indicador_analise` via ORM, com versionamento incremental (`versao_processamento`) a cada reprocessamento. |

---

## 🗂️ Estrutura do Projeto

```text
nexo_mvp_semana1/
├── Docs/               # Documentação de arquitetura e DER V4
├── dados/              # Planilhas brutas de exemplo (gitignored)
├── blueprints/         # Rotas Flask separadas por contexto (auth, admin, cliente)
├── templates/          # Views Bootstrap 5 isoladas por perfil de acesso
├── app.py              # Application Factory + event listeners de FK do SQLite
├── config.py           # Configuração por ambiente (development / production)
├── extensions.py       # Instâncias compartilhadas (SQLAlchemy, Flask-Login)
├── models.py           # 8 modelos SQLAlchemy 2.x do DER V4 ativo no MVP
├── etl_processor.py    # Motor de ETL em camadas (leitura → sanitização → KPIs)
├── test_etl.py         # Homologação isolada do motor (sem Flask / sem banco)
├── init_db.py          # Cria as tabelas do zero
├── seed.py             # Popula planos, segmentos e o usuário admin master
├── requirements.txt    # Dependências do ecossistema Python
└── .gitignore          # Filtros de proteção (uploads, dados, .env, .venv)
```

---

## 🚀 Guia de Instalação Local

Compatível com **PowerShell (Windows)** e **Bash (Linux/macOS)**. Apenas as linhas de ativação do `venv` divergem por SO; o restante é idêntico.

### 1. Clonar e acessar o repositório

```bash
git clone <url-do-repositorio>
cd nexo_mvp_semana1
```

### 2. Criar e ativar o ambiente virtual

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

### 3. Instalar as dependências

```bash
pip install -r requirements.txt
```

### 4. Configurar variáveis de ambiente

```bash
cp .env.example .env
```

Abra o `.env` e defina `SECRET_KEY` (chave aleatória forte) e `ADMIN_SENHA` (credencial inicial do administrador master).

### 5. Inicializar o banco e popular dados de demonstração

```bash
python init_db.py
python seed.py
```

### 6. Homologar o motor de ETL (modo isolado)

Valida parsing, sanitização e cálculo de KPIs contra os arquivos reais — sem subir o Flask e sem persistir nada:

```powershell
python test_etl.py .\dados\Vendas_2025.xlsx .\dados\Compras_2025.xlsx 1
```

### 7. Subir o servidor web

```bash
python app.py
```

Acesse `http://127.0.0.1:5000` e autentique-se com o `ADMIN_EMAIL` e a `ADMIN_SENHA` definidos no `.env`.

---

## 🏛️ Matriz de Decisões de Engenharia

| Componente técnico | Escolha arquitetural | Justificativa de engenharia |
|---|---|---|
| **Padrão web** | Application Factory | Permite múltiplas instâncias por ambiente (dev/test/prod), facilita testes isolados e elimina imports circulares entre `app`, `models` e `extensions`. |
| **ORM** | SQLAlchemy 2.x com `Mapped` / `mapped_column` | Sintaxe moderna com *type hints* nativos; melhora autocomplete da IDE, valida tipos em tempo de desenvolvimento e é a API recomendada pela própria SQLAlchemy. |
| **Integridade referencial** | SQLite + *event listener* do SQLAlchemy executando `PRAGMA foreign_keys = ON` por conexão | O SQLite desliga FKs por padrão, e o PRAGMA escrito no `.sql` vale apenas na sessão que o executou. O listener garante FKs ativas em **toda** conexão nova, em qualquer script (app, seed, testes). |
| **Concorrência** | `PRAGMA journal_mode = WAL` | O modo *Write-Ahead Logging* permite leitores e escritor coexistirem sem bloquear, eliminando a contenção típica do SQLite em servidores Flask multi-thread. |
| **Hashing de senhas** | `werkzeug.security.generate_password_hash` | Padrão seguro (PBKDF2) embutido no Flask; suficiente para o MVP, sem dependências extras. Migração para Argon2/bcrypt fica reservada para pós-MVP, se necessária. |
| **Contrato de upload** | `UNIQUE (id_analise, tipo_relatorio)` | Restrição no nível do banco que garante exatamente um upload de `VENDAS` e um de `COMPRAS` por análise, prevenindo duplicidade e ambiguidade no ETL. |
| **Persistência do ETL** | Apenas KPIs finais via ORM (`indicador_analise`) | Mantém o banco enxuto, alinhado à LGPD e à diretriz de governança de dados. Linhas brutas vivem apenas na memória do Pandas durante o processamento. |

---

## 📄 Licença

Projeto acadêmico — direitos reservados aos autores.
