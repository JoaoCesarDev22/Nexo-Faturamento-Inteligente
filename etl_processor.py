"""
NEXO - Faturamento Inteligente | Motor ETL (Semana 2)
=======================================================
Processa relatórios brutos de PDV (Giro de Vendas + Posição de Compras),
calcula os KPIs OFICIAIS do DER V4 e persiste em indicador_analise.

Princípios (MVP/PI2):
  - Os DataFrames pandas vivem APENAS em memória durante o processamento.
  - Linhas processadas NÃO são persistidas; só os KPIs finais consolidados.
  - O indicador de descasamento é o "Indicador de Pressão de Estoque"
    (saldo estimado entre compras e vendas). NÃO representa lucro, margem,
    CMV, prejuízo, rentabilidade nem estoque físico real.
  - Falha de leitura/validação nunca é mascarada com dados falsos: o ETL
    falha com erro claro ou registra limitação técnica explícita.

A função pública `calcular_kpis_em_memoria` faz todo o cálculo SEM tocar no
banco (usada pelo teste isolado). `processar_arquivos_analise` mantém a
assinatura oficial e adiciona a persistência via ORM.
"""

import os
import re
import logging
import unicodedata
from datetime import datetime
from collections import namedtuple

import pandas as pd
from sqlalchemy import select

from models import IndicadorAnalise

logger = logging.getLogger(__name__)

ResultadoETL = namedtuple(
    "ResultadoETL", ["sucesso", "mensagem", "kpis", "telemetria", "alertas"]
)


class ETLValidationError(Exception):
    """Erro de validação de dados/arquivo. Mensagem é segura para exibir ao admin."""
    pass


# Encodings e separadores testados em sequência para arquivos texto/CSV.
ENCODINGS = ("utf-8-sig", "cp1252", "utf-8", "latin1")
SEPARADORES = (";", ",", "\t")


# =====================================================================
# Helpers de texto / sanitização
# =====================================================================
def _strip_acentos(texto: str) -> str:
    """Remove acentos para comparação (normalização NFKD + descarte de diacríticos)."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(texto)) if not unicodedata.combining(c)
    )


def _sanitizar_nome(valor) -> str:
    """
    Sanitização ESTRITA de um nome de coluna/célula de cabeçalho:
      - vira string; remove BOM (\\ufeff) e espaço fantasma (\\xa0);
      - elimina quebras de linha; colapsa espaços duplicados; faz strip.
    Mantém acentos e caixa (é o nome legível/exibível).
    """
    if valor is None:
        return ""
    s = str(valor)
    s = s.replace("﻿", "")
    s = s.replace("\xa0", " ")
    s = s.replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_chave(valor) -> str:
    """Chave normalizada para casar sinônimos de coluna: sanitiza, tira acento, minúsculas."""
    s = _sanitizar_nome(valor)
    s = _strip_acentos(s).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalizar_produto(valor) -> str:
    """
    Normaliza o NOME do produto para agrupamento/cruzamento consistente entre
    VENDAS e COMPRAS: uppercase, sem acentos, sem caracteres especiais,
    espaços colapsados. O nome legível original é preservado à parte para exibição.
    """
    s = _strip_acentos(_sanitizar_nome(valor)).upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# =====================================================================
# Conversão numérica robusta (pt-BR e en-US)
# =====================================================================
def _parse_valor(bruto) -> float | None:
    """
    Converte um único valor monetário/quantidade em float, tolerando:
    'R$ 1.234,56', '1.234,56', '1234,56', '1,234.56', '1234.56', espaços,
    NBSP, vazios e hífens. Retorna None quando o valor é ausente/ilegível.
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

    tem_virgula = "," in s2
    tem_ponto = "." in s2
    if tem_virgula and tem_ponto:
        # O separador mais à direita é o decimal.
        if s2.rfind(",") > s2.rfind("."):
            s2 = s2.replace(".", "").replace(",", ".")
        else:
            s2 = s2.replace(",", "")
    elif tem_virgula:
        # Padrão brasileiro: vírgula decimal.
        s2 = s2.replace(".", "").replace(",", ".")
    elif tem_ponto and s2.count(".") > 1:
        # Múltiplos pontos => separadores de milhar.
        s2 = s2.replace(".", "")
    # ponto único => decimal (mantém)

    try:
        val = float(s2)
    except ValueError:
        return None
    return -val if (negativo and val > 0) else val


def _converter_serie(serie: pd.Series) -> pd.Series:
    """Aplica _parse_valor elemento a elemento, retornando Series de float (NaN p/ ausentes)."""
    return pd.Series([_parse_valor(v) for v in serie], index=serie.index, dtype="float64")


def _converter_coluna_obrigatoria(df: pd.DataFrame, coluna: str, nome_arquivo: str) -> pd.Series:
    """
    Converte uma coluna obrigatória para número. Se valores NÃO vazios não puderem
    ser convertidos, falha com erro claro (coluna, arquivo, quantidade, amostras).
    Valores genuinamente vazios viram 0.0 (ausência, não erro).
    """
    orig = df[coluna].map(lambda x: str(x))
    num = _converter_serie(df[coluna])
    nao_vazio = orig.map(lambda x: x.strip().lower() not in ("", "nan", "none", "-", "--"))
    problematicos = nao_vazio & num.isna()
    n = int(problematicos.sum())
    if n > 0:
        amostras = orig[problematicos].head(5).tolist()
        raise ETLValidationError(
            f"A coluna '{coluna}' do arquivo '{nome_arquivo}' tem {n} valor(es) "
            f"não numéricos que impedem o cálculo confiável dos KPIs. "
            f"Exemplos: {amostras}"
        )
    return num.fillna(0.0)


# =====================================================================
# Camada 1: leitura defensiva e inteligente
# =====================================================================
def _detectar_e_ler_arquivo(caminho_arquivo: str) -> pd.DataFrame:
    """
    Detecta a extensão e lê o arquivo BRUTO (header=None, tudo string) para que
    as camadas de limpeza localizem o cabeçalho real. Falha com erro claro em vez
    de inventar dados quando o arquivo é ilegível.
    """
    if not os.path.exists(caminho_arquivo):
        raise ETLValidationError(f"Arquivo não encontrado: {caminho_arquivo}")

    ext = os.path.splitext(caminho_arquivo)[1].lower()

    if ext == ".xlsx":
        try:
            return pd.read_excel(caminho_arquivo, dtype=str, header=None, engine="openpyxl")
        except Exception as e:
            raise ETLValidationError(
                f"Falha ao ler o arquivo .xlsx '{caminho_arquivo}' com openpyxl. "
                f"Erro original: {e}"
            )

    if ext == ".xls":
        # Suporte a .xls só existe se 'xlrd' estiver instalado. Não anunciamos
        # suporte que o sistema não consegue cumprir.
        try:
            import xlrd  # noqa: F401
        except ImportError:
            raise ETLValidationError(
                f"Formato .xls não suportado: a dependência 'xlrd' não está instalada "
                f"neste projeto. Converta '{os.path.basename(caminho_arquivo)}' para .xlsx ou .csv."
            )
        try:
            return pd.read_excel(caminho_arquivo, dtype=str, header=None, engine="xlrd")
        except Exception as e:
            raise ETLValidationError(
                f"Falha ao ler o arquivo .xls '{caminho_arquivo}'. Erro original: {e}"
            )

    # CSV / texto: fallbacks controlados de encoding e separador.
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

    # Último recurso: ignora bytes inválidos, mas registra alerta técnico.
    for sep in SEPARADORES:
        try:
            logger.warning(
                "Leitura de '%s' usando encoding_errors='ignore' como último recurso "
                "(possível perda de caracteres). Separador testado: %r.", caminho_arquivo, sep
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
        "Não foi possível ler o arquivo de texto/CSV.\n"
        f"  Arquivo: {caminho_arquivo}\n"
        f"  Extensão: {ext or '(sem extensão)'}\n"
        f"  Encodings testados: {list(ENCODINGS)}\n"
        f"  Separadores testados: {[repr(s) for s in SEPARADORES]}\n"
        f"  Erro original: {ultimo_erro}"
    )


# =====================================================================
# Camadas 2/3: localização de cabeçalho e limpeza
# =====================================================================
def _is_data_marker(celula) -> bool:
    """True se a célula da 1ª coluna parece ser DADO (código numérico ou data), não cabeçalho."""
    if celula is None:
        return False
    s = str(celula).strip()
    if s.lower() in ("", "nan", "none"):
        return False
    if re.search(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", s):  # data
        return True
    if re.fullmatch(r"[0-9.,]+", s):  # código/valor puramente numérico
        return True
    return False


def _localizar_idx_cabecalho(df: pd.DataFrame, palavras: list[str]) -> int:
    """
    Índice da 1ª linha-âncora de cabeçalho: a 1ª coluna deve COMEÇAR por uma
    palavra-âncora (token exato) e ser curta. Evita casar com linhas de filtro
    longas do PDV (ex.: 'Produtos ....: -Todos até -Todos').
    """
    for i in range(len(df)):
        v = _norm_chave(df.iloc[i, 0])
        if not v:
            continue
        tokens = v.split()
        if tokens and tokens[0] in palavras and len(v) <= 25:
            return i
    return -1


def _montar_cabecalho(df_raw: pd.DataFrame, idx_header: int) -> pd.DataFrame:
    """
    Reconstrói o cabeçalho a partir da linha-âncora, dobrando linhas subsequentes
    de sub-cabeçalho (caso do PDV, que quebra títulos em 2 linhas) até a 1ª linha
    de dados. Retorna o DataFrame de dados com nomes de coluna sanitizados.
    """
    n = len(df_raw)
    linhas_header = [idx_header]
    j = idx_header + 1
    while j < n and (j - idx_header) <= 3:
        if _is_data_marker(df_raw.iloc[j, 0]):
            break
        linhas_header.append(j)
        j += 1
    idx_data = j
    if idx_data >= n:
        raise ETLValidationError("Cabeçalho localizado, mas nenhuma linha de dados encontrada após ele.")

    combinado = []
    for col in range(df_raw.shape[1]):
        tokens = []
        for r in linhas_header:
            s = _sanitizar_nome(df_raw.iloc[r, col])
            if s and s.lower() != "nan":
                tokens.append(s)
        combinado.append(" ".join(tokens).strip())

    df = df_raw.iloc[idx_data:].copy()
    df.columns = combinado
    return df


def _aplicar_mapa(df: pd.DataFrame, mapa: dict[str, str]) -> pd.DataFrame:
    """
    Renomeia colunas para nomes canônicos via sinônimos normalizados e descarta
    colunas sem nome. Mantém o mapeamento 1:1 (1º match vence) e preserva colunas
    extras (não usadas) sem quebrar.
    """
    renomear = {}
    usados = set()
    manter = []
    vistos = set()
    for col in df.columns:
        nome = _sanitizar_nome(col)
        if nome == "" or nome in vistos:
            continue  # descarta colunas vazias e nomes duplicados
        vistos.add(nome)
        manter.append(col)
        chave = _norm_chave(col)
        if chave in mapa and mapa[chave] not in usados:
            renomear[col] = mapa[chave]
            usados.add(mapa[chave])
    df = df.loc[:, manter].rename(columns=renomear)
    return df


MAPA_VENDAS = {
    "codigo": "Codigo",
    "nome do produto": "Produto",
    "produto": "Produto",
    "descricao": "Produto",
    "qtde vend": "Qtde Vend",
    "qtde": "Qtde Vend",
    "quantidade": "Qtde Vend",
    "qtd": "Qtde Vend",
    "total venda": "Total Venda",
    "total vendas": "Total Venda",
    "valor total": "Total Venda",
    "total custo": "Total Custo",
    "custo total": "Total Custo",
    "estoque": "Estoque Atual",
    "estoque atual": "Estoque Atual",
}

MAPA_COMPRAS = {
    "data da compra": "Data Compra",
    "data compra": "Data Compra",
    "fornecedor": "Fornecedor",
    "vr total produtos": "Vr Total Produtos",
    "vr total": "Vr Total Produtos",
    "valor nf": "Valor Nf",
    "numero nf": "Numero NF",
    # Sinônimos de produto/quantidade: usados SE o relatório de compras os tiver
    # (permite calcular o saldo parado por produto quando os dados existirem).
    "produto": "Produto",
    "nome do produto": "Produto",
    "descricao": "Produto",
    "qtde comprada": "Qtde Comprada",
    "qtde compra": "Qtde Comprada",
    "qtde": "Qtde Comprada",
    "quantidade": "Qtde Comprada",
}

# Linhas de rodapé/lixo a descartar (totais, movimentação, marca d'água do PDV).
PADRAO_LIXO = re.compile(r"(?:totais|total geral|movimenta|desconto|software|labsofti)", re.IGNORECASE)


def limpar_vendas(df_raw: pd.DataFrame, nome_arquivo: str) -> pd.DataFrame:
    """Camada 2: localiza cabeçalho do Giro de Vendas, sanitiza e remove lixo/rodapés."""
    if df_raw.empty:
        raise ETLValidationError(f"O arquivo de vendas '{nome_arquivo}' está totalmente vazio.")
    idx = _localizar_idx_cabecalho(df_raw, ["codigo", "produto"])
    if idx < 0:
        raise ETLValidationError(
            f"Não foi possível localizar o cabeçalho do relatório de VENDAS em "
            f"'{nome_arquivo}' (esperado uma linha com 'Codigo'/'Código' ou 'Produto')."
        )
    df = _montar_cabecalho(df_raw, idx)
    df = _aplicar_mapa(df, MAPA_VENDAS)

    if "Produto" in df.columns:
        prod = df["Produto"].map(lambda x: str(x).strip())
        prod_norm = df["Produto"].map(_norm_chave)
        valido = prod.str.lower().map(lambda x: x not in ("", "nan", "none"))
        nao_header = ~prod_norm.isin(["nome do produto", "produto", "descricao"])
        mask = valido & nao_header & ~prod.str.contains(PADRAO_LIXO, na=False)
        if "Codigo" in df.columns:
            # Quebras de página reimprimem o cabeçalho ('Codigo') no meio dos dados.
            mask = mask & ~df["Codigo"].map(_norm_chave).isin(["codigo"])
        df = df[mask]
    return df.reset_index(drop=True)


def limpar_compras(df_raw: pd.DataFrame, nome_arquivo: str) -> pd.DataFrame:
    """Camada 3: localiza cabeçalho da Posição de Compras, sanitiza e remove rodapé de totais."""
    if df_raw.empty:
        raise ETLValidationError(f"O arquivo de compras '{nome_arquivo}' está totalmente vazio.")
    idx = _localizar_idx_cabecalho(df_raw, ["data", "fornecedor"])
    if idx < 0:
        raise ETLValidationError(
            f"Não foi possível localizar o cabeçalho do relatório de COMPRAS em "
            f"'{nome_arquivo}' (esperado uma linha com 'Data'/'Fornecedor')."
        )
    df = _montar_cabecalho(df_raw, idx)
    df = _aplicar_mapa(df, MAPA_COMPRAS)

    # Remove rodapé "Totais geral" e similares (1ª coluna disponível).
    if len(df.columns) > 0:
        col_ref = "Data Compra" if "Data Compra" in df.columns else df.columns[0]
        df = df[~df[col_ref].astype(str).str.contains(PADRAO_LIXO, na=False)]
    return df.reset_index(drop=True)


# =====================================================================
# Cálculo dos KPIs em memória (sem banco)
# =====================================================================
def calcular_kpis_em_memoria(caminho_vendas: str, caminho_compras: str) -> ResultadoETL:
    """
    Lê, limpa e calcula todos os KPIs/telemetria em memória, SEM persistir nada.
    Levanta ETLValidationError com mensagem clara em caso de problema de dados.
    """
    nome_v = os.path.basename(caminho_vendas)
    nome_c = os.path.basename(caminho_compras)

    df_v = limpar_vendas(_detectar_e_ler_arquivo(caminho_vendas), nome_v)
    df_c = limpar_compras(_detectar_e_ler_arquivo(caminho_compras), nome_c)

    colunas_vendas = list(df_v.columns)
    colunas_compras = list(df_c.columns)

    # --- Validação de colunas obrigatórias de VENDAS (produto, qtd, faturamento) ---
    obrig_v = ["Produto", "Qtde Vend", "Total Venda"]
    faltando_v = [c for c in obrig_v if c not in df_v.columns]
    if faltando_v:
        raise ETLValidationError(
            f"Relatório de VENDAS '{nome_v}' sem coluna(s) obrigatória(s): {faltando_v}. "
            f"Colunas encontradas após sanitização: {colunas_vendas}"
        )
    if df_v.empty:
        raise ETLValidationError(f"Relatório de VENDAS '{nome_v}' sem linhas de produto após a limpeza.")

    # --- Conversão numérica VENDAS ---
    df_v["_qtde"] = _converter_coluna_obrigatoria(df_v, "Qtde Vend", nome_v)
    df_v["_fat"] = _converter_coluna_obrigatoria(df_v, "Total Venda", nome_v)
    df_v["_prod_norm"] = df_v["Produto"].map(_normalizar_produto)
    df_v = df_v[df_v["_prod_norm"] != ""]
    if df_v.empty:
        raise ETLValidationError(f"Relatório de VENDAS '{nome_v}' sem nomes de produto válidos.")

    # --- KPI: faturamento e produtos de destaque (agrupados por produto normalizado) ---
    grupo_v = (
        df_v.groupby("_prod_norm")
        .agg(qtde=("_qtde", "sum"), fat=("_fat", "sum"), nome=("Produto", "first"))
        .reset_index()
    )
    faturamento_total = float(grupo_v["fat"].sum())

    i_qtd = grupo_v["qtde"].idxmax()
    produto_mais_vendido_nome = str(grupo_v.loc[i_qtd, "nome"])
    produto_mais_vendido_quantidade = float(grupo_v.loc[i_qtd, "qtde"])

    i_fat = grupo_v["fat"].idxmax()
    produto_maior_faturamento_nome = str(grupo_v.loc[i_fat, "nome"])
    produto_maior_faturamento_valor = float(grupo_v.loc[i_fat, "fat"])

    # --- COMPRAS: total comprado (nível nota fiscal) ---
    col_valor_c = next((c for c in ("Vr Total Produtos", "Valor Nf") if c in df_c.columns), None)
    if col_valor_c is None:
        raise ETLValidationError(
            f"Relatório de COMPRAS '{nome_c}' sem coluna de valor "
            f"('Vr Total Produtos' ou 'Valor Nf'). Colunas encontradas: {colunas_compras}"
        )
    df_c["_valc"] = _converter_coluna_obrigatoria(df_c, col_valor_c, nome_c)
    df_c = df_c[df_c["_valc"] > 0]
    total_comprado = float(df_c["_valc"].sum())

    # --- Indicador de Pressão de Estoque (saldo estimado compras x vendas) ---
    saldo_estimado_compras_vendas = total_comprado - faturamento_total

    alertas: list[str] = []

    # --- Produto com maior saldo estimado parado (exige produto + quantidade em COMPRAS) ---
    produto_maior_saldo_parado_nome = None
    saldo_estimado_parado = None
    tem_produto_c = "Produto" in df_c.columns
    tem_qtde_c = "Qtde Comprada" in df_c.columns
    if tem_produto_c and tem_qtde_c:
        df_c["_qtdec"] = _converter_coluna_obrigatoria(df_c, "Qtde Comprada", nome_c)
        df_c["_prod_norm"] = df_c["Produto"].map(_normalizar_produto)
        compras_prod = (
            df_c[df_c["_prod_norm"] != ""]
            .groupby("_prod_norm")
            .agg(qtde_comprada=("_qtdec", "sum"), nome=("Produto", "first"))
            .reset_index()
        )
        vendas_prod = grupo_v[["_prod_norm", "qtde"]].rename(columns={"qtde": "qtde_vendida"})
        cruz = compras_prod.merge(vendas_prod, on="_prod_norm", how="left")
        cruz["qtde_vendida"] = cruz["qtde_vendida"].fillna(0.0)
        cruz["saldo"] = cruz["qtde_comprada"] - cruz["qtde_vendida"]
        positivos = cruz[cruz["saldo"] > 0]
        if not positivos.empty:
            i_saldo = positivos["saldo"].idxmax()
            produto_maior_saldo_parado_nome = str(positivos.loc[i_saldo, "nome"])
            saldo_estimado_parado = float(positivos.loc[i_saldo, "saldo"])  # em unidades
    else:
        alertas.append(
            "Limitação técnica: o relatório de COMPRAS está em nível de nota fiscal "
            "(fornecedor/valor) e não traz produto + quantidade comprada. O KPI "
            "'produto com maior saldo estimado parado' não pôde ser calculado e foi "
            "persistido como nulo (sem gerar dado enganoso)."
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
    }

    # --- Telemetria (em memória, NÃO persistida — sem campo no DER V4) ---
    telemetria = {
        "coluna_valor_compras": col_valor_c,
        "colunas_vendas": colunas_vendas,
        "colunas_compras": colunas_compras,
        "linhas_vendas": int(len(df_v)),
        "linhas_compras": int(len(df_c)),
    }

    # Saldo de estoque informado pelo PDV (coluna bruta, opcional).
    if "Estoque Atual" in df_v.columns:
        est = _converter_serie(df_v["Estoque Atual"]).fillna(0.0)
        n_neg = int((est < 0).sum())
        taxa = n_neg / len(df_v) if len(df_v) else 0.0
        telemetria["produtos_estoque_pdv_negativo"] = n_neg
        telemetria["taxa_estoque_pdv_negativo"] = taxa
        if taxa > 0.10:
            alertas.append(
                f"Atenção: {n_neg} produto(s) ({taxa * 100:.1f}%) apresentam saldo de estoque "
                f"NEGATIVO informado pelo PDV — possível inconsistência de registro no sistema do cliente."
            )

    # Alerta de custo informado pelo PDV (coluna bruta, opcional — NÃO é margem/lucro).
    if "Total Custo" in df_v.columns:
        custo = _converter_serie(df_v["Total Custo"]).fillna(0.0)
        abaixo = (custo > 0) & (df_v["_fat"].values < custo.values)
        n_abaixo = int(abaixo.sum())
        telemetria["itens_venda_abaixo_custo_pdv"] = n_abaixo
        if n_abaixo > 0:
            alertas.append(
                f"Alerta de custo informado pelo PDV: {n_abaixo} item(ns) tiveram valor total "
                f"de venda inferior ao custo total informado pelo PDV no período. É um sinal "
                f"extraído da coluna bruta de custo do PDV, não um cálculo contábil de margem."
            )

    return ResultadoETL(
        sucesso=True,
        mensagem="Cálculo concluído em memória.",
        kpis=kpis,
        telemetria=telemetria,
        alertas=alertas,
    )


# =====================================================================
# Persistência (apenas KPIs finais) e orquestrador oficial
# =====================================================================
def _persistir_indicadores(db_session, id_analise: int, kpis: dict) -> None:
    """Insere/atualiza SOMENTE os KPIs consolidados em indicador_analise (via ORM)."""
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
    indicador.data_geracao = datetime.utcnow()


def processar_arquivos_analise(caminho_vendas: str, caminho_compras: str, id_analise: int, db_session) -> ResultadoETL:
    """
    Orquestrador oficial (assinatura imutável). Calcula os KPIs em memória e
    persiste APENAS os KPIs finais. NÃO comita (quem chama controla a transação)
    e NÃO persiste linhas processadas. Em falha, retorna sucesso=False sem gravar
    indicadores parciais.
    """
    try:
        resultado = calcular_kpis_em_memoria(caminho_vendas, caminho_compras)
        _persistir_indicadores(db_session, id_analise, resultado.kpis)
        return resultado._replace(
            mensagem="Processamento concluído. KPIs consolidados persistidos em indicador_analise."
        )
    except ETLValidationError as e:
        logger.warning("Validação do ETL falhou: %s", e)
        return ResultadoETL(sucesso=False, mensagem=str(e), kpis={}, telemetria={}, alertas=[])
    except Exception as e:  # noqa: BLE001
        logger.exception("Erro inesperado no motor de ETL")
        return ResultadoETL(sucesso=False, mensagem=f"Erro interno do ETL: {e}", kpis={}, telemetria={}, alertas=[])
