"""
NEXO - Faturamento Inteligente | Geração automatizada da devolutiva (IA)
========================================================================
Gera "Resumo Executivo" e "Conclusão Estratégica" a partir do resumo estatístico
da análise (indicador + Curva ABC), eliminando a digitação manual.

Provider: Claude (Anthropic), modelo claude-opus-4-8, via SDK oficial `anthropic`.

REGRA DE OURO (blindada no prompt e revalidada na saída): a devolutiva NUNCA
cita lucro, margem, CMV, rentabilidade ou prejuízo — só faturamento, volume de
compras, Indicador de Pressão de Estoque (descasamento compras × vendas),
faturamento por produto e a Curva ABC (Pareto). É a mesma disciplina do ETL.

Fail-safe: sem ANTHROPIC_API_KEY (ou em erro/refusal), cai num gerador
determinístico local a partir dos mesmos números — o sistema nunca quebra.
A flag `fonte` ('ia' | 'fallback') diz qual caminho gerou o texto, para a UI
ser honesta sobre a origem.
"""

import json
import logging

from flask import current_app

logger = logging.getLogger(__name__)

# Palavras proibidas — a saída (de qualquer fonte) é auditada contra elas.
_PROIBIDAS = ("margem", "lucro", "cmv", "rentab", "prejuíz", "prejuiz")

_SYSTEM = (
    "Você é o consultor sênior da NEXO - Faturamento Inteligente, escrevendo a "
    "devolutiva estratégica para o cliente (um lojista). Escreva em português do "
    "Brasil, tom executivo, direto e fundamentado.\n\n"
    "REGRAS INEGOCIÁVEIS:\n"
    "1. NUNCA mencione lucro, margem, margem de contribuição, CMV, rentabilidade "
    "ou prejuízo — os dados do PDV não sustentam esses conceitos. Trabalhe SOMENTE "
    "com: faturamento, volume de compras, Indicador de Pressão de Estoque "
    "(descasamento entre compras e vendas em R$), faturamento por produto e a "
    "Curva ABC (Princípio de Pareto).\n"
    "2. Use APENAS os números fornecidos no contexto. Não invente valores, "
    "percentuais ou nomes de produtos.\n"
    "3. Seja conciso e acionável (metodologia 5W2H: o que e por que). Sem floreio.\n"
    "4. 'resumo_executivo': 1 parágrafo (4–6 frases) com a leitura dos números. "
    "'conclusao_estrategica': 1 parágrafo (3–5 frases) com a prioridade do próximo "
    "período."
)


def _brl(v) -> str:
    try:
        return f"{float(v or 0):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    except (TypeError, ValueError):
        return "0,00"


def _num(v) -> str:
    try:
        return f"{float(v or 0):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def _montar_contexto(analise) -> dict:
    """Resumo estatístico estruturado da análise (entrada do prompt)."""
    ind = analise.indicador
    emp = analise.empresa
    abc = list(analise.curva_abc or [])
    classe_a = [p for p in abc if p.classe_abc == "A"]
    perc_a = sum(float(p.percentual_individual or 0) for p in classe_a)
    ref = f"{analise.mes_referencia:02d}/{analise.ano_referencia}"
    top = abc[0] if abc else None
    return {
        "empresa": emp.nome_fantasia or emp.razao_social if emp else "—",
        "referencia": ref,
        "periodo": f"{analise.periodo_inicio.strftime('%d/%m/%Y')} a {analise.periodo_fim.strftime('%d/%m/%Y')}",
        "faturamento_total": float(ind.faturamento_total or 0) if ind else 0.0,
        "total_comprado": float(ind.total_comprado or 0) if ind else 0.0,
        "pressao_estoque": float(ind.saldo_estimado_compras_vendas or 0) if ind else 0.0,
        "produto_mais_vendido": (ind.produto_mais_vendido_nome if ind else None),
        "qtd_mais_vendido": float(ind.produto_mais_vendido_quantidade or 0) if ind else 0.0,
        "produto_maior_faturamento": (ind.produto_maior_faturamento_nome if ind else None),
        "valor_maior_faturamento": float(ind.produto_maior_faturamento_valor or 0) if ind else 0.0,
        "abc_classe_a_qtd": len(classe_a),
        "abc_classe_a_perc": round(perc_a, 1),
        "abc_total_produtos": len(abc),
        "abc_lider_nome": top.produto_nome if top else None,
        "abc_lider_perc": round(float(top.percentual_individual or 0), 2) if top else 0.0,
    }


def _prompt_usuario(c: dict) -> str:
    giro = "negativo (vendeu mais do que comprou — giro saudável)" if c["pressao_estoque"] < 0 \
        else "positivo (comprou mais do que vendeu — capital parado)"
    return (
        f"Dados da análise da empresa {c['empresa']} — referência {c['referencia']} "
        f"(período {c['periodo']}):\n"
        f"- Faturamento total: R$ {_brl(c['faturamento_total'])}\n"
        f"- Volume de compras (entradas): R$ {_brl(c['total_comprado'])}\n"
        f"- Indicador de Pressão de Estoque: R$ {_brl(c['pressao_estoque'])} — sinal {giro}\n"
        f"- Produto mais vendido (volume): {c['produto_mais_vendido'] or '—'} "
        f"({_num(c['qtd_mais_vendido'])} un)\n"
        f"- Maior faturamento individual: {c['produto_maior_faturamento'] or '—'} "
        f"(R$ {_brl(c['valor_maior_faturamento'])})\n"
        f"- Curva ABC: {c['abc_classe_a_qtd']} produto(s) na Classe A concentram "
        f"~{c['abc_classe_a_perc']:.0f}% do faturamento; líder {c['abc_lider_nome'] or '—'} "
        f"({c['abc_lider_perc']:.2f}% do total).\n\n"
        "Gere a devolutiva seguindo as regras."
    )


def _gerar_via_claude(ctx: dict) -> dict | None:
    """Chama o Claude. Retorna {resumo_executivo, conclusao_estrategica} ou None."""
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.info("ANTHROPIC_API_KEY ausente — devolutiva via fallback local.")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("Pacote 'anthropic' não instalado — usando fallback local.")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    modelo = current_app.config.get("ANTHROPIC_MODEL", "claude-opus-4-8")
    try:
        resp = client.messages.create(
            model=modelo,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "resumo_executivo": {"type": "string"},
                            "conclusao_estrategica": {"type": "string"},
                        },
                        "required": ["resumo_executivo", "conclusao_estrategica"],
                        "additionalProperties": False,
                    },
                },
            },
            system=_SYSTEM,
            messages=[{"role": "user", "content": _prompt_usuario(ctx)}],
        )
        if resp.stop_reason == "refusal":
            logger.warning("Claude recusou a geração da devolutiva — usando fallback.")
            return None
        texto = next((b.text for b in resp.content if b.type == "text"), "")
        dados = json.loads(texto)
        return {
            "resumo_executivo": dados["resumo_executivo"].strip(),
            "conclusao_estrategica": dados["conclusao_estrategica"].strip(),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("Falha ao gerar devolutiva via Claude: %s — usando fallback.", e)
        return None


def _gerar_fallback(c: dict) -> dict:
    """Gerador determinístico local (sem IA): texto honesto a partir dos números."""
    giro = c["pressao_estoque"] < 0
    resumo = (
        f"No período {c['referencia']}, a {c['empresa']} registrou faturamento total de "
        f"R$ {_brl(c['faturamento_total'])}, com volume de compras de "
        f"R$ {_brl(c['total_comprado'])} e Indicador de Pressão de Estoque de "
        f"R$ {_brl(c['pressao_estoque'])}. "
        + ("O sinal negativo aponta giro saudável: a loja escoou estoque mais rápido do que repôs. "
           if giro else
           "O sinal positivo indica compras acima das vendas — capital em estoque a observar. ")
        + f"Pela Curva ABC, {c['abc_classe_a_qtd']} produto(s) da Classe A concentram "
        f"~{c['abc_classe_a_perc']:.0f}% da receita"
        + (f", liderados por {c['abc_lider_nome']} ({c['abc_lider_perc']:.2f}% do total)."
           if c["abc_lider_nome"] else ".")
    )
    conclusao = (
        "A prioridade do próximo período é proteger o abastecimento dos produtos Classe A, "
        "que sustentam a maior fatia do faturamento. "
        + ("Mantido o ritmo de compras abaixo do de vendas, os campeões tendem à ruptura — "
           "recompor o giro evita desabastecimento. " if giro else
           "Ajustar o ritmo de compras ao de vendas libera capital de giro. ")
        + "Foco em disponibilidade do que já vende, não em ampliar o mix."
    )
    return {"resumo_executivo": resumo, "conclusao_estrategica": conclusao}


def _auditar(texto: dict) -> bool:
    """True se o texto NÃO contém termos proibidos (margem/lucro/CMV...)."""
    blob = (texto.get("resumo_executivo", "") + " " + texto.get("conclusao_estrategica", "")).lower()
    return not any(p in blob for p in _PROIBIDAS)


def gerar_devolutiva_ia(analise) -> dict:
    """
    Orquestra a geração. Retorna:
        {resumo_executivo, conclusao_estrategica, fonte: 'ia'|'fallback'}
    Garante integridade: se a saída da IA contiver termo proibido, descarta e
    usa o fallback (que é construído para nunca conter).
    """
    ctx = _montar_contexto(analise)
    via_ia = _gerar_via_claude(ctx)
    if via_ia and _auditar(via_ia):
        return {**via_ia, "fonte": "ia"}
    if via_ia:
        logger.warning("Saída da IA continha termo proibido — descartada; usando fallback.")
    return {**_gerar_fallback(ctx), "fonte": "fallback"}
