"""Autenticación local por PIN (offline). Roles: dueno, cajero."""
import hashlib, hmac, os, re, sqlite3
from datetime import datetime, timedelta, timezone
from tools.db import transaccion

ITER = 120_000
MAX_INTENTOS = 5
BLOQUEO_MIN = 5
FMT = "%Y-%m-%dT%H:%M:%SZ"
ROLES = ("dueno", "cajero")
_DEBILES = {"1234", "4321", "123456", "654321", "12345"}


class AuthError(Exception):
    pass


def _ahora():
    return datetime.now(timezone.utc)


def _validar_pin(pin: str):
    if not isinstance(pin, str) or not re.fullmatch(r"\d{4,6}", pin):
        raise AuthError("El PIN debe tener de 4 a 6 números")
    if len(set(pin)) == 1 or pin in _DEBILES:
        raise AuthError("PIN demasiado fácil de adivinar, elija otro")


def hash_pin(pin: str) -> str:
    _validar_pin(pin)
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, ITER)
    return f"pbkdf2${ITER}${salt.hex()}${dk.hex()}"


def verify_pin(pin: str, almacenado: str) -> bool:
    try:
        _, it, salt, h = almacenado.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except (ValueError, AttributeError):
        return False


def auditar(con, usuario_id, accion, detalle=None):
    con.execute("INSERT INTO auditoria(usuario_id,accion,detalle) VALUES(?,?,?)", (usuario_id, accion, detalle))


def require_rol(usuario: dict, *roles):
    if usuario.get("rol") not in roles:
        raise AuthError("No tiene permiso para esta acción")


def crear_usuario(con, sucursal_id, nombre, rol, pin, actor=None) -> int:
    nombre = (nombre or "").strip()
    if not nombre:
        raise AuthError("El nombre es obligatorio")
    if rol not in ROLES:
        raise AuthError("Rol inválido")
    h = hash_pin(pin)
    with transaccion(con):
        hay = con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        if hay == 0:
            if rol != "dueno":
                raise AuthError("El primer usuario debe ser el dueño")
        else:
            if actor is None:
                raise AuthError("Solo el dueño puede crear usuarios")
            require_rol(actor, "dueno")
        try:
            cur = con.execute(
                "INSERT INTO usuarios(sucursal_id,nombre,rol,pin_hash) VALUES(?,?,?,?)",
                (sucursal_id, nombre, rol, h))
        except sqlite3.IntegrityError as e:
            raise AuthError("Ya existe un usuario con ese nombre en la sucursal") from e
        auditar(con, actor["id"] if actor else cur.lastrowid, "usuario_creado", f"{nombre}/{rol}")
        return cur.lastrowid


def login(con, sucursal_id, nombre, pin, ahora=None) -> dict:
    ahora = ahora or _ahora()
    with transaccion(con):
        r = con.execute(
            "SELECT id,nombre,rol,pin_hash,activo,intentos_fallidos,bloqueado_hasta,sucursal_id "
            "FROM usuarios WHERE sucursal_id=? AND nombre=? COLLATE NOCASE",
            (sucursal_id, (nombre or "").strip())).fetchone()
        if not r or not r[4]:
            raise AuthError("Usuario o PIN incorrecto")
        uid, nom, rol, h, _, intentos, bloq, suc = r
        if bloq and bloq > ahora.strftime(FMT):
            raise AuthError(f"Usuario bloqueado temporalmente hasta {bloq}")
        if verify_pin(pin or "", h):
            con.execute("UPDATE usuarios SET intentos_fallidos=0, bloqueado_hasta=NULL WHERE id=?", (uid,))
            auditar(con, uid, "login_ok")
            return {"id": uid, "nombre": nom, "rol": rol, "sucursal_id": suc}
        intentos += 1
        if intentos >= MAX_INTENTOS:
            hasta = (ahora + timedelta(minutes=BLOQUEO_MIN)).strftime(FMT)
            con.execute("UPDATE usuarios SET intentos_fallidos=0, bloqueado_hasta=? WHERE id=?", (hasta, uid))
            auditar(con, uid, "bloqueo", hasta)
        else:
            con.execute("UPDATE usuarios SET intentos_fallidos=? WHERE id=?", (intentos, uid))
            auditar(con, uid, "login_fallido", str(intentos))
    # el fallo se confirma (commit) antes de avisar al llamador
    raise AuthError("Usuario o PIN incorrecto")


def cambiar_pin(con, actor, usuario_id, nuevo_pin, pin_actual=None):
    h = hash_pin(nuevo_pin)
    with transaccion(con):
        if actor["rol"] != "dueno":
            if actor["id"] != usuario_id:
                raise AuthError("No tiene permiso para esta acción")
            fila = con.execute("SELECT pin_hash FROM usuarios WHERE id=?", (usuario_id,)).fetchone()
            if not fila or not verify_pin(pin_actual or "", fila[0]):
                raise AuthError("El PIN actual no coincide")
        con.execute("UPDATE usuarios SET pin_hash=?, intentos_fallidos=0, bloqueado_hasta=NULL WHERE id=?",
                    (h, usuario_id))
        auditar(con, actor["id"], "pin_cambiado", str(usuario_id))
