"""
NEXO - Faturamento Inteligente | NexoBot (assistente de suporte do portal)
==========================================================================
A base de conhecimento agora é DINÂMICA: vive na tabela `guia_topico`,
gerenciável pelo Admin (CMS). O NexoBot e a aba "Guia" leem da MESMA tabela
(DRY — fonte única de verdade).

Estratégia do bot (open source / gratuito, com fail-safe):
  1) Se HF_API_TOKEN estiver configurado, tenta a Inference API gratuita da
     Hugging Face (modelo instruct tipo Mistral/Llama-3), com prompt rígido.
  2) Em qualquer falha (sem token, sem rede, timeout, erro), cai num
     FALLBACK DETERMINÍSTICO por similaridade de palavras-chave contra a base —
     robusto e instantâneo. O bot NUNCA fica mudo.

Escopo rígido: o NexoBot só fala do uso do portal. Dúvida fora do escopo →
orienta a aba "Guia" ou abrir um chamado.
"""

import logging
import re
import unicodedata

from flask import current_app
from sqlalchemy import select

from extensions import db
from models import GuiaTopico

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Você é o NexoBot, assistente do portal NEXO - Faturamento Inteligente. "
    "Responda SOMENTE dúvidas sobre o uso do portal (upload de relatórios, troca "
    "e recuperação de senha, exportação dos relatórios do PDV/ERP e navegação). "
    "Use o passo a passo da base de conhecimento. Seja conciso, em português do "
    "Brasil, tom amigável. Se a dúvida fugir desse escopo ou for complexa, oriente "
    "o usuário a consultar a aba “Guia” ou a abrir um chamado em “Suporte”. "
    "Não invente funcionalidades."
)

_STOPWORDS = {
    "a", "o", "os", "as", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
    "um", "uma", "para", "pra", "por", "com", "que", "como", "meu", "minha", "eu",
    "se", "ao", "the", "is", "qual", "quero", "preciso", "faco", "fazer", "voce",
    "sobre", "tem", "ter", "ser", "esta", "este", "isso", "aqui", "onde", "quando",
}


def _norm(texto: str) -> str:
    s = unicodedata.normalize("NFKD", str(texto or "")).lower()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(texto: str) -> set:
    return {t for t in _norm(texto).split() if len(t) > 2 and t not in _STOPWORDS}


def _carregar_topicos() -> list:
    """
    Lê a base de conhecimento do banco (tópicos ativos). Para cada um, deriva o
    conjunto de palavras-chave de pergunta + resposta + categoria (sem coluna
    extra de keywords — mantém o CMS simples para o Admin).
    """
    topicos = db.session.execute(
        select(GuiaTopico)
        .where(GuiaTopico.ativo.is_(True))
        .order_by(GuiaTopico.categoria, GuiaTopico.id)
    ).scalars().all()
    out = []
    for t in topicos:
        out.append({
            "id": t.id,
            "categoria": t.categoria,
            "pergunta": t.pergunta,
            "resposta": t.resposta,
            "imagem_url": t.imagem_url,
            "keywords": _tokens(f"{t.pergunta} {t.resposta} {t.categoria}"),
        })
    return out


def _melhor_topico(mensagem: str, topicos: list):
    """Pontua cada tópico pela interseção de palavras-chave. Retorna (topico, score)."""
    toks = _tokens(mensagem)
    if not toks:
        return None, 0
    melhor, melhor_score = None, 0
    for t in topicos:
        score = len(toks & t["keywords"])
        if score > melhor_score:
            melhor, melhor_score = t, score
    return melhor, melhor_score


def _saudacao(mensagem: str) -> bool:
    palavras = set(_norm(mensagem).split())
    return bool(palavras & {"oi", "ola", "ei", "opa", "bom", "boa", "dia", "tarde", "noite", "hello", "hey"})


def _quer_humano(mensagem: str) -> bool:
    n = _norm(mensagem)
    return any(p in n for p in ("chamado", "humano", "atendente", "suporte", "pessoa", "falar com"))


def _responder_local(mensagem: str) -> str:
    """Fallback determinístico — sempre responde algo útil, lendo do banco."""
    if _quer_humano(mensagem):
        return ("Posso te ajudar com dúvidas do portal por aqui. 🙂 Se preferir falar "
                "com a equipe NEXO, abra um chamado na aba “Suporte” — respondemos por lá.")
    topicos = _carregar_topicos()
    topico, score = _melhor_topico(mensagem, topicos)
    if topico and score >= 1:
        return f"**{topico['pergunta']}**\n\n{topico['resposta']}"
    if _saudacao(mensagem):
        cats = sorted({t["categoria"] for t in topicos})
        ajuda = ", ".join(cats) if cats else "uso do portal"
        return (f"Olá! Eu sou o NexoBot 🤖. Posso te ajudar com: {ajuda}. "
                "Sobre o que você precisa de ajuda?")
    return ("Não tenho certeza se entendi. 🤔 Confira a aba “Guia” para o passo a passo "
            "completo, tente reformular a pergunta, ou abra um chamado em “Suporte” "
            "se precisar falar com a equipe NEXO.")


def _responder_hf(mensagem: str) -> str | None:
    """Tenta a Inference API gratuita da Hugging Face. Retorna texto ou None."""
    token = current_app.config.get("HF_API_TOKEN")
    if not token:
        return None
    modelo = current_app.config.get("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")
    topicos = _carregar_topicos()
    contexto = "\n\n".join(f"- {t['pergunta']}: {t['resposta']}" for t in topicos) or "(base vazia)"
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
