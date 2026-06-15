"""
NEXO - Faturamento Inteligente | Motor ETL UNIVERSAL (v3)
==========================================================
Processa relatórios de PDV (Vendas + Compras) com mapeamento DINÂMICO de
cabeçalhos: independe da posição das colunas, do nome exato, do tamanho da
janela temporal (15 dias, mês, trimestre, ano ou histórico inteiro) e
tolera sujeira (linhas decorativas, quebras de página, totalizações,
células e linhas em branco entre os registros).

Estratégia (4 camadas):
  1) Leitura crua: lê o arquivo SEM assumir cabeçalho (header=None,
     dtype=str), preservando a matriz como string para a varredura.
  2) Detecção de cabeçalho: varre as primeiras 60 linhas e procura a
     linha (ou 2 linhas adjacentes, no caso de cabeçalho composto) que
     MELHOR mapeia os aliases canônicos via substring normalizada.
  3) DataFrame canônico: a partir da 1ª linha de dados, monta um DF só
     com as colunas reconhecidas, renomeadas para nomes canônicos.
  4) Filtro de lixo: descarta linhas onde a coluna-chave é nula ou
     contém marcadores de rodapé/cabeçalho repetido (totais, "Movimenta",
     marca d'água "LabSofti" etc.).

Regras inegociáveis (MVP/PI2):
  - DataFrames vivem APENAS em memória; só os KPIs finais são persistidos.
  - O indicador de descasamento é "Indicador de Pressão de Estoque"
    (saldo estimado entre compras e vendas). NUNCA expor lucro, margem,
    CMV, prejuízo, rentabilidade ou estoque real como KPI.
  - 'Lucro' e 'Total Custo' do PDV são DETECTADOS para mapeamento
    defensivo (não confundem com outras colunas) e DESCARTADOS para fins
    de KPI. 'Total Custo' pode gerar APENAS o alerta "venda abaixo do
    custo informado pelo PDV" — observação do dado bruto, NÃO margem.
  - Falha de leitura/validação nunca é mascarada com dados falsos.
  - Status CONCLUIDO nunca é setado aqui — é decisão humana via admin.

Assinatura imutável:
  processar_arquivos_analise(caminho_vendas, caminho_compras,
                             id_analise, db_session) -> ResultadoETL
"""

import os
import re
import logging
import unicodedata
from datetime import datetime
from collections import namedtuple

import pandas as pd
from sqlalchemy import select

from models import IndicadorAnalise, ProdutoCurvaABC

logger = logging.getLogger(__name__)

ResultadoETL = namedtuple(
    "ResultadoETL", ["sucesso", "mensagem", "kpis", "telemetria", "alertas"]
)

# Quantos produtos do topo do ranking persistir/exibir na Curva ABC.
TOP_N_CURVA_ABC = 20

# Limiares do Princípio de Pareto (Curva ABC) sobre o faturamento ACUMULADO:
#   Classe A (Estratégicos): até 80%
#   Classe B (Táticos):      de 80% a 95%
#   Classe C (Operacionais): os ~5% restantes
ABC_LIMITE_A = 80.0
ABC_LIMITE_B = 95.0


class ETLValidationError(Exception):
    """Erro de validação de dados/arquivo. Mensagem é segura para exibir ao admin."""


# Encodings e separadores tentados em sequência para arquivos texto/CSV
ENCODINGS = ("utf-8-sig", "cp1252", "utf-8", "latin1")
SEPARADORES = (";", ",", "\t")


# =====================================================================
# 1) Sanitização de strings
# =====================================================================
def _strip_acentos(texto) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(texto))
        if not unicodedata.combining(c)
    )


def _norm_chave(valor) -> str:
    """
    Normalização AGRESSIVA para casar fragmentos do dicionário de aliases:
      - remove BOM/NBSP/quebras de linha;
      - tira acentos; vira minúsculas;
      - substitui pontuação por espaço; colapsa múltiplos espaços.
    Resultado é seguro para `fragmento in chave_normalizada`.
    """
    if valor is None:
        return ""
    s = str(valor).replace("﻿", "").replace("\xa0", " ")
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    s = _strip_acentos(s).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalizar_produto(valor) -> str:
    """Chave de agrupamento por produto (uppercase, sem acentos, sem pontuação)."""
    s = _strip_acentos(str(valor or "")).upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Linhas que NÃO são produto e contaminam rankings/Curva ABC quando o PDV as
# exporta como itens (ex.: "Descontos:" aparecendo como 50% do faturamento).
# Comparadas contra o nome JÁ normalizado por _normalizar_produto (uppercase,
# sem acento/pontuação). Match exato para não derrubar produtos reais.
_NAO_PRODUTOS = {
    "DESCONTO", "DESCONTOS", "ACRESCIMO", "ACRESCIMOS", "FRETE", "TROCO",
    "TAXA", "TAXAS", "JUROS", "TOTAL", "SUBTOTAL", "TOTAL GERAL",
    "ARREDONDAMENTO", "ENTRADA", "SANGRIA", "SUPRIMENTO",
}


# =====================================================================
# 2) Conversão numérica robusta (pt-BR e en-US)
# =====================================================================
def _parse_valor(bruto):
    """
    Aceita 'R$ 1.234,56', '1.234,56', '1234,56', '1,234.56', '1234.56',
    '(123,45)', ' - ', '', None — retorna float ou None se irrecuperável.
    """
    if bruto is None:
        return None
    s = str(bruto).strip()
    if s.lower() in ("", "nan", "none", "-", "--"):
        return None

    s = s.replace("\xa0", "").replace(" ", "")
    s = re.sub(r"(?i)r\$", "", s)
    negativo = s.startswith("-") or s.startswith("(")
    s2 = re.sub(r"[^0-9.,]", "", s)
    if s2 == "":
        return None

    tem_virg = "," in s2
    tem_pto = "." in s2
    if tem_virg and tem_pto:
        # o separador mais à direita é o decimal
        if s2.rfind(",") > s2.rfind("."):
            s2 = s2.replace(".", "").replace(",", ".")
        else:
            s2 = s2.replace(",", "")
    elif tem_virg:
        # padrão BR: vírgula é decimal
        s2 = s2.replace(".", "").replace(",", ".")
    elif tem_pto and s2.count(".") > 1:
        # múltiplos pontos => separadores de milhar
        s2 = s2.replace(".", "")
    # único ponto = decimal => mantém

    try:
        val = float(s2)
    except ValueError:
        return None
    return -val if (negativo and val > 0) else val


def _converter_serie(serie: pd.Series) -> pd.Series:
    return pd.Series([_parse_valor(v) for v in serie], index=serie.index, dtype="float64")


# =====================================================================
# 3) Leitura defensiva (.xlsx / .csv) sem assumir cabeçalho
# =====================================================================
def _ler_bruto(caminho_arquivo: str) -> pd.DataFrame:
    """Lê o arquivo como matriz crua (header=None, dtype=str)."""
    if not os.path.exists(caminho_arquivo):
        raise ETLValidationError(f"Arquivo não encontrado: {caminho_arquivo}")

    ext = os.path.splitext(caminho_arquivo)[1].lower()
    nome = os.path.basename(caminho_arquivo)

    if ext == ".xlsx":
        try:
            return pd.read_excel(caminho_arquivo, dtype=str, header=None, engine="openpyxl")
        except Exception as e:
            raise ETLValidationError(
                f"Falha ao ler .xlsx '{nome}' (openpyxl). Erro: {e}"
            )

    if ext == ".xls":
        try:
            import xlrd  # noqa: F401
        except ImportError:
            raise ETLValidationError(
                f"Formato .xls não suportado (dependência 'xlrd' ausente). "
                f"Converta '{nome}' para .xlsx ou .csv."
            )
        try:
            return pd.read_excel(caminho_arquivo, dtype=str, header=None, engine="xlrd")
        except Exception as e:
            raise ETLValidationError(f"Falha ao ler .xls '{nome}': {e}")

    # CSV/TXT/extensão desconhecida: fallback de encoding + separador
    ultimo_erro = None
    for enc in ENCODINGS:
        for sep in SEPARADORES:
            try:
                df = pd.read_csv(
                    caminho_arquivo, dtype=str, header=None,
                    sep=sep, encoding=enc, engine="python",
                )
                if df.shape[1] > 1:
                    return df
            except Exception as e:
                ultimo_erro = e

    # Último recurso: ignora bytes inválidos
    for sep in SEPARADORES:
        try:
            logger.warning(
                "Leitura de '%s' com encoding_errors='ignore' (último recurso). Sep=%r", nome, sep
            )
            df = pd.read_csv(
                caminho_arquivo, dtype=str, header=None, sep=sep,
                encoding="utf-8", engine="python", encoding_errors="ignore",
            )
            if df.shape[1] > 1:
                return df
        except Exception as e:
            ultimo_erro = e

    raise ETLValidationError(
        "Não foi possível ler o arquivo.\n"
        f"  Arquivo: {nome}\n"
        f"  Extensão: {ext or '(sem)'}\n"
        f"  Encodings testados: {list(ENCODINGS)}\n"
        f"  Separadores testados: {[repr(s) for s in SEPARADORES]}\n"
        f"  Erro original: {ultimo_erro}"
    )


# =====================================================================
# 4) Dicionário de aliases — mapeamento DINÂMICO por substring
# =====================================================================
# IMPORTANTE: a ORDEM dos canônicos importa. Fragmentos mais ESPECÍFICOS
# devem vir ANTES de fragmentos mais GENÉRICOS, para evitar colisões
# (ex.: "Qtde Estoque" cair em quantidade, "Vr Total Produtos" cair em
# valor_nf). Todos os fragmentos estão escritos JÁ na forma normalizada
# (sem acento, minúsculas, sem pontuação, sem espaços extras).

ALIAS_VENDAS = {
    "codigo":      ["codigo", "cod produto", "cod barras", "cod "],
    "produto":     ["nome do produto", "descricao do produto", "produto", "descricao"],
    # estoque ANTES de quantidade — "Qtde Estoque" não pode cair em qtd
    "estoque":     ["estoque atual", "saldo estoque", "saldo atual", "estoque"],
    # custo ANTES de venda — só por consistência
    "valor_custo": ["total custo", "vr custo", "vlr custo", "valor custo",
                    "custo total", "custo"],
    "valor_venda": ["total venda", "total vendas", "vr venda", "vlr venda",
                    "valor total venda", "valor venda"],
    "quantidade":  ["qtde vend", "qtde vendida", "quantidade vendida",
                    "qtd vend", "qtde", "quantidade", "qtd"],
    # 'lucro' é DETECTADO defensivamente (reservar a coluna) e DESCARTADO
    # no cálculo. NEXO/PI2 NÃO usa lucro/CMV como KPI — terminologia oficial.
    "lucro":       ["lucro"],
}

ALIAS_COMPRAS = {
    "data":           ["data emissao", "data nf", "data compra", "data"],
    "fornecedor":     ["razao social fornecedor", "nome fornecedor", "fornecedor"],
    # produtos_total ANTES de valor_nf — "Vr Total Produtos" é mais específico
    "produtos_total": ["total produtos", "vr total produtos", "vr produtos",
                       "valor produtos"],
    "valor_nf":       ["valor nf", "vr total", "total nf", "valor total",
                       "valor nota", "vr nota"],
    # Colunas item-a-item (raras em relatórios NF-level reais):
    "produto":        ["nome do produto", "descricao", "produto"],
    "qtde_comprada":  ["qtde comprada", "quantidade comprada", "qtde compra", "qtde"],
}


def _mapear_cabecalho(linha_norm: list, aliases: dict) -> dict:
    """
    Para cada COLUNA da linha (já normalizada), encontra o 1º canônico cujo
    fragmento aparece como substring. Cada canônico é claimed no máximo 1x
    (1ª coluna vence — proteção contra duplicatas no relatório).
    """
    mapa: dict = {}
    for idx, chave in enumerate(linha_norm):
        if not chave:
            continue
        for canonico, fragmentos in aliases.items():
            if canonico in mapa:
                continue
            for frag in fragmentos:
                if frag in chave:
                    mapa[canonico] = idx
                    break
            if canonico in mapa and mapa[canonico] == idx:
                break
    return mapa


def _detectar_cabecalho(df_bruto: pd.DataFrame, aliases: dict, valida_fn,
                        janela: int = 60):
    """
    Localiza a linha de cabeçalho na janela inicial:
      1) Cabeçalho de UMA linha — escolhe a que mapeia MAIS canônicos
         entre as que passam por `valida_fn` (cobertura mínima).
      2) Cabeçalho COMPOSTO (2 linhas adjacentes concatenadas) — fallback
         para PDVs que quebram títulos em duas linhas.
    Retorna (idx_cabecalho, mapa_canonico, linhas_cabecalho).
    """
    limite = min(janela, len(df_bruto))
    melhor = None

    # 1) cabeçalho de 1 linha
    for i in range(limite):
        linha = [_norm_chave(v) for v in df_bruto.iloc[i].tolist()]
        mapa = _mapear_cabecalho(linha, aliases)
        if valida_fn(mapa):
            if melhor is None or len(mapa) > len(melhor[1]):
                melhor = (i, mapa, 1)
    if melhor is not None:
        return melhor

    # 2) cabeçalho composto (2 linhas)
    for i in range(limite - 1):
        l1 = [_norm_chave(v) for v in df_bruto.iloc[i].tolist()]
        l2 = [_norm_chave(v) for v in df_bruto.iloc[i + 1].tolist()]
        # zip por menor comprimento — colunas extras ficam de fora
        comb = [(a + " " + b).strip() for a, b in zip(l1, l2)]
        mapa = _mapear_cabecalho(comb, aliases)
        if valida_fn(mapa):
            if melhor is None or len(mapa) > len(melhor[1]):
                melhor = (i, mapa, 2)

    if melhor is None:
        raise ETLValidationError(
            "Não foi possível localizar a linha de cabeçalho nas "
            f"primeiras {limite} linhas. Verifique se o relatório foi exportado "
            "em formato tabular do PDV (com cabeçalho identificável)."
        )
    return melhor


# =====================================================================
# 5) DataFrame canônico + limpeza de linhas-lixo
# =====================================================================
# Linhas-rodapé / marca d'água típicas de PDV (regex case-insensitive).
_PADRAO_LIXO = re.compile(
    r"\b(?:total geral|totais|subtotal|movimenta|labsofti|software|"
    r"resumo|periodo|usuario|filtro|impresso|pagina)\b",
    re.IGNORECASE,
)

# Strings que, se aparecerem como valor da coluna-chave, indicam que aquela
# linha é um cabeçalho REPETIDO (quebra de página do PDV) — devem ser puladas.
_CABECALHOS_REPETIDOS = {
    "codigo", "produto", "nome do produto", "descricao",
    "data", "data emissao", "data nf", "fornecedor",
    "valor nf", "vr total", "total nf",
}


def _construir_df_canonico(df_bruto: pd.DataFrame, idx_cab: int,
                           mapa: dict, linhas_cab: int) -> pd.DataFrame:
    """Recorta a partir da 1ª linha de dados e renomeia para nomes canônicos."""
    inicio = idx_cab + linhas_cab
    if inicio >= len(df_bruto):
        raise ETLValidationError("Cabeçalho localizado, mas sem linhas de dados após ele.")
    bloco = df_bruto.iloc[inicio:].reset_index(drop=True)
    out = pd.DataFrame(index=bloco.index)
    for nome_canonico, col_idx in mapa.items():
        if col_idx < bloco.shape[1]:
            out[nome_canonico] = bloco.iloc[:, col_idx].values
        else:
            out[nome_canonico] = pd.Series([None] * len(bloco))
    return out


def _filtrar_linhas_validas(df: pd.DataFrame, chave: str) -> pd.DataFrame:
    """
    Descarta linhas onde a coluna-chave está vazia, é rodapé/marca d'água, OU
    é um cabeçalho reimpresso (quebra de página típica do PDV).
    """
    if chave not in df.columns:
        raise ETLValidationError(f"Coluna-chave '{chave}' ausente após o mapeamento.")
    bruto = df[chave].map(lambda v: "" if v is None else str(v).strip())
    valido = bruto.map(lambda v: v.lower() not in ("", "nan", "none"))
    nao_rodape = ~bruto.str.contains(_PADRAO_LIXO, na=False)
    nao_cabec = ~bruto.map(_norm_chave).isin(_CABECALHOS_REPETIDOS)
    return df[valido & nao_rodape & nao_cabec].reset_index(drop=True)


# =====================================================================
# 6) Carregadores específicos (Vendas / Compras)
# =====================================================================
def _validar_vendas(mapa: dict) -> bool:
    return all(k in mapa for k in ("produto", "quantidade", "valor_venda"))


def _validar_compras(mapa: dict) -> bool:
    if "fornecedor" not in mapa:
        return False
    return "valor_nf" in mapa or "produtos_total" in mapa


def carregar_vendas(caminho: str) -> pd.DataFrame:
    nome = os.path.basename(caminho)
    bruto = _ler_bruto(caminho)
    if bruto.empty:
        raise ETLValidationError(f"O arquivo de vendas '{nome}' está totalmente vazio.")

    idx, mapa, linhas_cab = _detectar_cabecalho(bruto, ALIAS_VENDAS, _validar_vendas)
    df = _construir_df_canonico(bruto, idx, mapa, linhas_cab)
    df = _filtrar_linhas_validas(df, "produto")

    # Conversões numéricas — NaN vira 0 (linha sem qtd/valor não impacta KPI)
    df["quantidade"] = _converter_serie(df["quantidade"]).fillna(0.0)
    df["valor_venda"] = _converter_serie(df["valor_venda"]).fillna(0.0)
    if "valor_custo" in df.columns:
        df["valor_custo"] = _converter_serie(df["valor_custo"]).fillna(0.0)
    if "estoque" in df.columns:
        df["estoque"] = _converter_serie(df["estoque"]).fillna(0.0)

    # 'lucro' é DETECTADO e DESCARTADO — terminologia NEXO proíbe usar.
    if "lucro" in df.columns:
        df = df.drop(columns=["lucro"])

    df["_prod_norm"] = df["produto"].map(_normalizar_produto)
    # Remove vazios e linhas que não são produto (descontos, frete, troco...),
    # que distorceriam o ranking de faturamento e a Curva ABC.
    df = df[(df["_prod_norm"] != "") & (~df["_prod_norm"].isin(_NAO_PRODUTOS))].reset_index(drop=True)
    if df.empty:
        raise ETLValidationError(
            f"Relatório de VENDAS '{nome}' sem nomes de produto válidos após a limpeza."
        )
    return df


def carregar_compras(caminho: str):
    """
    Retorna (df_compras, coluna_valor_usada).
    `coluna_valor_usada` ∈ {"produtos_total", "valor_nf"} — produtos_total
    tem preferência (valor "puro" dos itens; valor_nf inclui frete/imposto).
    """
    nome = os.path.basename(caminho)
    bruto = _ler_bruto(caminho)
    if bruto.empty:
        raise ETLValidationError(f"O arquivo de compras '{nome}' está totalmente vazio.")

    idx, mapa, linhas_cab = _detectar_cabecalho(bruto, ALIAS_COMPRAS, _validar_compras)
    df = _construir_df_canonico(bruto, idx, mapa, linhas_cab)
    df = _filtrar_linhas_validas(df, "fornecedor")

    if "produtos_total" in df.columns:
        coluna_valor = "produtos_total"
    elif "valor_nf" in df.columns:
        coluna_valor = "valor_nf"
    else:
        raise ETLValidationError(
            f"Relatório de COMPRAS '{nome}' sem coluna de valor reconhecida "
            "('Total Produtos' nem 'Valor NF' encontradas)."
        )
    df[coluna_valor] = _converter_serie(df[coluna_valor]).fillna(0.0)
    df = df[df[coluna_valor] > 0].reset_index(drop=True)

    if "qtde_comprada" in df.columns:
        df["qtde_comprada"] = _converter_serie(df["qtde_comprada"]).fillna(0.0)
    if "produto" in df.columns:
        df["_prod_norm"] = df["produto"].map(_normalizar_produto)
    return df, coluna_valor


# =====================================================================
# 6a) Detecção do PERÍODO DA BASE (auditoria de origem dos dados)
# =====================================================================
# Relatórios de PDV trazem o período no cabeçalho textual (ex.:
# "Período de : 01/01/26 á 31/03/26"). Extraímos isso via pandas para exibir
# no dashboard COM QUE BASE/PERÍODO os dados foram gerados — sem digitar à mão.
_RE_DATA_BR = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")


def _parse_data_br(grupos) -> "datetime.date | None":
    d, m, y = (int(x) for x in grupos)
    if y < 100:
        y += 2000  # ano de 2 dígitos: 26 -> 2026
    try:
        return datetime(y, m, d).date()
    except ValueError:
        return None


def _extrair_periodo_base(df_bruto: pd.DataFrame, janela: int = 30):
    """
    Varre as primeiras linhas procurando um cabeçalho de 'período' e extrai as
    duas datas (início, fim). Retorna (date, date) ou None se não encontrar.
    """
    limite = min(janela, len(df_bruto))
    for i in range(limite):
        for cell in df_bruto.iloc[i].tolist():
            if cell is None:
                continue
            txt = _strip_acentos(str(cell)).lower()
            if "periodo" not in txt:
                continue
            datas = [d for d in (_parse_data_br(g) for g in _RE_DATA_BR.findall(str(cell))) if d]
            if len(datas) >= 2:
                ini, fim = datas[0], datas[1]
                return (ini, fim) if ini <= fim else (fim, ini)
            if len(datas) == 1:
                return datas[0], datas[0]
    return None


def _detectar_periodo_arquivo(caminho: str):
    """Lê o cru do arquivo e tenta extrair o período do cabeçalho. Falha → None."""
    try:
        return _extrair_periodo_base(_ler_bruto(caminho))
    except ETLValidationError:
        return None


# =====================================================================
# 6b) Curva ABC (Princípio de Pareto) sobre o faturamento por produto
# =====================================================================
def _classificar_curva_abc(grupo: pd.DataFrame) -> dict:
    """
    Recebe o DataFrame já agregado por produto (colunas: _prod_norm, qtde, fat,
    nome) e devolve um dict com:
      - "produtos": lista (ordenada por faturamento desc) de dicts com posicao,
        nome, faturamento, quantidade, perc_individual, perc_acumulado e classe.
        Truncada em TOP_N_CURVA_ABC para persistência/exibição.
      - "resumo": agregados por classe (qtd de produtos, faturamento e % do total)
        calculados sobre o UNIVERSO COMPLETO de produtos (não só o top-N).

    Regra Pareto: ordena por faturamento desc, acumula o percentual e classifica
    A (<=80%), B (<=95%), C (resto). O produto líder é sempre Classe A — garante
    que um único produto dominante não caia em C por ultrapassar o limiar sozinho.
    """
    base = grupo[grupo["fat"] > 0].sort_values("fat", ascending=False).reset_index(drop=True)
    total = float(base["fat"].sum())
    if base.empty or total <= 0:
        return {"produtos": [], "resumo": []}

    # Percentual individual e ACUMULADO calculados VETORIALMENTE no pandas:
    # cumsum sobre o ranking decrescente de faturamento = curva de Pareto.
    base["perc_ind"] = base["fat"] / total * 100.0
    base["perc_acum"] = base["perc_ind"].cumsum().clip(upper=100.0)

    produtos = []
    contagem = {"A": 0, "B": 0, "C": 0}
    fat_classe = {"A": 0.0, "B": 0.0, "C": 0.0}

    for i, row in base.iterrows():
        acum = float(row["perc_acum"])
        # Líder sempre Classe A; demais por faixa de acumulado (80% / 95%).
        if i == 0 or acum <= ABC_LIMITE_A:
            classe = "A"
        elif acum <= ABC_LIMITE_B:
            classe = "B"
        else:
            classe = "C"
        fat = float(row["fat"])
        contagem[classe] += 1
        fat_classe[classe] += fat
        if i < TOP_N_CURVA_ABC:
            produtos.append({
                "posicao": i + 1,
                "nome": str(row["nome"]),
                "faturamento": fat,
                "quantidade": float(row["qtde"]) if "qtde" in base.columns else None,
                "perc_individual": round(float(row["perc_ind"]), 2),
                "perc_acumulado": round(acum, 2),
                "classe": classe,
            })

    resumo = [
        {
            "classe": c,
            "qtd_produtos": contagem[c],
            "faturamento": round(fat_classe[c], 2),
            "perc_faturamento": round(fat_classe[c] / total * 100.0, 2) if total else 0.0,
        }
        for c in ("A", "B", "C")
    ]
    return {"produtos": produtos, "resumo": resumo}


# =====================================================================
# 7) Cálculo dos KPIs em memória (sem tocar no banco)
# =====================================================================
def calcular_kpis_em_memoria(caminho_vendas: str, caminho_compras: str) -> ResultadoETL:
    nome_v = os.path.basename(caminho_vendas)

    df_v = carregar_vendas(caminho_vendas)
    df_c, col_valor_c = carregar_compras(caminho_compras)

    # ---- VENDAS: agregação por produto normalizado ----
    grupo = (
        df_v.groupby("_prod_norm")
        .agg(qtde=("quantidade", "sum"),
             fat=("valor_venda", "sum"),
             nome=("produto", "first"))
        .reset_index()
    )
    if grupo.empty:
        raise ETLValidationError(f"Sem produtos para agregar em '{nome_v}'.")
    faturamento_total = float(grupo["fat"].sum())

    i_qtd = grupo["qtde"].idxmax()
    prod_norm_mais_vendido = grupo.loc[i_qtd, "_prod_norm"]
    produto_mais_vendido_nome = str(grupo.loc[i_qtd, "nome"])
    produto_mais_vendido_quantidade = float(grupo.loc[i_qtd, "qtde"])

    # Maior faturamento: em varejos pequenos qtde e faturamento são fortemente
    # correlacionados, então o topo dos dois rankings costuma coincidir. Para
    # evitar a redundância visual ("mesmo produto em dois cards") sem violar a
    # regra NEXO (lucro/CMV proibidos como KPI), se o produto de maior
    # faturamento coincidir com o mais vendido mostramos o 2º colocado do
    # ranking de faturamento (exigindo fat > 0 para não cair em zero falso).
    # Se sobrar só 1 candidato (relatório com produto único, ou todos os outros
    # com fat=0), aceitamos a coincidência sem mascarar.
    ranking_fat = grupo.sort_values("fat", ascending=False)
    candidatos_fat = ranking_fat[
        (ranking_fat["_prod_norm"] != prod_norm_mais_vendido) & (ranking_fat["fat"] > 0)
    ]
    linha_fat = candidatos_fat.iloc[0] if not candidatos_fat.empty else ranking_fat.iloc[0]
    produto_maior_faturamento_nome = str(linha_fat["nome"])
    produto_maior_faturamento_valor = float(linha_fat["fat"])

    # ---- Curva ABC (Princípio de Pareto) sobre o faturamento por produto ----
    curva_abc = _classificar_curva_abc(grupo)

    # ---- Período da base (cabeçalho do relatório): vendas tem prioridade ----
    periodo_base = _detectar_periodo_arquivo(caminho_vendas) or _detectar_periodo_arquivo(caminho_compras)
    periodo_base_inicio = periodo_base[0] if periodo_base else None
    periodo_base_fim = periodo_base[1] if periodo_base else None

    # ---- COMPRAS: total (nível NF) ----
    total_comprado = float(df_c[col_valor_c].sum())

    # ---- Indicador de Pressão de Estoque ----
    saldo_estimado_compras_vendas = total_comprado - faturamento_total

    alertas: list = []

    # ---- Produto com maior saldo estimado parado (só se COMPRAS tem produto+qtde) ----
    produto_maior_saldo_parado_nome = None
    saldo_estimado_parado = None
    tem_prod_c = "produto" in df_c.columns and "_prod_norm" in df_c.columns
    tem_qtd_c = "qtde_comprada" in df_c.columns
    if tem_prod_c and tem_qtd_c:
        compras_prod = (
            df_c[df_c["_prod_norm"] != ""]
            .groupby("_prod_norm")
            .agg(qtde_comprada=("qtde_comprada", "sum"),
                 nome=("produto", "first"))
            .reset_index()
        )
        vendas_prod = grupo[["_prod_norm", "qtde"]].rename(columns={"qtde": "qtde_vendida"})
        cruz = compras_prod.merge(vendas_prod, on="_prod_norm", how="left")
        cruz["qtde_vendida"] = cruz["qtde_vendida"].fillna(0.0)
        cruz["saldo"] = cruz["qtde_comprada"] - cruz["qtde_vendida"]
        positivos = cruz[cruz["saldo"] > 0]
        if not positivos.empty:
            i_saldo = positivos["saldo"].idxmax()
            produto_maior_saldo_parado_nome = str(positivos.loc[i_saldo, "nome"])
            saldo_estimado_parado = float(positivos.loc[i_saldo, "saldo"])
    else:
        alertas.append(
            "Limitação técnica: o relatório de COMPRAS está em nível de nota "
            "fiscal (fornecedor/valor) e não traz produto + quantidade comprada. "
            "O KPI 'produto com maior saldo estimado parado' não pôde ser "
            "calculado e foi persistido como nulo (sem gerar dado enganoso)."
        )

    # ---- Telemetria (em memória, NÃO persistida — sem campo no DER V4) ----
    colunas_v = [c for c in df_v.columns if c != "_prod_norm"]
    colunas_c = [c for c in df_c.columns if c != "_prod_norm"]
    telemetria = {
        "coluna_valor_compras": col_valor_c,
        "colunas_vendas": colunas_v,
        "colunas_compras": colunas_c,
        "linhas_vendas": int(len(df_v)),
        "linhas_compras": int(len(df_c)),
    }

    # Alerta: saldo de estoque NEGATIVO informado pelo PDV (não é KPI, é sinal)
    if "estoque" in df_v.columns:
        n_neg = int((df_v["estoque"] < 0).sum())
        taxa = n_neg / len(df_v) if len(df_v) else 0.0
        telemetria["produtos_estoque_pdv_negativo"] = n_neg
        telemetria["taxa_estoque_pdv_negativo"] = taxa
        if taxa > 0.10:
            alertas.append(
                f"Atenção: {n_neg} produto(s) ({taxa * 100:.1f}%) apresentam saldo "
                f"de estoque NEGATIVO informado pelo PDV — possível inconsistência "
                f"de registro no sistema do cliente."
            )

    # Alerta: venda abaixo do custo informado pelo PDV (observação, NÃO margem)
    if "valor_custo" in df_v.columns:
        abaixo = (df_v["valor_custo"] > 0) & (df_v["valor_venda"] < df_v["valor_custo"])
        n_abaixo = int(abaixo.sum())
        telemetria["itens_venda_abaixo_custo_pdv"] = n_abaixo
        if n_abaixo > 0:
            alertas.append(
                f"Alerta de custo informado pelo PDV: {n_abaixo} item(ns) tiveram "
                f"valor total de venda inferior ao custo total informado pelo PDV no "
                f"período. É um sinal extraído da coluna bruta de custo do PDV, NÃO "
                f"um cálculo contábil de margem."
            )

    kpis = {
        "faturamento_total": faturamento_total,
        "total_comprado": total_comprado,
        "saldo_estimado_compras_vendas": saldo_estimado_compras_vendas,
        "produto_mais_vendido_nome": produto_mais_vendido_nome,
        "produto_mais_vendido_quantidade": produto_mais_vendido_quantidade,
        "produto_maior_faturamento_nome": produto_maior_faturamento_nome,
        "produto_maior_faturamento_valor": produto_maior_faturamento_valor,
        "produto_maior_saldo_parado_nome": produto_maior_saldo_parado_nome,
        "saldo_estimado_parado": saldo_estimado_parado,
        # Curva ABC: NÃO é um único KPI escalar; é o ranking + resumo por classe.
        # Persistido em produto_curva_abc (1:N), não em indicador_analise.
        "curva_abc": curva_abc,
        # Período real coberto pela base (lido do cabeçalho do relatório).
        "periodo_base_inicio": periodo_base_inicio,
        "periodo_base_fim": periodo_base_fim,
    }
    return ResultadoETL(
        sucesso=True,
        mensagem="Cálculo concluído em memória.",
        kpis=kpis,
        telemetria=telemetria,
        alertas=alertas,
    )


# =====================================================================
# 8) Persistência (somente KPIs finais) + orquestrador oficial
# =====================================================================
def _persistir_indicadores(db_session, id_analise: int, kpis: dict) -> None:
    indicador = db_session.execute(
        select(IndicadorAnalise).where(IndicadorAnalise.id_analise == id_analise)
    ).scalar_one_or_none()

    if indicador is None:
        indicador = IndicadorAnalise(id_analise=id_analise, versao_processamento=1)
        db_session.add(indicador)
    else:
        indicador.versao_processamento = (indicador.versao_processamento or 0) + 1

    indicador.faturamento_total = kpis["faturamento_total"]
    indicador.total_comprado = kpis["total_comprado"]
    indicador.saldo_estimado_compras_vendas = kpis["saldo_estimado_compras_vendas"]
    indicador.produto_mais_vendido_nome = kpis["produto_mais_vendido_nome"]
    indicador.produto_mais_vendido_quantidade = kpis["produto_mais_vendido_quantidade"]
    indicador.produto_maior_faturamento_nome = kpis["produto_maior_faturamento_nome"]
    indicador.produto_maior_faturamento_valor = kpis["produto_maior_faturamento_valor"]
    indicador.produto_maior_saldo_parado_nome = kpis["produto_maior_saldo_parado_nome"]
    indicador.saldo_estimado_parado = kpis["saldo_estimado_parado"]
    indicador.periodo_base_inicio = kpis.get("periodo_base_inicio")
    indicador.periodo_base_fim = kpis.get("periodo_base_fim")
    indicador.data_geracao = datetime.utcnow()


def _persistir_curva_abc(db_session, id_analise: int, kpis: dict) -> None:
    """
    Regrava (idempotente) o ranking ABC do top-N em produto_curva_abc:
    apaga as linhas anteriores desta análise e insere o ranking novo. Reflete
    o reprocessamento — cada execução do ETL substitui a Curva ABC anterior.
    """
    # Limpa o ranking anterior desta análise (reprocessamento limpo).
    antigos = db_session.execute(
        select(ProdutoCurvaABC).where(ProdutoCurvaABC.id_analise == id_analise)
    ).scalars().all()
    for linha in antigos:
        db_session.delete(linha)
    db_session.flush()

    for p in kpis.get("curva_abc", {}).get("produtos", []):
        db_session.add(ProdutoCurvaABC(
            id_analise=id_analise,
            posicao_ranking=p["posicao"],
            produto_nome=p["nome"],
            faturamento=p["faturamento"],
            quantidade=p.get("quantidade"),
            percentual_individual=p["perc_individual"],
            percentual_acumulado=p["perc_acumulado"],
            classe_abc=p["classe"],
        ))


def processar_arquivos_analise(caminho_vendas: str, caminho_compras: str,
                               id_analise: int, db_session) -> ResultadoETL:
    """
    Orquestrador oficial (assinatura imutável). Calcula em memória e persiste
    APENAS os KPIs finais. NÃO comita (caller controla transação) e NÃO
    persiste linhas processadas. Em falha, retorna sucesso=False sem gravar.
    """
    try:
        resultado = calcular_kpis_em_memoria(caminho_vendas, caminho_compras)
        _persistir_indicadores(db_session, id_analise, resultado.kpis)
        _persistir_curva_abc(db_session, id_analise, resultado.kpis)
        return resultado._replace(
            mensagem="Processamento concluído. KPIs consolidados + Curva ABC persistidos."
        )
    except ETLValidationError as e:
        logger.warning("Validação do ETL falhou: %s", e)
        return ResultadoETL(sucesso=False, mensagem=str(e),
                            kpis={}, telemetria={}, alertas=[])
    except Exception as e:  # noqa: BLE001
        logger.exception("Erro inesperado no motor de ETL")
        return ResultadoETL(sucesso=False, mensagem=f"Erro interno do ETL: {e}",
                            kpis={}, telemetria={}, alertas=[])
