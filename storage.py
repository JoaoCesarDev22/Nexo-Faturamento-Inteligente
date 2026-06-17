"""
NEXO - Faturamento Inteligente | Camada de armazenamento de arquivos
====================================================================
PONTO ÚNICO de acesso ao storage dos relatórios ERP (Vendas/Compras). Hoje
grava no filesystem local (UPLOAD_FOLDER). Centralizar aqui resolve o ponto
nevrálgico do item de auditoria #9: em deploy cloud com múltiplas instâncias
(ou disco efêmero), o FS local não persiste/escala.

PARA MIGRAR PARA OBJECT STORAGE (Supabase Storage / S3), basta trocar o corpo
destas 3 funções (ex.: boto3 client.put_object / get_object / delete_object) e
guardar em `caminho_arquivo` a *key* do objeto em vez do path local — nenhuma
rota/route precisa mudar, pois todas falam só com esta interface.
"""

import os
from pathlib import Path

from flask import current_app


def _pasta() -> Path:
    pasta = Path(current_app.config["UPLOAD_FOLDER"])
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def salvar(conteudo: bytes, nome_final: str) -> str:
    """Grava os bytes com o nome dado e devolve a referência persistida
    (hoje, o caminho absoluto no FS). Em object storage, devolveria a key."""
    caminho = _pasta() / nome_final
    caminho.write_bytes(conteudo)
    return str(caminho)


def resolver(caminho_arquivo: str) -> str:
    """Resolve a referência salva para um caminho absoluto legível. Relativiza
    ao UPLOAD_FOLDER quando o valor persistido não existe como caminho direto."""
    caminho = caminho_arquivo
    if not os.path.isabs(caminho) and not os.path.exists(caminho):
        caminho = os.path.join(current_app.config["UPLOAD_FOLDER"], os.path.basename(caminho))
    return caminho


def remover(caminho_arquivo: str) -> None:
    """Remove o arquivo físico (best-effort: erros de IO são silenciados, pois
    a remoção do registro no banco é o que de fato importa)."""
    if not caminho_arquivo:
        return
    try:
        alvo = resolver(caminho_arquivo)
        if alvo and os.path.exists(alvo):
            os.remove(alvo)
    except OSError:
        pass
