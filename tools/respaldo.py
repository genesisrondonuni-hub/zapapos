"""Respaldo y restauración. Copia consistente con la API de respaldo de SQLite (se puede hacer con la caja abierta),
verificada con integridad + SHA-256, rotada (14 días, 8 semanas, 6 meses) y con segunda copia opcional (USB/otro disco).
   python -m tools.respaldo respaldar|verificar|restaurar|listar ..."""
import hashlib, json, os, re, shutil, sqlite3, sys, tempfile, threading, time, zipfile
from datetime import datetime, timedelta, timezone
from tools.db import connect, migrate

PATRON = re.compile(r"^respaldo_(\d{8})_(\d{6})(?:_\d+)?\.zip$")
CONTAR = ("ventas", "usuarios", "variantes", "pagos", "inventario_movimientos")


class RespaldoError(Exception):
    pass


def _sha(ruta):
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _fecha(nombre):
    m = PATRON.match(nombre)
    return datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc) if m else None


def listar(carpeta):
    if not os.path.isdir(carpeta):
        return []
    items = [(n, _fecha(n)) for n in os.listdir(carpeta) if PATRON.match(n)]
    return [{"archivo": os.path.join(carpeta, n), "fecha_utc": f.isoformat(), "bytes": os.path.getsize(os.path.join(carpeta, n))}
            for n, f in sorted(items, key=lambda x: x[1], reverse=True)]


def respaldar(db_path, carpeta, ahora=None) -> dict:
    ahora = ahora or datetime.now(timezone.utc)
    os.makedirs(carpeta, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=carpeta, prefix=".tmp_")
    try:
        copia = os.path.join(tmp, "zapateria.db")
        src = sqlite3.connect(db_path, timeout=30); dst = sqlite3.connect(copia)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RespaldoError("La base de datos tiene errores de integridad: no se respaldó")
            esquema = [r[0] for r in dst.execute("SELECT nombre FROM schema_migrations ORDER BY nombre")]
            inst = dst.execute("SELECT valor FROM config WHERE clave='instalacion_id'").fetchone()
            filas = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in CONTAR}
        finally:
            src.close(); dst.close()
        man = {"version_respaldo": 1, "fecha_utc": ahora.isoformat(), "esquema": esquema, "instalacion_id": inst[0] if inst else None,
               "sha256": _sha(copia), "bytes": os.path.getsize(copia), "filas": filas}
        base, n = f"respaldo_{ahora:%Y%m%d_%H%M%S}", 1
        final = os.path.join(carpeta, base + ".zip")
        while os.path.exists(final):
            n += 1; final = os.path.join(carpeta, f"{base}_{n}.zip")
        parcial = os.path.join(tmp, "respaldo.zip")
        with zipfile.ZipFile(parcial, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copia, "zapateria.db"); z.writestr("manifest.json", json.dumps(man, indent=1))
        os.replace(parcial, final)                      # nunca queda un zip a medias a la vista
        return {"archivo": final, **man}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verificar_respaldo(zip_path) -> dict:
    try:
        with zipfile.ZipFile(zip_path) as z:
            if z.testzip() is not None:
                raise RespaldoError("El archivo de respaldo está dañado")
            man = json.loads(z.read("manifest.json"))
            tmp = tempfile.mkdtemp()
            try:
                z.extract("zapateria.db", tmp)
                ruta = os.path.join(tmp, "zapateria.db")
                if _sha(ruta) != man["sha256"]:
                    raise RespaldoError("El respaldo fue alterado o se dañó (la huella no coincide)")
                con = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True)
                try:
                    if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RespaldoError("La base dentro del respaldo tiene errores de integridad")
                finally:
                    con.close()
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as e:
        raise RespaldoError(f"No es un respaldo válido: {e}") from e
    return man


def restaurar(zip_path, db_path) -> dict:
    """Detenga la caja antes. Deja una copia de seguridad de la base actual y, si algo falla, vuelve a ella."""
    t0 = time.monotonic()
    man = verificar_respaldo(zip_path)                    # si el respaldo no sirve, no se toca nada
    db_path = os.path.abspath(db_path)
    tmp = tempfile.mkdtemp(dir=os.path.dirname(db_path))
    seguridad = None
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extract("zapateria.db", tmp)
        nuevo = os.path.join(tmp, "zapateria.db")
        if os.path.exists(db_path):
            seguridad = f"{db_path}.antes_de_restaurar_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
            try:
                a, b = sqlite3.connect(db_path, timeout=5), sqlite3.connect(seguridad)
                a.backup(b); a.close(); b.close()
            except sqlite3.DatabaseError:                 # base actual dañada: se conserva tal cual
                shutil.copy2(db_path, seguridad)
        for ext in ("-wal", "-shm"):
            if os.path.exists(db_path + ext):
                os.remove(db_path + ext)
        os.replace(nuevo, db_path)
        try:
            con = connect(db_path); migrado = migrate(con)
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; con.close()
            if not ok:
                raise RespaldoError("La base restaurada no pasó la verificación")
        except Exception:
            if seguridad:
                os.replace(seguridad, db_path)
            raise
        return {"seguridad": seguridad, "migraciones": migrado, "segundos": round(time.monotonic() - t0, 2), "filas": man["filas"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def rotar(carpeta, diarios=14, semanales=8, mensuales=6) -> list:
    """Conserva el más reciente de cada uno de los últimos N días, semanas y meses. Borra el resto."""
    items = [(r["archivo"], _fecha(os.path.basename(r["archivo"]))) for r in listar(carpeta)]      # recientes primero
    conservar = set()
    for clave, n in ((lambda f: f.date(), diarios), (lambda f: f.isocalendar()[:2], semanales), (lambda f: (f.year, f.month), mensuales)):
        vistos = []
        for ruta, f in items:
            k = clave(f)
            if k not in vistos:
                vistos.append(k)
                if len(vistos) <= n:
                    conservar.add(ruta)
    borrados = [r for r, _ in items if r not in conservar]
    for r in borrados:
        os.remove(r)
    return borrados


def respaldo_diario(db_path, carpeta, copia=None, ahora=None, utc_offset_min=-240):
    """Uno por día (hora local). Devuelve None si hoy ya hay uno."""
    ahora = ahora or datetime.now(timezone.utc)
    hoy = (ahora + timedelta(minutes=utc_offset_min)).date()
    if any((_fecha(os.path.basename(r["archivo"])) + timedelta(minutes=utc_offset_min)).date() == hoy for r in listar(carpeta)):
        return None
    r = respaldar(db_path, carpeta, ahora)
    out = {"archivo": r["archivo"], "copia": None, "copia_error": None}
    if copia:
        try:
            os.makedirs(copia, exist_ok=True)
            out["copia"] = shutil.copy2(r["archivo"], copia)
            rotar(copia)
        except OSError as e:                       # el USB no está: el respaldo principal ya existe
            out["copia_error"] = str(e)
    rotar(carpeta)
    return out


def estado(carpeta, ahora=None, max_horas=36) -> dict:
    ahora = ahora or datetime.now(timezone.utc)
    ult = listar(carpeta)
    if not ult:
        return {"ultimo": None, "edad_horas": None, "alerta": True, "mensaje": "Todavía no hay ningún respaldo de la información"}
    edad = (ahora - datetime.fromisoformat(ult[0]["fecha_utc"])).total_seconds() / 3600
    msg = f"El último respaldo tiene {int(edad // 24)} día(s): respalde ahora" if edad > max_horas else "Respaldos al día"
    return {"ultimo": ult[0], "edad_horas": round(edad, 1), "alerta": edad > max_horas, "mensaje": msg, "cantidad": len(ult)}


def configuracion(con, db_path):
    g = lambda k: (con.execute("SELECT valor FROM config WHERE clave=?", (k,)).fetchone() or [""])[0]
    return (g("respaldo_carpeta") or os.path.join(os.path.dirname(os.path.abspath(db_path)), "respaldos"), g("respaldo_copia") or None,
            int(g("utc_offset_min") or -240))


def iniciar_respaldos(db_path, intervalo=3600):
    """Hilo de fondo: al arrancar y cada hora verifica que hoy haya respaldo."""
    parar = threading.Event()

    def bucle():
        while not parar.is_set():
            try:
                con = connect(db_path); carpeta, copia, off = configuracion(con, db_path); con.close()
                respaldo_diario(db_path, carpeta, copia, utc_offset_min=off)
            except Exception as e:
                print(f"[respaldo] error: {e}", file=sys.stderr)
            parar.wait(intervalo)
    hilo = threading.Thread(target=bucle, daemon=True); hilo.start()
    return hilo, parar


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        if a[:1] == ["respaldar"] and len(a) >= 3:
            r = respaldar(a[1], a[2]); print("Respaldo creado:", r["archivo"], r["filas"])
        elif a[:1] == ["verificar"] and len(a) == 2:
            print("Respaldo válido:", verificar_respaldo(a[1])["filas"])
        elif a[:1] == ["restaurar"] and len(a) == 3:
            print("Restaurado:", restaurar(a[1], a[2]))
        elif a[:1] == ["listar"] and len(a) == 2:
            [print(x["archivo"], x["fecha_utc"]) for x in listar(a[1])]
        else:
            print("Uso: respaldar BD CARPETA | verificar ZIP | restaurar ZIP BD | listar CARPETA"); sys.exit(2)
    except RespaldoError as e:
        print("ERROR:", e); sys.exit(1)
