"""Notificações push opcionais (Telegram Bot API) para leads quentes."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def eh_lead_quente(categoria: str, config: dict | None = None) -> bool:
    """Retorna True se a categoria deve disparar alerta imediato."""
    cats = (config or {}).get("telegram_categorias") or ["INTERESSE", "DUVIDA"]
    return str(categoria or "").upper() in {str(c).upper() for c in cats}


def notificar_telegram(
    titulo: str,
    texto: str,
    config: dict | None = None,
    timeout: float = 5.0,
) -> bool:
    """Envia alerta via Telegram Bot API se token e chat_id estiverem configurados.

    Nunca levanta exceção em caso de falha de rede ou configuração ausente.
    """
    cfg = config or {}
    token = str(cfg.get("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = str(cfg.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return False

    corpo = f"{titulo}\n\n{texto}".strip() if texto else titulo
    payload = json.dumps({
        "chat_id": chat_id,
        "text": corpo[:4000],
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url=f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except urllib.error.HTTPError as exc:
        exc.close()
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return False

