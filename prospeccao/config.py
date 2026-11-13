"""Carregamento de configuração regional/operacional (config.local.json ou config.json)."""

from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_PADRAO: dict = {
    "cidade_padrao": "Aracaju",
    "feriados_locais": [
        [7, 8],   # Emancipação Política de Sergipe
        [3, 17],  # Aniversário de Aracaju
        [12, 8],  # N. Sra. da Conceição
    ],
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_categorias": ["INTERESSE", "DUVIDA"],
}


def carregar_config(base_dir: Path = BASE_DIR) -> dict:
    """Lê config.local.json (prioridade) ou config.json, mesclando com CONFIG_PADRAO."""
    cfg = dict(CONFIG_PADRAO)
    for nome in ("config.json", "config.local.json"):
        caminho = base_dir / nome
        if caminho.is_file():
            try:
                dados = json.loads(caminho.read_text(encoding="utf-8"))
                if isinstance(dados, dict):
                    cfg.update(dados)
            except (OSError, ValueError):
                pass
    return cfg

