"""Lanzador estable de ZapaPOS (no se actualiza con las versiones). Lee qué versión usar y abre la caja en el navegador."""
import subprocess, sys, threading, time, webbrowser
from pathlib import Path


def comando_arranque(inst, puerto=8787):
    inst = Path(inst)
    version = (inst / "version_actual.txt").read_text().strip()
    return [sys.executable, "-m", "app.server", "--db", str(inst / "datos" / "zapateria.db"), "--port", str(puerto)], str(inst / "versiones" / version)


if __name__ == "__main__":
    cmd, cwd = comando_arranque(Path(__file__).resolve().parent)
    threading.Thread(target=lambda: (time.sleep(2.5), webbrowser.open("http://127.0.0.1:8787")), daemon=True).start()
    sys.exit(subprocess.call(cmd, cwd=cwd))
