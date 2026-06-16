"""
NEXO - Faturamento Inteligente | NexoBot (assistente de suporte do portal)
==========================================================================
Base de conhecimento ÚNICA (GUIA_TOPICOS) consumida tanto pela aba "Guia"
quanto pelo NexoBot — assim Guia e bot nunca divergem.

Estratégia do bot (open source / gratuito, com fail-safe):
  1) Se HF_API_TOKEN estiver configurado, tenta a Inference API gratuita da
     Hugging Face (modelo instruct tipo Mistral/Llama-3), com prompt rígido.
  2) Em qualquer falha (sem token, sem rede, timeout, erro), cai num
     FALLBACK DETERMINÍSTICO por similaridade de palavras-chave contra a base —
     robusto e instantâneo. O bot NUNCA fica mudo.

Escopo rígido: o NexoBot só fala do uso do portal (upload, senha, exportação
do PDV, navegação). Dúvida fora do escopo → orienta Guia ou abrir chamado.
"""

import logging
import re
import unicodedata

from flask import current_app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Base de conhecimento (fonte única: Guia + bot)
# ---------------------------------------------------------------------
GUIA_TOPICOS = [
    {
        "id": "upload",
        "icone": "bi-cloud-arrow-up",
        "titulo": "Como fazer o upload dos relatórios?",
        "resumo": "Você envia os relatórios de Vendas e Compras na aba “Enviar relatórios”; a equipe NEXO valida e processa.",
        "passos": [
            "No menu lateral, clique em “Enviar relatórios”.",
            "Escolha a análise em aberto correspondente ao período desejado.",
            "Anexe o arquivo de VENDAS e o de COMPRAS (formatos aceitos: .csv ou .xlsx).",
            "Confirme o envio — o status fica como “Aguardando validação”.",
            "A equipe NEXO homologa e processa; quando a devolutiva é publicada, você é avisado pelo sininho e por e-mail.",
        ],
        "keywords": ["upload", "enviar", "envio", "relatorio", "relatorios", "planilha",
                     "anexar", "anexo", "arquivo", "subir", "mandar", "carregar"],
    },
    {
        "id": "senha",
        "icone": "bi-shield-lock",
        "titulo": "Como altero minha senha de acesso?",
        "resumo": "No primeiro acesso o portal pede a troca; depois, use “Esqueci minha senha” na tela de login.",
        "passos": [
            "Primeiro acesso: ao entrar pela primeira vez, o portal obriga você a definir uma senha pessoal antes de liberar o painel.",
            "Esqueceu a senha? Na tela de login, clique em “Esqueci minha senha”.",
            "Informe seu e-mail cadastrado — enviamos um link seguro, válido por 15 minutos.",
            "Abra o link e defina a nova senha. Depois é só fazer login com ela.",
        ],
        "keywords": ["senha", "trocar", "alterar", "mudar", "esqueci", "recuperar",
                     "redefinir", "password", "acesso", "esquecer"],
    },
    {
        "id": "exportar_pdv",
        "icone": "bi-file-earmark-spreadsheet",
        "titulo": "Como exportar os relatórios do meu sistema PDV?",
        "resumo": "Exporte dois arquivos do seu PDV/ERP — um de Vendas e um de Compras — em .xlsx ou .csv, nomeando por período.",
        "passos": [
            "No seu sistema PDV/ERP, gere o relatório de VENDAS do período de referência.",
            "Gere também o relatório de COMPRAS do mesmo período.",
            "Exporte ambos em Excel (.xlsx) ou CSV (.csv).",
            "Nomeie de forma clara por período — ex.: “Relatório mensal março 2026 - vendas.xlsx” e “Relatório mensal março 2026 - compras.xlsx”.",
            "Volte ao portal, abra “Enviar relatórios” e anexe os dois arquivos.",
        ],
        "keywords": ["exportar", "exportacao", "pdv", "erp", "sistema", "gerar", "extrair",
                     "excel", "xlsx", "csv", "nome", "nomear", "mensal", "periodo", "formato"],
    },
]

_POR_ID = {t["id"]: t for t in GUIA_TOPICOS}

SYSTEM_PROMPT = (
    "Você é o NexoBot, assistente do portal NEXO - Faturamento Inteligente. "
    "Responda SOMENTE dúvidas sobre o uso do portal: upload de relatórios, troca "
    "e recuperação de senha, exportação dos relatórios do PDV/ERP e navegação. "
    "Use o passo a passo do Guia. Seja conciso, em português do Brasil, tom amigável. "
    "Se a dúvida fugir desse escopo ou for complexa, oriente o usuário a consultar "
    "a aba “Guia” ou a abrir um chamado em “Suporte”. Não invente funcionalidades."
)

_STOPWORDS = {
    "a", "o", "os", "as", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
    "um", "uma", "para", "pra", "por", "com", "que", "como", "meu", "minha", "eu",
    "se", "ao", "the", "is", "qual", "quero", "preciso", "faco", "fazer", "voce",
}


def _norm(texto: str) -> str:
    s = unicodedata.normalize("NFKD", str(texto or "")).lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(texto: str) -> set:
    return {t for t in _norm(texto).split() if len(t) > 2 and t not in _STOPWORDS}


def _formatar_resposta(topico: dict) -> str:
    passos = "\n".join(f"{i}. {p}" for i, p in enumerate(topico["passos"], 1))
    return f"{topico['resumo']}\n\n{passos}"


def _melhor_topico(mensagem: str):
    """Pontua cada tópico pela interseção de palavras-chave. Retorna (topico, score)."""
    toks = _tokens(mensagem)
    if not toks:
        return None, 0
    melhor, melhor_score = None, 0
    for t in GUIA_TOPICOS:
        score = len(toks & set(t["keywords"]))
        # bônus se a mensagem contém o id/título normalizado
        if score > melhor_score:
            melhor, melhor_score = t, score
    return melhor, melhor_score


def _saudacao(mensagem: str) -> bool:
    # Usa as palavras CRUAS (sem o filtro de len>2 do _tokens, que removeria "oi"/"ei").
    palavras = set(_norm(mensagem).split())
    return bool(palavras & {"oi", "ola", "ei", "opa", "bom", "boa", "dia", "tarde", "noite", "hello", "hey"})


def _quer_humano(mensagem: str) -> bool:
    n = _norm(mensagem)
    return any(p in n for p in ("chamado", "humano", "atendente", "suporte", "pessoa", "falar com"))


def _responder_local(mensagem: str) -> str:
    """Fallback determinístico — sempre responde algo útil."""
    if _quer_humano(mensagem):
        return ("Posso te ajudar com dúvidas do portal por aqui. 🙂 Se preferir falar "
                "com a equipe NEXO, abra um chamado na aba “Suporte” — respondemos por lá.")
    topico, score = _melhor_topico(mensagem)
    if topico and score >= 1:
        return _formatar_resposta(topico)
    if _saudacao(mensagem):
        return ("Olá! Eu sou o NexoBot 🤖. Posso te ajudar com: enviar relatórios, "
                "alterar/recuperar senha e exportar os relatórios do seu PDV. "
                "Sobre o que você precisa de ajuda?")
    return ("Não tenho certeza se entendi. 🤔 Posso ajudar com: **upload de relatórios**, "
            "**senha de acesso** e **exportar relatórios do PDV**. Tente reformular, "
            "confira a aba “Guia” para o passo a passo completo, ou abra um chamado em "
            "“Suporte” se precisar falar com a equipe.")


def _responder_hf(mensagem: str) -> str | None:
    """Tenta a Inference API gratuita da Hugging Face. Retorna texto ou None."""
    token = current_app.config.get("HF_API_TOKEN")
    if not token:
        return None
    modelo = current_app.config.get("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")
    contexto = "\n\n".join(f"- {t['titulo']} {t['resumo']}" for t in GUIA_TOPICOS)
    prompt = (
        f"<s>[INST] {SYSTEM_PROMPT}\n\nBase de conhecimento (Guia):\n{contexto}\n\n"
        f"Pergunta do usuário: {mensagem.strip()} [/INST]"
    )
    try:
        import httpx
        resp = httpx.post(
            f"https://api-inference.huggingface.co/models/{modelo}",
            headers={"Authorization": f"Bearer {token}"},
            json={"inputs": prompt, "parameters": {
                "max_new_tokens": 300, "temperature": 0.3, "return_full_text": False}},
            timeout=12.0,
        )
        resp.raise_for_status()
        dados = resp.json()
        if isinstance(dados, list) and dados and dados[0].get("generated_text"):
            return dados[0]["generated_text"].strip()
        logger.warning("Resposta inesperada da HF Inference API: %s", str(dados)[:200])
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Falha na HF Inference API (%s) — usando fallback local.", e)
        return None


def responder(mensagem: str) -> dict:
    """
    Resposta do NexoBot. Retorna {'resposta': str, 'fonte': 'hf'|'local'}.
    Sempre entrega algo útil (fallback determinístico).
    """
    mensagem = (mensagem or "").strip()
    if not mensagem:
        return {"resposta": "Pode escrever sua dúvida sobre o portal que eu te ajudo. 🙂", "fonte": "local"}

    via_hf = _responder_hf(mensagem)
    if via_hf:
        return {"resposta": via_hf, "fonte": "hf"}
    return {"resposta": _responder_local(mensagem), "fonte": "local"}
