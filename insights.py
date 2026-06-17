"""
NEXO - Faturamento Inteligente | Síntese executiva em semáforos (5W2H)
======================================================================
Converte os KPIs honestos da análise (indicador_analise + Curva ABC) em
"Cards de Indicadores de Risco" — o Sistema de Semáforos que substitui a
muralha de texto na devolutiva ao cliente.

Cada card é DERIVADO DO DADO (não de texto manual de IA) e segue a estrutura
administrativa 5W2H reduzida ao essencial:
    - "O que fazer"  (ação direta)
    - "Por que fazer" (justificativa curta)

Níveis (cores):
    critico      -> Vermelho  (ruptura / capital imobilizado severo)
    atencao      -> Amarelo   (descompasso moderado / dependência)
    oportunidade -> Verde     (giro saudável / alavanca de crescimento)

REGRA INEGOCIÁVEL (espelha o etl_processor): nenhum card afirma lucro, margem,
CMV ou rentabilidade. Trabalhamos só com faturamento, volume comprado e o
descasamento estimado entre compras e vendas (Pressão de Estoque).
"""

from __future__ import annotations

# Limiares do descasamento compras x vendas, como fração do faturamento.
# Acima disso, o capital comprado sem giro correspondente vira risco.
PRESSAO_CRITICA = 0.15   # > 15% do faturamento comprado "a mais" sem girar
PRESSAO_ATENCAO = 0.05   # 5% a 15% -> atenção


def _f(valor) -> float:
    """Converte Decimal/None para float seguro."""
    try:
        return float(valor) if valor is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def gerar_semaforos(indicador, curva_abc=None) -> list[dict]:
    """
    Recebe um IndicadorAnalise (ou None) e, opcionalmente, a lista de
    ProdutoCurvaABC da análise. Devolve uma lista de cards-semáforo:

        {"nivel": "critico"|"atencao"|"oportunidade",
         "titulo": str, "o_que_fazer": str, "por_que": str}

    Sem indicador, devolve lista vazia (o template cai no estado "sem dados").
    """
    if indicador is None:
        return []

    faturamento = _f(indicador.faturamento_total)
    saldo = _f(indicador.saldo_estimado_compras_vendas)  # comprado - vendido
    cards: list[dict] = []

    # --- 1. Pressão de Estoque (descasamento compras x vendas) -------------
    ratio = (saldo / faturamento) if faturamento > 0 else 0.0
    if ratio > PRESSAO_CRITICA:
        cards.append({
            "nivel": "critico",
            "titulo": "Capital imobilizado em estoque",
            "o_que_fazer": "Reduzir o ritmo de compras e priorizar o giro do "
                           "estoque já adquirido no próximo ciclo.",
            "por_que": f"Você comprou cerca de {ratio*100:.0f}% a mais do que "
                       "vendeu (em R$) — capital parado e risco de ruptura de caixa.",
        })
    elif ratio > PRESSAO_ATENCAO:
        cards.append({
            "nivel": "atencao",
            "titulo": "Descompasso entre compras e vendas",
            "o_que_fazer": "Acompanhar o casamento entre prazo de pagamento a "
                           "fornecedores e o recebimento das vendas.",
            "por_que": f"Compras superaram as vendas em ~{ratio*100:.0f}% no "
                       "período — descasamento moderado de capital de giro.",
        })
    else:
        cards.append({
            "nivel": "oportunidade",
            "titulo": "Giro de estoque saudável",
            "o_que_fazer": "Manter a cadência atual de compras e estudar "
                           "expansão de mix nos produtos de maior saída.",
            "por_que": "As vendas acompanharam (ou superaram) as compras — "
                       "capital de giro fluindo bem no período.",
        })

    # --- 2. Produto com maior saldo estimado parado ------------------------
    nome_parado = indicador.produto_maior_saldo_parado_nome
    saldo_parado = _f(indicador.saldo_estimado_parado)
    if nome_parado and saldo_parado > 0:
        cards.append({
            "nivel": "atencao",
            "titulo": "Estoque parado concentrado",
            "o_que_fazer": f"Criar ação de escoamento para “{nome_parado}” "
                           "(promoção, combo ou recompra mais conservadora).",
            "por_que": f"Estimativa de {saldo_parado:.0f} unidades compradas e "
                       "ainda não vendidas — maior saldo parado do período.",
        })

    # --- 3. Concentração de faturamento na Classe A (Pareto) ---------------
    if curva_abc:
        classe_a = [p for p in curva_abc if p.classe_abc == "A"]
        if classe_a:
            n_a = len(classe_a)
            perc_a = sum(_f(p.percentual_individual) for p in classe_a)
            cards.append({
                "nivel": "oportunidade",
                "titulo": "Produtos estratégicos (Classe A)",
                "o_que_fazer": "Blindar o abastecimento e a precificação dos "
                               f"{n_a} produto(s) Classe A.",
                "por_que": f"Eles concentram ~{perc_a:.0f}% do faturamento "
                           "(Princípio de Pareto) — ruptura neles dói no caixa.",
            })

    return cards
