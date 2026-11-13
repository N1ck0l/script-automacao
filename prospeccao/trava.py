"""Trava de processo único por arquivo (.envio.lock) com checagem de PID."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ARQUIVO_TRAVA = BASE_DIR / ".envio.lock"
_TRAVAS_ATIVAS: dict[Path, int] = {}  # profundidade reentrante por caminho resolvido


def _processo_vivo(pid: int) -> bool:
    """True se o PID existe. (No Windows, os.kill(pid, 0) MATARIA o processo.)"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            codigo = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo))
            return codigo.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class EnvioEmAndamento(RuntimeError):
    """Outro processo já está enviando (ou usando o perfil do WhatsApp Web)."""


class TravaEnvio:
    """Garante UM único processo enviando por vez (arquivo .envio.lock com o PID).

    Reentrante no mesmo processo (por caminho de trava); trava de processo morto é descartada.
    """

    def __init__(self, caminho: Path | None = None) -> None:
        self.caminho = Path(caminho or ARQUIVO_TRAVA)
        self._chave = self.caminho.resolve()

    def __enter__(self) -> "TravaEnvio":
        profundidade = _TRAVAS_ATIVAS.get(self._chave, 0)
        if profundidade:
            _TRAVAS_ATIVAS[self._chave] = profundidade + 1
            return self
        for _ in range(2):
            try:
                fd = os.open(self.caminho, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    dono = int(self.caminho.read_text(encoding="utf-8").strip() or 0)
                except (OSError, ValueError):
                    dono = 0
                if dono and dono != os.getpid() and _processo_vivo(dono):
                    raise EnvioEmAndamento(
                        f"Outro envio está em andamento (processo {dono}). Aguarde "
                        f"ele terminar ou feche-o; se tiver certeza de que não há "
                        f"nenhum, apague {self.caminho.name}.")
                self.caminho.unlink(missing_ok=True)  # trava órfã
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            _TRAVAS_ATIVAS[self._chave] = 1
            return self
        raise EnvioEmAndamento(f"Não foi possível criar {self.caminho.name}.")

    def __exit__(self, *_exc) -> None:
        profundidade = _TRAVAS_ATIVAS.get(self._chave, 0) - 1
        if profundidade <= 0:
            _TRAVAS_ATIVAS.pop(self._chave, None)
            self.caminho.unlink(missing_ok=True)
        else:
            _TRAVAS_ATIVAS[self._chave] = profundidade

