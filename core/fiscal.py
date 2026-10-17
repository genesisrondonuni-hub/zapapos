"""Fiscal (base, independiente del proveedor). La venta NUNCA espera a la imprenta digital: al cerrarse queda una factura
'pendiente' en la cola y se emite cuando haya conexión. Los números de factura y de control los entrega el proveedor.
Cada proveedor es un adaptador; sin adaptador real no hay emisión. Los documentos del simulador (solo pruebas) no entran al libro."""
import json, re, sqlite3, uuid as _uuid
from datetime import datetime, timezone
from tools.db import transaccion
from core import reportes
from core.auth import auditar, require_rol
from core.pricing import usd_a_bs


class FiscalError(Exception):
    pass


class Adaptador:
    nombre, simulado = "base", False

    def emitir(self, doc: dict, factura_afectada: dict | None = None) -> dict:
        """Debe devolver {'numero': str, 'numero_control': str|None, 'fecha_emision': datetime con zona, 'respuesta': dict}."""
        raise NotImplementedError


class Simulador(Adaptador):
    nombre, simulado = "simulador", True

    def emitir(self, doc, factura_afectada=None):
        return {"numero": f"SIM-{doc['tipo'][0].upper()}-{doc['id']:08d}", "numero_control": f"SIM-{doc['id']:08d}",
                "fecha_emision": datetime.now(timezone.utc), "respuesta": {"simulado": True}}


ADAPTADORES = {"simulador": Simulador}          # aquí se registra cada imprenta digital real cuando tenga su adaptador


def _cfg(con, k, d=""):
    r = con.execute("SELECT valor FROM config WHERE clave=?", (k,)).fetchone()
    return r[0] if r and r[0] else d


def activo(con):
    return _cfg(con, "fiscal_activo", "0") == "1"


def obtener_adaptador(con) -> Adaptador:
    nombre = _cfg(con, "fiscal_adaptador", "ninguno")
    if nombre in ("", "ninguno"):
        raise FiscalError("No hay proveedor fiscal configurado")
    if nombre not in ADAPTADORES:
        raise FiscalError(f"Proveedor fiscal desconocido: {nombre}")
    if ADAPTADORES[nombre].simulado and _cfg(con, "fiscal_modo_prueba", "0") != "1":
        raise FiscalError("El simulador solo se permite en modo de prueba")
    return ADAPTADORES[nombre]()


def normalizar_documento_cliente(txt) -> str:
    s = re.sub(r"\s", "", str(txt or "")).upper()
    m = re.fullmatch(r"([JGVEPC])-?(\d{8})-?(\d)", s)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]}"
    m = re.fullmatch(r"([VE])-?(\d{5,9})", s)
    if m:
        return f"{m[1]}-{m[2]}"
    raise FiscalError("Documento inválido: use RIF (J-12345678-9) o cédula (V-12345678)")


def asignar_cliente(con, venta_id, rif, nombre, domicilio=None):
    """Datos del adquirente para la factura. Sin datos = consumidor final. Solo mientras la venta está abierta."""
    nombre = " ".join(str(nombre or "").split())
    if (rif or nombre) and not (rif and nombre):
        raise FiscalError("Indique el documento y el nombre del cliente (o deje ambos vacíos)")
    if len(nombre) > 120:
        raise FiscalError("El nombre del cliente es demasiado largo")
    doc = normalizar_documento_cliente(rif) if rif else None
    with transaccion(con):
        est = con.execute("SELECT estado FROM ventas WHERE id=?", (venta_id,)).fetchone()
        if not est or est[0] != "abierta":
            raise FiscalError("Solo se puede asignar cliente a una venta abierta")
        con.execute("UPDATE ventas SET cliente_rif=?, cliente_nombre=?, cliente_domicilio=? WHERE id=?",
                    (doc, nombre or None, " ".join(str(domicilio or "").split())[:200] or None, venta_id))


def _bs(usd_total, iva_usd, total_bs, tasa):
    iva_bs = min(usd_a_bs(iva_usd, tasa), total_bs)
    return total_bs - iva_bs, iva_bs, total_bs                  # base + IVA = total, siempre


def encolar_factura(con, venta_id):
    """Se llama dentro del cierre de la venta (misma transacción). No hace nada si el módulo fiscal está apagado."""
    if not activo(con):
        return None
    v = con.execute("SELECT subtotal_usd_cents,iva_usd_cents,total_usd_cents,total_bs_centimos,iva_bp,tasa_micro,cliente_rif,cliente_nombre,"
                    "cliente_domicilio FROM ventas WHERE id=?", (venta_id,)).fetchone()
    b, i, t = _bs(v[2], v[1], v[3], v[5])
    return con.execute("INSERT INTO documentos_fiscales(uuid,tipo,venta_id,receptor_rif,receptor_nombre,receptor_domicilio,base_bs_centimos,iva_bs_centimos,"
                       "total_bs_centimos,base_usd_cents,iva_usd_cents,total_usd_cents,iva_bp,tasa_micro) VALUES(?,'factura',?,?,?,?,?,?,?,?,?,?,?,?)",
                       (str(_uuid.uuid4()), venta_id, v[6], v[7], v[8], b, i, t, v[0], v[1], v[2], v[4], v[5])).lastrowid


def encolar_nota_credito(con, devolucion_id):
    """La devolución genera una nota de crédito que referencia la factura de esa venta. Sin factura fiscal previa no hay nota."""
    if not activo(con):
        return None
    d = con.execute("SELECT venta_id,subtotal_usd_cents,iva_usd_cents,total_usd_cents,total_bs_centimos,tasa_micro FROM devoluciones WHERE id=?",
                    (devolucion_id,)).fetchone()
    f = con.execute("SELECT receptor_rif,receptor_nombre,receptor_domicilio,iva_bp FROM documentos_fiscales WHERE venta_id=? AND tipo='factura'", (d[0],)).fetchone()
    if not f:
        auditar(con, None, "devolucion_sin_factura_fiscal", f"devolucion {devolucion_id}")
        return None
    b, i, t = _bs(d[3], d[2], d[4], d[5])
    return con.execute("INSERT INTO documentos_fiscales(uuid,tipo,venta_id,devolucion_id,receptor_rif,receptor_nombre,receptor_domicilio,base_bs_centimos,"
                       "iva_bs_centimos,total_bs_centimos,base_usd_cents,iva_usd_cents,total_usd_cents,iva_bp,tasa_micro) VALUES(?,'nota_credito',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (str(_uuid.uuid4()), d[0], devolucion_id, f[0], f[1], f[2], b, i, t, d[1], d[2], d[3], f[3], d[5])).lastrowid


def _doc(con, did):
    cur = con.execute("SELECT * FROM documentos_fiscales WHERE id=?", (did,))
    return dict(zip([c[0] for c in cur.description], cur.fetchone()))


def emitir_pendientes(con, actor=None, adaptador=None, limite=50) -> dict:
    """Envía al proveedor lo pendiente (facturas primero). Un fallo no detiene el resto ni toca la venta; se reintenta después."""
    if actor:
        require_rol(actor, "dueno")
    ad = adaptador or obtener_adaptador(con)
    ids = [r[0] for r in con.execute("SELECT id FROM documentos_fiscales WHERE estado IN ('pendiente','error') "
                                     "ORDER BY CASE tipo WHEN 'factura' THEN 0 ELSE 1 END, id LIMIT ?", (limite,))]
    res = {"emitidos": 0, "errores": 0, "omitidos": 0}
    for did in ids:
        doc, afectada = _doc(con, did), None
        if doc["tipo"] == "nota_credito":
            r = con.execute("SELECT id FROM documentos_fiscales WHERE venta_id=? AND tipo='factura' AND estado='emitido'", (doc["venta_id"],)).fetchone()
            if not r:
                res["omitidos"] += 1; continue                    # la nota espera a que su factura esté emitida
            afectada = _doc(con, r[0])
        try:
            r = ad.emitir(doc, afectada)
            if not r.get("numero") or not isinstance(r.get("fecha_emision"), datetime):
                raise FiscalError("El proveedor no devolvió número y fecha de emisión")
        except Exception as e:
            with transaccion(con):
                con.execute("UPDATE documentos_fiscales SET estado='error', error=?, intentos=intentos+1 WHERE id=?", (str(e)[:300], did))
            res["errores"] += 1; continue
        try:
            with transaccion(con):
                con.execute("UPDATE documentos_fiscales SET estado='emitido', simulado=?, numero=?, numero_control=?, fecha_emision=?, respuesta=?, error=NULL, "
                            "intentos=intentos+1, emitido_en=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                            (1 if ad.simulado else 0, str(r["numero"]), r.get("numero_control"), reportes._iso(r["fecha_emision"]),
                             json.dumps(r.get("respuesta") or {}, ensure_ascii=False), did))
            res["emitidos"] += 1
        except sqlite3.IntegrityError:                    # el proveedor repitió un número ya usado: no se acepta, se revisa
            with transaccion(con):
                con.execute("UPDATE documentos_fiscales SET estado='error', error=?, intentos=intentos+1 WHERE id=?",
                            (f"El proveedor devolvió un número ya usado: {r['numero']}", did))
            res["errores"] += 1
    return res


def estado(con) -> dict:
    g = lambda e: con.execute("SELECT COUNT(*) FROM documentos_fiscales WHERE estado=?", (e,)).fetchone()[0]
    errs = [dict(zip(("id", "tipo", "error"), r)) for r in con.execute(
        "SELECT id,tipo,error FROM documentos_fiscales WHERE estado='error' ORDER BY id DESC LIMIT 3")]
    return {"activo": activo(con), "adaptador": _cfg(con, "fiscal_adaptador", "ninguno"), "modo_prueba": _cfg(con, "fiscal_modo_prueba", "0") == "1",
            "pendientes": g("pendiente"), "errores": g("error"), "emitidos": g("emitido"), "ultimos_errores": errs}


def libro_ventas(con, actor, desde, hasta=None, sucursal_id=None, incluir_simulados=False) -> dict:
    """Documentos EMITIDOS en el período (fecha local). Las notas de crédito restan. Verifique las columnas con su contador."""
    require_rol(actor, "dueno")
    ini, fin = reportes.rango_local(con, desde, hasta)
    off = reportes._offset_min(con)
    filtro = "" if sucursal_id is None else " AND v.sucursal_id=?"
    cur = con.execute(
        "SELECT d.fecha_emision,d.tipo,d.numero,d.numero_control,d.receptor_rif,d.receptor_nombre,d.base_bs_centimos,d.iva_bs_centimos,d.total_bs_centimos,"
        "d.iva_bp,f.numero FROM documentos_fiscales d JOIN ventas v ON v.id=d.venta_id "
        "LEFT JOIN documentos_fiscales f ON f.venta_id=d.venta_id AND f.tipo='factura' AND d.tipo='nota_credito' "
        "WHERE d.estado='emitido' AND d.fecha_emision>=? AND d.fecha_emision<?" + filtro + ("" if incluir_simulados else " AND d.simulado=0") +
        " ORDER BY d.fecha_emision, d.id", [ini, fin] + ([sucursal_id] if sucursal_id is not None else []))
    filas, tot = [], {"base_bs_centimos": 0, "iva_bs_centimos": 0, "total_bs_centimos": 0}
    for fe, tipo, num, ctl, rif, nom, b, i, t, bp, afect in cur:
        s = -1 if tipo == "nota_credito" else 1
        local = datetime.strptime(fe[:19], "%Y-%m-%dT%H:%M:%S") + __import__("datetime").timedelta(minutes=off)
        filas.append({"fecha": local.strftime("%d/%m/%Y"), "tipo": "Nota de Crédito" if s < 0 else "Factura", "numero": num, "numero_control": ctl or "",
                      "rif": rif or "", "cliente": nom or "Consumidor final", "base_bs_centimos": s * b, "iva_bs_centimos": s * i,
                      "total_bs_centimos": s * t, "alicuota": f"{bp / 100:g}%", "documento_afectado": afect or ""})
        tot["base_bs_centimos"] += s * b; tot["iva_bs_centimos"] += s * i; tot["total_bs_centimos"] += s * t
    sv = " AND v.sucursal_id=?" if sucursal_id is not None else ""
    sin = con.execute("SELECT COUNT(*) FROM ventas v WHERE v.estado='cerrada' AND v.cerrada_en>=? AND v.cerrada_en<?" + sv +
                      " AND NOT EXISTS (SELECT 1 FROM documentos_fiscales d WHERE d.venta_id=v.id AND d.tipo='factura' AND d.estado='emitido'"
                      + ("" if incluir_simulados else " AND d.simulado=0") + ")", [ini, fin] + ([sucursal_id] if sucursal_id is not None else [])).fetchone()[0]
    adv = [f"{sin} venta(s) cerrada(s) del período sin factura emitida: no aparecen en el libro"] if sin and activo(con) else []
    return {"filas": filas, "totales": tot, "advertencias": adv}
