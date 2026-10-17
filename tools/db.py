"""Conexión SQLite con PRAGMAs de resiliencia y runner de migraciones."""
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "database" / "migrations"


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10, isolation_level=None)  # transacciones manuales
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")   # no perder ventas ante corte de luz
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


@contextmanager
def transaccion(con: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT; si ya hay una transacción abierta, se anida sin efecto."""
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def migrate(con: sqlite3.Connection, carpeta=None) -> list[str]:
    con.execute("CREATE TABLE IF NOT EXISTS schema_migrations(nombre TEXT PRIMARY KEY, aplicada_en TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    hechas = {r[0] for r in con.execute("SELECT nombre FROM schema_migrations")}
    aplicadas = []
    for f in sorted((Path(carpeta) if carpeta else MIGRATIONS).glob("*.sql")):
        if f.name in hechas:
            continue
        con.execute("BEGIN IMMEDIATE")
        try:
            for stmt in _split(f.read_text(encoding="utf-8")):
                con.execute(stmt)
            con.execute("INSERT INTO schema_migrations(nombre) VALUES (?)", (f.name,))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        aplicadas.append(f.name)
    return aplicadas


def _split(sql: str):
    """Separa sentencias aunque haya varias en una línea. Un ';' dentro de un trigger (BEGIN ... END) no corta."""
    inicio = 0
    for i, ch in enumerate(sql):
        if ch == ";" and sqlite3.complete_statement(sql[inicio:i + 1]):
            if sql[inicio:i + 1].strip():
                yield sql[inicio:i + 1]
            inicio = i + 1
    resto = re.sub(r"--[^\n]*", "", sql[inicio:]).strip()
    if resto:
        raise ValueError("SQL incompleto en migración")


if __name__ == "__main__":
    import sys
    ruta = sys.argv[1] if len(sys.argv) > 1 else "zapateria.db"
    c = connect(ruta)
    print("Migraciones aplicadas:", migrate(c) or "ninguna (al día)")
