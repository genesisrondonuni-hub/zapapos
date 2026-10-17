"""Actualizaciones seguras. Cada versión vive en su carpeta (versiones/X.Y.Z) y un archivo (version_actual.txt) indica cuál se usa.
Un paquete va firmado (Ed25519) y se prueba ANTES de tocar nada: firma, huellas, sintaxis, migración sobre una copia de la base
y arranque real de la nueva versión. Luego: respaldo, migración real y cambio de versión; si algo falla, todo vuelve a como estaba.
Aplíquela con la caja CERRADA.   python -m tools.actualizar generar-claves|empaquetar|verificar|aplicar|revertir|version ..."""
import base64, hashlib, json, os, queue, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.request, zipfile
from datetime import datetime, timezone
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tools import respaldo as RS
from tools.db import connect, migrate

PERMITIDAS = ("app/", "core/", "tools/", "database/", "fonts/")


class ActualizacionError(Exception):
    pass


def _semver(v):
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(v))
    if not m:
        raise ActualizacionError(f"Versión inválida: {v!r} (use X.Y.Z)")
    return tuple(int(x) for x in m.groups())


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def generar_claves(carpeta):
    """La clave PRIVADA se queda solo con usted (el proveedor). La pública va en cada instalación."""
    os.makedirs(carpeta, exist_ok=True)
    k = Ed25519PrivateKey.generate()
    priv, pub = Path(carpeta) / "clave_privada.pem", Path(carpeta) / "clave_publica.pem"
    priv.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    pub.write_bytes(k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return str(priv), str(pub)


def empaquetar(origen, destino_zip, version, clave_privada, min_version=None, notas=""):
    _semver(version)
    if min_version:
        _semver(min_version)
    origen, archivos = Path(origen), {}
    for p in sorted(origen.rglob("*")):
        rel = p.relative_to(origen).as_posix()
        if p.is_file() and rel.startswith(PERMITIDAS) and "__pycache__" not in rel and not rel.endswith(".pyc"):
            archivos[rel] = _sha(p.read_bytes())
    man = {"version": version, "min_version": min_version, "fecha": datetime.now(timezone.utc).isoformat(), "notas": notas, "archivos": archivos}
    mb = json.dumps(man, sort_keys=True, ensure_ascii=False, indent=1).encode("utf-8")
    k = serialization.load_pem_private_key(Path(clave_privada).read_bytes(), None)
    with zipfile.ZipFile(destino_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in archivos:
            z.write(origen / rel, rel)
        z.writestr("manifest.json", mb); z.writestr("manifest.sig", base64.b64encode(k.sign(mb)))
    return str(destino_zip)


def _seguro(n):
    return n.startswith(PERMITIDAS) and not n.startswith("/") and "\\" not in n and ".." not in n.split("/")


def verificar_paquete(paquete, clave_publica) -> dict:
    try:
        with zipfile.ZipFile(paquete) as z:
            if z.testzip() is not None:
                raise ActualizacionError("El paquete está dañado")
            mb, sig = z.read("manifest.json"), base64.b64decode(z.read("manifest.sig"))
            try:
                serialization.load_pem_public_key(Path(clave_publica).read_bytes()).verify(sig, mb)
            except InvalidSignature:
                raise ActualizacionError("Firma inválida: este paquete no viene de su proveedor")
            man = json.loads(mb)
            _semver(man["version"])
            nombres = {n for n in z.namelist() if not n.endswith("/")} - {"manifest.json", "manifest.sig"}
            if nombres - set(man["archivos"]):
                raise ActualizacionError("El paquete trae archivos que no están en su manifiesto")
            for n, h in man["archivos"].items():
                if not _seguro(n):
                    raise ActualizacionError(f"Ruta no permitida en el paquete: {n}")
                if n not in nombres or _sha(z.read(n)) != h:
                    raise ActualizacionError(f"El archivo {n} falta o fue alterado")
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as e:
        raise ActualizacionError(f"Paquete inválido: {e}") from e
    return man


def version_actual(inst):
    try:
        return (Path(inst) / "version_actual.txt").read_text().strip()
    except OSError:
        raise ActualizacionError("No se encontró la instalación (falta version_actual.txt)")


def _puntero(inst, v):
    tmp = Path(inst) / "version_actual.tmp"
    tmp.write_text(v + "\n"); os.replace(tmp, Path(inst) / "version_actual.txt")


def historial(inst):
    p = Path(inst) / "actualizaciones.json"
    return json.loads(p.read_text()) if p.exists() else []


def _anotar(inst, reg):
    h = historial(inst) + [{"fecha": datetime.now(timezone.utc).isoformat(), **reg}]
    (Path(inst) / "actualizaciones.json").write_text(json.dumps(h, indent=1, ensure_ascii=False))


def prueba_de_arranque(dir_version, db_path, espera=30):
    """Arranca de verdad la versión indicada con una base de prueba y consulta la caja."""
    p = subprocess.Popen([sys.executable, "-u", "-m", "app.server", "--db", str(db_path), "--port", "0"], cwd=str(dir_version),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    q, salida, puerto = queue.Queue(), [], None
    threading.Thread(target=lambda: [q.put(l) for l in p.stdout], daemon=True).start()
    try:
        t0 = time.monotonic()
        while puerto is None and time.monotonic() - t0 < espera:
            try:
                l = q.get(timeout=0.3); salida.append(l)
                m = re.search(r"127\.0\.0\.1:(\d+)", l)
                puerto = int(m[1]) if m else None
            except queue.Empty:
                if p.poll() is not None:
                    break
        if puerto is None:
            raise ActualizacionError("La nueva versión no arrancó: " + "".join(salida)[-400:].strip())
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/instalacion", timeout=10) as r:
            if "configurado" not in json.loads(r.read()):
                raise ActualizacionError("La nueva versión arrancó pero respondió algo inesperado")
    except OSError as e:
        raise ActualizacionError(f"La nueva versión no respondió: {e}") from e
    finally:
        p.kill(); p.wait(); p.stdout.close()
    return True


def aplicar(paquete, inst, conservar=3, prueba=None, forzar=False, ahora=None) -> dict:
    t0, prueba = time.monotonic(), prueba or prueba_de_arranque
    inst = Path(inst).resolve(); db = inst / "datos" / "zapateria.db"
    man = verificar_paquete(paquete, inst / "clave_publica.pem")
    nueva, actual = man["version"], version_actual(inst)
    if _semver(nueva) <= _semver(actual):
        raise ActualizacionError(f"Ya tiene la versión {actual} (o una más nueva): no se aplicó {nueva}")
    if man.get("min_version") and _semver(actual) < _semver(man["min_version"]):
        raise ActualizacionError(f"Esta actualización requiere tener al menos la versión {man['min_version']} (tiene {actual})")
    if not forzar:                                        # actualizar es parte del mantenimiento; soporte puede forzarlo si hace falta
        from core import licencia
        c = connect(db)
        try:
            lic = licencia.estado(c, inst / "clave_publica.pem", ahora)
        finally:
            c.close()
        if not lic["puede_actualizar"]:
            raise ActualizacionError(f"No se aplicó: {lic['mensaje']}")
    destino, stage = inst / "versiones" / nueva, inst / "versiones" / f".tmp_{nueva}"
    if destino.exists():
        raise ActualizacionError(f"La carpeta de la versión {nueva} ya existe")
    shutil.rmtree(stage, ignore_errors=True)
    aux = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(paquete) as z:
            for n in man["archivos"]:
                z.extract(n, stage)
        for py in stage.rglob("*.py"):
            try:
                compile(py.read_text(encoding="utf-8"), str(py), "exec")
            except SyntaxError as e:
                raise ActualizacionError(f"Error de sintaxis en {py.relative_to(stage).as_posix()}: {e.msg}")
        copia = os.path.join(aux, "zapateria.db")                                  # ensayo completo sobre una COPIA de la base
        a, b = sqlite3.connect(db), sqlite3.connect(copia); a.backup(b); a.close(); b.close()
        try:
            c = connect(copia); migrate(c, stage / "database" / "migrations")
            malo = c.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or c.execute("PRAGMA foreign_key_check").fetchall()
            c.close()
        except sqlite3.Error as e:
            raise ActualizacionError(f"La migración de datos falló en el ensayo ({e}): no se cambió nada")
        if malo:
            raise ActualizacionError("La migración dejó la base inconsistente en el ensayo: no se cambió nada")
        prueba(stage, copia)
        respaldo = RS.respaldar(str(db), str(inst / "datos" / "respaldos" / "antes_de_actualizar"))["archivo"]
        try:
            c = connect(db); migrate(c, stage / "database" / "migrations"); c.close()
            os.replace(stage, destino)
            _puntero(inst, nueva)
        except Exception as e:
            if version_actual(inst) != nueva:
                shutil.rmtree(destino, ignore_errors=True)
            RS.restaurar(respaldo, str(db))
            raise ActualizacionError(f"Falló al aplicar; todo quedó como estaba: {e}") from e
    finally:
        shutil.rmtree(stage, ignore_errors=True); shutil.rmtree(aux, ignore_errors=True)
    _anotar(inst, {"tipo": "actualizacion", "desde": actual, "hasta": nueva, "respaldo": respaldo, "notas": man.get("notas", "")})
    vers = sorted((d for d in (inst / "versiones").iterdir() if re.fullmatch(r"\d+\.\d+\.\d+", d.name)), key=lambda d: _semver(d.name), reverse=True)
    for d in vers[conservar:]:
        shutil.rmtree(d, ignore_errors=True)
    return {"desde": actual, "hasta": nueva, "respaldo": respaldo, "segundos": round(time.monotonic() - t0, 1)}


def revertir(inst, perder_datos_posteriores=False) -> dict:
    """Vuelve a la versión anterior Y a la base de justo antes de actualizar. Lo vendido después se guarda en una copia aparte."""
    inst = Path(inst).resolve(); db = inst / "datos" / "zapateria.db"
    actual = version_actual(inst)
    reg = next((h for h in reversed(historial(inst)) if h["tipo"] == "actualizacion" and h["hasta"] == actual), None)
    if not reg:
        raise ActualizacionError("No hay ninguna actualización para revertir")
    if not perder_datos_posteriores:
        raise ActualizacionError("Revertir restaura la base de antes de la actualización: lo vendido después no estará (quedará una copia aparte). "
                                 "Confirme con perder_datos_posteriores=True")
    if not (inst / "versiones" / reg["desde"]).is_dir():
        raise ActualizacionError(f"La versión {reg['desde']} ya no está instalada")
    r = RS.restaurar(reg["respaldo"], str(db))
    _puntero(inst, reg["desde"])
    shutil.rmtree(inst / "versiones" / reg["hasta"], ignore_errors=True)       # la versión abandonada se puede volver a instalar corregida
    _anotar(inst, {"tipo": "reversion", "desde": reg["hasta"], "hasta": reg["desde"], "copia_datos_posteriores": r["seguridad"]})
    return {"version": reg["desde"], "copia_datos_posteriores": r["seguridad"]}


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        if a[:1] == ["generar-claves"] and len(a) == 2:
            print("Creadas:", *generar_claves(a[1]))
        elif a[:1] == ["empaquetar"] and len(a) >= 5:
            mn = a[a.index("--min") + 1] if "--min" in a else None
            print("Paquete:", empaquetar(a[1], a[2], a[3], a[4], mn))
        elif a[:1] == ["verificar"] and len(a) == 3:
            m = verificar_paquete(a[1], a[2]); print("Paquete válido. Versión", m["version"], "·", len(m["archivos"]), "archivos")
        elif a[:1] == ["aplicar"] and len(a) == 3:
            print("Actualizado:", aplicar(a[1], a[2], forzar="--forzar" in a))
        elif a[:1] == ["revertir"] and len(a) >= 2:
            print("Revertido:", revertir(a[1], "--confirmo-perder-datos" in a))
        elif a[:1] == ["version"] and len(a) == 2:
            print(version_actual(a[1]))
        else:
            print("Uso: generar-claves DIR | empaquetar ORIGEN ZIP VERSION CLAVE_PRIVADA [--min X.Y.Z] | verificar ZIP CLAVE_PUBLICA | "
                  "aplicar ZIP INSTALACION | revertir INSTALACION [--confirmo-perder-datos] | version INSTALACION"); sys.exit(2)
    except (ActualizacionError, RS.RespaldoError) as e:
        print("ERROR:", e); sys.exit(1)
