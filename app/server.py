"""Servidor local de la caja: API JSON + pantalla web en 127.0.0.1 (sin internet, sin dependencias).
Solo acepta conexiones de la propia PC, exige sesión por PIN y revisa el encabezado Host."""
import argparse, json, os, re, secrets, sys, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from tools.db import connect, migrate, transaccion
from tools import respaldo as RS, sync as SY
from core import auth, fiscal as F, licencia as LIC, negocio as N, inventario as I, pagos as PG, pos as POS, pricing as PR, productos as P, reportes as R, ticket as T

ESTATICO = Path(__file__).resolve().parent / "static"
ERRORES = (LIC.LicenciaError, F.FiscalError, RS.RespaldoError, N.NegocioError, POS.PosError, PG.PagosError, PR.PricingError, I.InventarioError, P.ProductoError, R.ReporteError, T.TicketError)
RUTAS = []


class Denegado(Exception):
    pass


class NoEncontrado(Exception):
    pass


class Sesiones:
    def __init__(self, ttl=8 * 3600, reloj=time.monotonic):
        self.ttl, self.reloj, self._d, self._l = ttl, reloj, {}, threading.Lock()

    def crear(self, usuario):
        t = secrets.token_urlsafe(24)
        with self._l:
            self._d[t] = (usuario, self.reloj())
        return t

    def obtener(self, t):
        with self._l:
            e = self._d.get(t or "")
            if not e:
                return None
            if self.reloj() - e[1] > self.ttl:
                del self._d[t]
                return None
            self._d[t] = (e[0], self.reloj())            # vence por inactividad
            return e[0]

    def cerrar(self, t):
        with self._l:
            self._d.pop(t or "", None)


def ruta(metodo, patron, login=True):
    def deco(f):
        RUTAS.append((metodo, re.compile(f"^{patron}$"), login, f))
        return f
    return deco


def _cents(x, cero=False):
    if cero and str(x).strip() in ("", "0", "0,00", "0.00"):
        return 0
    return PG.a_cents(x)


def _entero(x, nombre="cantidad"):
    try:
        return int(x)
    except (TypeError, ValueError):
        raise POS.PosError(f"El campo {nombre} debe ser un número entero")


def _actor(c):
    """Acción que pide permiso de dueño: el dueño autoriza con su nombre y PIN sin cerrar la sesión del cajero."""
    a = c.body.get("autoriza")
    if not a:
        return c.user
    try:
        return auth.login(c.con, c.user["sucursal_id"], a.get("nombre", ""), a.get("pin", ""))
    except auth.AuthError:
        raise Denegado("PIN de autorización incorrecto")


def _venta(c, vid):
    r = c.con.execute("SELECT sucursal_id FROM ventas WHERE id=?", (int(vid),)).fetchone()
    if not r or r[0] != c.user["sucursal_id"]:
        raise NoEncontrado("Venta inexistente")
    return int(vid)


def _vista(con, vid):
    r = POS.ver_venta(con, vid)
    r["pagos"], r["resumen_pagos"] = PG.listar_pagos(con, vid), PG.resumen_pagos(con, vid)
    return r


def _tasa(con):
    try:
        t = PR.tasa_vigente(con)
        return {**t, "edad_horas": round(POS._edad_horas(t["vigente_desde"]), 1)}
    except PR.PricingError as e:
        return {"error": str(e)}


@ruta("POST", "/api/login", login=False)
def _login(c):
    try:
        u = auth.login(c.con, _entero(c.body.get("sucursal_id", 1), "sucursal_id"), c.body.get("nombre", ""), c.body.get("pin", ""))
    except auth.AuthError as e:
        raise Denegado(str(e)) if "bloqueado" in str(e) else PermissionError(str(e))
    return {"token": c.ses.crear(u), "usuario": u}


@ruta("GET", "/api/instalacion", login=False)
def _inst(c):
    return {"configurado": not N.necesita_configuracion(c.con)}


@ruta("POST", "/api/instalacion", login=False)
def _inst_crear(c):
    b = c.body
    r = N.configurar_negocio(c.con, b.get("negocio"), b.get("rif"), b.get("direccion"), b.get("telefono"), b.get("dueno_nombre"),
                             b.get("dueno_pin"), b.get("cajero_nombre"), b.get("cajero_pin"))
    u = auth.login(c.con, r["sucursal_id"], b["dueno_nombre"], b["dueno_pin"])
    return {"token": c.ses.crear(u), "usuario": u}


@ruta("GET", "/api/negocio")
def _negocio(c):
    return N.leer_datos(c.con)


@ruta("POST", "/api/negocio")
def _negocio_guardar(c):
    N.actualizar_datos(c.con, c.user, c.body)
    return N.leer_datos(c.con)


@ruta("POST", "/api/usuarios")
def _usuario_nuevo(c):
    return {"id": N.crear_cajero(c.con, c.user, c.body.get("nombre"), c.body.get("pin"))}


@ruta("GET", "/api/respaldos")
def _resp(c):
    auth.require_rol(c.user, "dueno")
    carpeta, copia, _ = RS.configuracion(c.con, c.db)
    return {**RS.estado(carpeta), "carpeta": carpeta, "copia": copia, "lista": RS.listar(carpeta)[:10]}


@ruta("POST", "/api/respaldos")
def _resp_ahora(c):
    auth.require_rol(c.user, "dueno")
    carpeta, copia, _ = RS.configuracion(c.con, c.db)
    r = RS.respaldar(c.db, carpeta)
    RS.verificar_respaldo(r["archivo"])                  # un respaldo que no se puede leer no sirve: se comprueba al crearlo
    error = None
    if copia:
        try:
            import shutil; os.makedirs(copia, exist_ok=True); shutil.copy2(r["archivo"], copia)
        except OSError as e:
            error = f"No se pudo copiar al segundo destino: {e}"
    RS.rotar(carpeta)
    return {"archivo": r["archivo"], "filas": r["filas"], "copia_error": error}


@ruta("POST", r"/api/venta/(\d+)/cliente")
def _cliente(c, vid):
    vid = _venta(c, vid)
    F.asignar_cliente(c.con, vid, c.body.get("rif"), c.body.get("nombre"), c.body.get("domicilio"))
    return _vista(c.con, vid)


@ruta("GET", "/api/fiscal/estado")
def _fiscal_estado(c):
    auth.require_rol(c.user, "dueno")
    return F.estado(c.con)


@ruta("POST", "/api/fiscal/emitir")
def _fiscal_emitir(c):
    return F.emitir_pendientes(c.con, c.user)


@ruta("GET", "/api/fiscal/libro")
def _fiscal_libro(c):
    d = (c.q.get("desde") or [""])[0]
    return F.libro_ventas(c.con, c.user, d, (c.q.get("hasta") or [None])[0], c.user["sucursal_id"])


@ruta("GET", "/api/licencia")
def _licencia(c):
    auth.require_rol(c.user, "dueno")
    return LIC.estado(c.con, LIC.clave_publica(c.db))


@ruta("POST", "/api/licencia")
def _licencia_instalar(c):
    auth.require_rol(c.user, "dueno")
    return LIC.instalar(c.con, c.body.get("texto"), LIC.clave_publica(c.db))


@ruta("GET", "/api/nube")
def _nube(c):
    auth.require_rol(c.user, "dueno")
    return {**SY.estado(c.con), "configurada": SY.transporte_desde_config(c.con) is not None,
            "permitida": LIC.permite(c.con, "nube", LIC.clave_publica(c.db))}


@ruta("POST", "/api/logout")
def _logout(c):
    c.ses.cerrar(c.token)
    return {"ok": True}


@ruta("GET", "/api/estado")
def _estado(c):
    resp = RS.estado(RS.configuracion(c.con, c.db)[0]) if c.user["rol"] == "dueno" else None
    lic = None
    if c.user["rol"] == "dueno":
        e = LIC.estado(c.con, LIC.clave_publica(c.db))
        lic = {"estado": e["estado"], "mensaje": e["mensaje"], "alerta": e["alerta"]}
    return {"licencia": lic, "respaldo": resp, "usuario": c.user, "tasa": _tasa(c.con), "ventas_abiertas": POS.ventas_abiertas(c.con, c.user["sucursal_id"]),
            "metodos": {m: {"moneda": v[0], "referencia": v[1], "digital": v[2], "efectivo": v[3]} for m, v in PG.METODOS.items()},
            "impresora": bool(T._cfg(c.con, "impresora_ticket"))}


@ruta("POST", "/api/tasa")
def _tasa_nueva(c):
    auth.require_rol(c.user, "dueno")
    PR.registrar_tasa(c.con, c.body.get("tipo", "bcv"), c.body.get("valor"), c.user["id"])
    return _tasa(c.con)


@ruta("GET", "/api/buscar")
def _buscar(c):
    return P.buscar(c.con, (c.q.get("q") or [""])[0])


@ruta("POST", "/api/venta")
def _abrir(c):
    r = POS.abrir_venta(c.con, c.user)
    return {**r, "venta": _vista(c.con, r["venta_id"])}


@ruta("GET", r"/api/venta/(\d+)")
def _ver(c, vid):
    return _vista(c.con, _venta(c, vid))


@ruta("POST", r"/api/venta/(\d+)/escanear")
def _escanear(c, vid):
    vid = _venta(c, vid)
    r = POS.escanear(c.con, vid, c.body.get("codigo", ""), _entero(c.body.get("cantidad", 1)))
    return {"advertencias": r["advertencias"], "venta": _vista(c.con, vid)}


@ruta("POST", r"/api/venta/(\d+)/item/(\d+)")
def _item(c, vid, item):
    vid = _venta(c, vid)
    POS.cambiar_cantidad(c.con, vid, int(item), _entero(c.body.get("cantidad")))
    return _vista(c.con, vid)


@ruta("POST", r"/api/venta/(\d+)/pago")
def _pago(c, vid):
    vid = _venta(c, vid)
    r = PG.registrar_pago(c.con, c.user, vid, c.body.get("metodo"), _cents(c.body.get("monto")), c.body.get("referencia"), c.body.get("banco"))
    return {**r, "venta": _vista(c.con, vid)}


@ruta("POST", r"/api/venta/(\d+)/vuelto")
def _vuelto(c, vid):
    vid = _venta(c, vid)
    PG.registrar_vuelto(c.con, c.user, vid, c.body.get("metodo"), _cents(c.body.get("monto")), c.body.get("referencia"))
    return _vista(c.con, vid)


def _de_pago(c, pid):
    r = c.con.execute("SELECT v.sucursal_id FROM pagos p JOIN ventas v ON v.id=p.venta_id WHERE p.id=?", (int(pid),)).fetchone()
    if not r or r[0] != c.user["sucursal_id"]:
        raise NoEncontrado("Pago inexistente")
    return int(pid)


@ruta("POST", r"/api/pago/(\d+)/confirmar")
def _confirmar(c, pid):
    PG.confirmar_pago(c.con, c.user, _de_pago(c, pid))
    return {"ok": True}


@ruta("POST", r"/api/pago/(\d+)/anular")
def _anular_pago(c, pid):
    PG.anular_pago(c.con, _actor(c), _de_pago(c, pid), c.body.get("motivo"))
    return {"ok": True}


@ruta("POST", r"/api/venta/(\d+)/cerrar")
def _cerrar(c, vid):
    vid = _venta(c, vid)
    POS.cerrar_venta(c.con, vid, c.user["id"])          # ya quedó guardada: un fallo de impresora no la afecta
    ticket, error = None, None
    try:
        ticket = T.imprimir_ticket(c.con, vid)
    except T.TicketError as e:
        error = str(e)
    return {"venta": _vista(c.con, vid), "ticket": ticket, "ticket_error": error}


@ruta("POST", r"/api/venta/(\d+)/ticket")
def _reimprimir(c, vid):
    return T.imprimir_ticket(c.con, _venta(c, vid), reimpresion=True)


@ruta("POST", r"/api/venta/(\d+)/anular")
def _anular(c, vid):
    POS.anular_venta_abierta(c.con, c.user, _venta(c, vid), c.body.get("motivo"))
    return {"ok": True}


@ruta("POST", "/api/devolucion")
def _devolucion(c):
    actor = _actor(c)
    vid = _venta(c, c.body.get("venta_id", 0))
    items = [(i[0], _entero(i[1])) for i in c.body.get("items", [])]
    with transaccion(c.con):                                 # devolución + reembolso: todo o nada
        d = POS.devolver(c.con, actor, vid, items, c.body.get("motivo"))
        rb = c.body.get("reembolso")
        if rb:
            PG.registrar_reembolso(c.con, actor, d["devolucion_id"], rb.get("metodo"), _cents(rb.get("monto")), rb.get("referencia"))
    return {**d, "pendiente_reembolso_usd_cents": PG.pendiente_reembolso(c.con, d["devolucion_id"])}


@ruta("POST", "/api/cajon")
def _cajon(c):
    auth.require_rol(_actor(c), "dueno")
    T.abrir_cajon(c.con)
    return {"ok": True}


@ruta("POST", "/api/caja/cerrar")
def _caja(c):
    b = c.body
    fondo = lambda k: None if b.get(k) in (None, "") else _cents(b[k], cero=True)
    return R.cerrar_caja(c.con, c.user, c.user["sucursal_id"], _cents(b.get("contado_usd", 0), True), _cents(b.get("contado_bs", 0), True),
                         _cents(b.get("retirado_usd", 0), True), _cents(b.get("retirado_bs", 0), True),
                         fondo("fondo_usd"), fondo("fondo_bs"), b.get("notas"))


class Handler(BaseHTTPRequestHandler):
    db_path, puerto, ses = None, None, None
    server_version = "ZapaPOS"

    def log_message(self, *a):
        pass

    def _enviar(self, estado, obj, tipo="application/json; charset=utf-8"):
        cuerpo = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(estado)
        for k, v in (("Content-Type", tipo), ("Content-Length", str(len(cuerpo))), ("Cache-Control", "no-store"),
                     ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def _despachar(self, metodo):
        if self.headers.get("Host", "") not in (f"127.0.0.1:{self.puerto}", f"localhost:{self.puerto}"):
            return self._enviar(403, {"error": "Acceso no permitido"})
        url = urlparse(self.path)
        if metodo == "GET" and url.path in ("/", "/index.html"):
            f = ESTATICO / "index.html"
            return self._enviar(200, f.read_bytes() if f.exists() else b"<h1>ZapaPOS</h1>", "text/html; charset=utf-8")
        if metodo == "GET" and url.path in ("/dueno", "/dueno.html"):
            f = ESTATICO.parent / "dueno.html"
            return self._enviar(200, f.read_bytes() if f.exists() else b"<h1>Panel del dueno</h1>", "text/html; charset=utf-8")
        for m, rx, login, fn in RUTAS:
            g = rx.match(url.path)
            if m == metodo and g:
                break
        else:
            return self._enviar(404, {"error": "No existe"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self._enviar(413, {"error": "Solicitud demasiado grande"})
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._enviar(400, {"error": "Solicitud inválida"})
        token = self.headers.get("X-Token")
        user = self.ses.obtener(token)
        if login and not user:
            return self._enviar(401, {"error": "Sesión vencida: ingrese su PIN"})
        con = connect(self.db_path)
        try:
            c = SimpleNamespace(db=self.db_path, con=con, user=user, body=body, q=parse_qs(url.query), ses=self.ses, token=token)
            self._enviar(200, fn(c, *g.groups()))
        except PermissionError as e:
            self._enviar(401, {"error": str(e)})
        except (Denegado, auth.AuthError) as e:
            self._enviar(403, {"error": str(e)})
        except NoEncontrado as e:
            self._enviar(404, {"error": str(e)})
        except ERRORES as e:
            self._enviar(400, {"error": str(e)})
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._enviar(500, {"error": "Error interno: el sistema registró el detalle"})
        finally:
            con.close()

    def do_GET(self):
        self._despachar("GET")

    def do_POST(self):
        self._despachar("POST")


def crear_servidor(db_path, puerto=8787, ttl=8 * 3600, respaldos=False, sync=False):
    con = connect(db_path); migrate(con); con.close()
    if respaldos:
        RS.iniciar_respaldos(db_path)
    if sync:
        SY.iniciar_sync(db_path)
    LIC.CLAVE_PUBLICA = str(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(db_path))), "clave_publica.pem"))
    H = type("H", (Handler,), {"db_path": db_path, "ses": Sesiones(ttl), "puerto": None})
    srv = ThreadingHTTPServer(("127.0.0.1", puerto), H)
    H.puerto = srv.server_port
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Caja ZapaPOS (servidor local)")
    ap.add_argument("--db", default="zapateria.db")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    s = crear_servidor(a.db, a.port, respaldos=True, sync=True)
    print(f"Caja lista en http://127.0.0.1:{s.server_port}  (Ctrl+C para salir)", flush=True)
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
