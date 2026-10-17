"""Datos del negocio y primer arranque. Todo lo que cambia de un cliente a otro vive en 'config' (nada en el código)."""
import re, uuid
from tools.db import transaccion
from core import auth, ticket
from core.auth import auditar, require_rol


class NegocioError(Exception):
    pass


TEXTOS = {"negocio_nombre": 120, "negocio_direccion": 200, "negocio_telefono": 30, "ticket_pie": 120}
CLAVES = ["negocio_nombre", "negocio_rif", "negocio_direccion", "negocio_telefono", "ticket_pie", "impresora_ticket",
          "ticket_codepage", "ticket_cajon", "ticket_cortar", "respaldo_carpeta", "respaldo_copia", "fiscal_activo", "fiscal_adaptador", "fiscal_modo_prueba", "nube_url", "nube_apikey", "nube_token"]


def normalizar_rif(txt) -> str:
    """Valida el FORMATO (letra + 8 dígitos + 1) y lo escribe J-12345678-9. No verifica el dígito final."""
    m = re.fullmatch(r"([JGVEPC])-?(\d{8})-?(\d)", re.sub(r"\s", "", str(txt or "")).upper())
    if not m:
        raise NegocioError("RIF inválido: use el formato J-12345678-9")
    return f"{m[1]}-{m[2]}-{m[3]}"


def _validar(clave, valor):
    v = " ".join(str(valor if valor is not None else "").split())
    if clave == "negocio_rif":
        return normalizar_rif(v) if v else ""
    if clave in TEXTOS:
        if len(v) > TEXTOS[clave]:
            raise NegocioError(f"{clave.replace('_', ' ')}: máximo {TEXTOS[clave]} caracteres")
        if clave == "negocio_nombre" and not v:
            raise NegocioError("El nombre del negocio es obligatorio")
        return v
    if clave == "ticket_codepage" and v not in ticket.CODEPAGES:
        raise NegocioError(f"Página de códigos inválida. Opciones: {', '.join(ticket.CODEPAGES)}")
    if clave in ("ticket_cajon", "ticket_cortar") and v not in ("0", "1"):
        raise NegocioError(f"{clave} debe ser 0 o 1")
    if clave == "impresora_ticket" and v and v != "ninguna":
        tipo, _, arg = v.partition(":")
        if tipo not in ("archivo", "red", "windows", "serial") or not arg:
            raise NegocioError("Impresora inválida. Use red:IP:9100, windows:NOMBRE, serial:COM3 o archivo:RUTA")
    if clave in ("fiscal_activo", "fiscal_modo_prueba") and v not in ("0", "1"):
        raise NegocioError(f"{clave} debe ser 0 o 1")
    if clave == "fiscal_adaptador":
        from core import fiscal
        if v not in ("ninguno", *fiscal.ADAPTADORES):
            raise NegocioError("Proveedor fiscal desconocido")
    if clave == "nube_url" and v and not re.fullmatch(r"https://[\w.\-]+(:\d+)?(/[\w.\-/]*)?", v):
        raise NegocioError("La dirección de la nube debe empezar con https://")
    if clave in ("nube_apikey", "nube_token") and len(v) > 600:
        raise NegocioError(f"{clave}: demasiado largo")
    if clave in ("respaldo_carpeta", "respaldo_copia") and len(v) > 260:
        raise NegocioError("La ruta es demasiado larga")
    return v


def _guardar(con, clave, valor):
    con.execute("INSERT INTO config(clave,valor) VALUES(?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (clave, valor))


def necesita_configuracion(con) -> bool:
    return con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0


def leer_datos(con) -> dict:
    d = {k: "" for k in CLAVES}
    d.update({k: v for k, v in con.execute("SELECT clave, valor FROM config") if k in d})
    r = con.execute("SELECT valor FROM config WHERE clave='instalacion_id'").fetchone()
    d["instalacion_id"] = r[0] if r else None
    for secreto in ("nube_token", "nube_apikey"):                       # los secretos no salen hacia la pantalla
        d[secreto] = "********" if d[secreto] else ""
    return d


def configurar_negocio(con, negocio, rif, direccion, telefono, dueno_nombre, dueno_pin, cajero_nombre=None, cajero_pin=None,
                       sucursal_nombre="Principal", pie="Gracias por su compra") -> dict:
    """Primer arranque: una sola vez, solo si no hay usuarios. Crea sucursal, dueño y (opcional) el primer cajero."""
    vals = {"negocio_nombre": _validar("negocio_nombre", negocio), "negocio_rif": _validar("negocio_rif", rif),
            "negocio_direccion": _validar("negocio_direccion", direccion), "negocio_telefono": _validar("negocio_telefono", telefono),
            "ticket_pie": _validar("ticket_pie", pie)}
    quiere_cajero = bool((cajero_nombre or "").strip())
    if quiere_cajero and cajero_pin == dueno_pin:
        raise NegocioError("El cajero debe tener un PIN distinto al del dueño")
    try:
        with transaccion(con):
            if not necesita_configuracion(con):
                raise NegocioError("El sistema ya está configurado")
            sid = con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1',?)", (" ".join((sucursal_nombre or "Principal").split()),)).lastrowid
            did = auth.crear_usuario(con, sid, dueno_nombre, "dueno", dueno_pin)
            if quiere_cajero:
                auth.crear_usuario(con, sid, cajero_nombre, "cajero", cajero_pin, actor={"id": did, "rol": "dueno"})
            for k, v in vals.items():
                _guardar(con, k, v)
            iid = str(uuid.uuid4())
            _guardar(con, "instalacion_id", iid)
            auditar(con, did, "negocio_configurado", vals["negocio_nombre"])
    except auth.AuthError as e:
        raise NegocioError(str(e)) from e
    return {"sucursal_id": sid, "dueno_id": did, "instalacion_id": iid}


def actualizar_datos(con, actor, datos: dict):
    require_rol(actor, "dueno")
    desconocidas = [k for k in datos if k not in CLAVES]
    if desconocidas:
        raise NegocioError(f"Dato no editable: {desconocidas[0]}")
    limpio = {k: _validar(k, v) for k, v in datos.items() if not (k in ("nube_token", "nube_apikey") and v == "********")}
    with transaccion(con):
        for k, v in limpio.items():
            _guardar(con, k, v)
        auditar(con, actor["id"], "negocio_actualizado", ",".join(sorted(limpio)))


def crear_cajero(con, actor, nombre, pin) -> int:
    require_rol(actor, "dueno")
    try:
        return auth.crear_usuario(con, actor["sucursal_id"], nombre, "cajero", pin, actor=actor)
    except auth.AuthError as e:
        raise NegocioError(str(e)) from e


def crear_sucursal(con, actor, codigo, nombre) -> int:
    require_rol(actor, "dueno")
    codigo, nombre = re.sub(r"\s", "", codigo or "").upper(), " ".join((nombre or "").split())
    if not re.fullmatch(r"[A-Z0-9]{2,6}", codigo) or not nombre:
        raise NegocioError("La sucursal necesita un código (2 a 6 letras o números) y un nombre")
    from core import licencia
    est = licencia.estado(con, licencia.clave_publica())
    n = con.execute("SELECT COUNT(*) FROM sucursales WHERE activa=1").fetchone()[0]
    if n >= est["sucursales_max"]:
        raise NegocioError(f"Su plan permite {est['sucursales_max']} sucursal(es). Amplíe su licencia para agregar más")
    with transaccion(con):
        try:
            sid = con.execute("INSERT INTO sucursales(codigo,nombre) VALUES(?,?)", (codigo, nombre)).lastrowid
        except Exception as e:
            raise NegocioError("Ya existe una sucursal con ese código") from e
        auditar(con, actor["id"], "sucursal_creada", codigo)
        return sid
