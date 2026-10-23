"""Punto de venta: carrito persistente (sobrevive a cortes de luz), escaneo tolerante, cierre atómico y devoluciones.
La venta guarda snapshot de tasa e IVA al abrirse. Cerrar = estado + descuento de stock + outbox en UNA transacción."""
import json, re, uuid as _uuid
from datetime import datetime, timezone
from tools.db import transaccion
from core import inventario, pagos as PG, pricing as PR, productos as P
from core.auth import auditar, require_rol

TASA_VIEJA_HORAS = 24


def _iso(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class PosError(Exception):
    pass


def _filas(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur]


def _cfg(con, clave):
    r = con.execute("SELECT valor FROM config WHERE clave=?", (clave,)).fetchone()
    return r[0] if r else None


def _venta(con, venta_id) -> dict:
    f = _filas(con.execute("SELECT * FROM ventas WHERE id=?", (venta_id,)))
    if not f:
        raise PosError("Venta inexistente")
    return f[0]


def _exigir_abierta(con, venta_id) -> dict:
    v = _venta(con, venta_id)
    if v["estado"] != "abierta":
        raise PosError(f"La venta está {v['estado']}: ya no se puede modificar")
    return v


def _edad_horas(vigente_desde, ahora=None):
    t = datetime.strptime(vigente_desde[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return ((ahora or datetime.now(timezone.utc)) - t).total_seconds() / 3600


def _recalcular(con, venta_id) -> dict:
    v = _venta(con, venta_id)
    items = con.execute("SELECT precio_unit_usd_cents, cantidad FROM venta_items WHERE venta_id=?", (venta_id,)).fetchall()
    t = PR.calcular_totales(items, v["iva_bp"], v["iva_modo"], v["tasa_micro"])
    con.execute("UPDATE ventas SET subtotal_usd_cents=?, iva_usd_cents=?, total_usd_cents=?, total_bs_centimos=? WHERE id=?",
                (t["subtotal_usd_cents"], t["iva_usd_cents"], t["total_usd_cents"], t["total_bs_centimos"], venta_id))
    return t


# ---------------- apertura y recuperación ----------------
def abrir_venta(con, usuario, ahora=None) -> dict:
    iva_bp, modo = _cfg(con, "iva_bp"), _cfg(con, "iva_modo_default")
    if modo not in ("sumado", "incluido") or not (iva_bp or "").isdigit():
        raise PosError("Falta configurar el IVA (modo y alícuota) antes de vender")
    with transaccion(con):
        tasa = PR.tasa_vigente(con)          # BCV por defecto; sin tasa registrada no se puede cobrar en Bs
        u = str(_uuid.uuid4())
        vid = con.execute("INSERT INTO ventas(uuid,sucursal_id,usuario_id,tasa_id,tasa_micro,iva_modo,iva_bp) VALUES(?,?,?,?,?,?,?)",
                          (u, usuario["sucursal_id"], usuario["id"], tasa["id"], tasa["tasa_micro"], modo, int(iva_bp))).lastrowid
        auditar(con, usuario["id"], "venta_abierta", str(vid))
    adv = []
    edad = _edad_horas(tasa["vigente_desde"], ahora)
    if edad > TASA_VIEJA_HORAS:
        adv.append(f"La tasa {tasa['tipo'].upper()} tiene {int(edad)} horas registrada: verifique que esté al día")
    return {"venta_id": vid, "uuid": u, "tasa_micro": tasa["tasa_micro"], "advertencias": adv}


def ventas_abiertas(con, sucursal_id):
    """Para retomar carritos tras un corte de luz."""
    return _filas(con.execute(
        "SELECT v.id, v.uuid, v.usuario_id, v.total_usd_cents, v.creado_en, "
        "(SELECT COUNT(*) FROM venta_items i WHERE i.venta_id=v.id) AS lineas "
        "FROM ventas v WHERE v.sucursal_id=? AND v.estado='abierta' ORDER BY v.id", (sucursal_id,)))


def actualizar_tasa_venta(con, venta_id, tipo=None) -> dict:
    """Acción explícita del cajero: re-aplica la tasa vigente a una venta abierta (si no tiene pagos)."""
    with transaccion(con):
        _exigir_abierta(con, venta_id)
        if con.execute("SELECT 1 FROM pagos WHERE venta_id=? AND anulado=0", (venta_id,)).fetchone():
            raise PosError("La venta ya tiene pagos registrados: no se puede cambiar la tasa")
        t = PR.tasa_vigente(con, tipo)
        con.execute("UPDATE ventas SET tasa_id=?, tasa_micro=? WHERE id=?", (t["id"], t["tasa_micro"], venta_id))
        tot = _recalcular(con, venta_id)
    return tot


# ---------------- carrito ----------------
def _buscar_variante(con, texto):
    codigo = re.sub(r"\s", "", texto or "").upper()
    if not codigo:
        raise PosError("Escanee o escriba un código")
    v = P.buscar_por_sku(con, codigo)
    if v:
        return v
    plano = re.sub(r"[^A-Z0-9]", "", codigo)    # lectores con teclado distinto pierden los guiones
    filas = con.execute("SELECT v.sku FROM variantes v JOIN productos p ON p.id=v.producto_id "
                        "WHERE v.activo=1 AND p.activo=1 AND REPLACE(v.sku,'-','')=?", (plano,)).fetchall()
    if len(filas) > 1:
        raise PosError("Código ambiguo: hay más de un producto que coincide")
    if filas:
        return P.buscar_por_sku(con, filas[0][0])
    raise PosError(f"Código no encontrado: {codigo}")


def escanear(con, venta_id, texto, cantidad=1) -> dict:
    v = _buscar_variante(con, texto)
    return agregar_variante(con, venta_id, v, cantidad)


def agregar_variante(con, venta_id, variante: dict, cantidad=1) -> dict:
    if not isinstance(cantidad, int) or isinstance(cantidad, bool) or cantidad <= 0:
        raise PosError("La cantidad debe ser un entero positivo")
    if variante["precio_usd_cents"] <= 0:
        raise PosError(f"{variante['sku']} no tiene precio: asígnelo antes de vender")
    with transaccion(con):
        venta = _exigir_abierta(con, venta_id)
        linea = con.execute("SELECT id, cantidad FROM venta_items WHERE venta_id=? AND variante_id=?",
                            (venta_id, variante["variante_id"])).fetchone()
        if linea:        # misma variante: se suma a la línea y conserva el precio con que se agregó
            item_id, total_qty = linea[0], linea[1] + cantidad
            con.execute("UPDATE venta_items SET cantidad=? WHERE id=?", (total_qty, item_id))
        else:
            total_qty = cantidad
            item_id = con.execute(     # el costo se congela con la línea: el margen reportado no cambia si luego sube el costo
                "INSERT INTO venta_items(venta_id,variante_id,cantidad,precio_unit_usd_cents,costo_unit_usd_cents) "
                "VALUES(?,?,?,?,(SELECT costo_usd_cents FROM variantes WHERE id=?))",
                (venta_id, variante["variante_id"], cantidad, variante["precio_usd_cents"], variante["variante_id"])).lastrowid
        tot = _recalcular(con, venta_id)
        hay = inventario.stock(con, venta["sucursal_id"], variante["variante_id"])
    adv = [f"Stock del sistema insuficiente para {variante['sku']} (hay {hay}): la venta sigue, revise el conteo"] \
        if hay < total_qty else []
    return {"item_id": item_id, "sku": variante["sku"], "cantidad": total_qty, "advertencias": adv, "totales": tot}


def cambiar_cantidad(con, venta_id, item_id, cantidad) -> dict:
    if not isinstance(cantidad, int) or isinstance(cantidad, bool) or cantidad < 0:
        raise PosError("La cantidad debe ser un entero >= 0")
    with transaccion(con):
        _exigir_abierta(con, venta_id)
        if not con.execute("SELECT 1 FROM venta_items WHERE id=? AND venta_id=?", (item_id, venta_id)).fetchone():
            raise PosError("Ese ítem no pertenece a la venta")
        if cantidad == 0:
            con.execute("DELETE FROM venta_items WHERE id=?", (item_id,))
        else:
            con.execute("UPDATE venta_items SET cantidad=? WHERE id=?", (cantidad, item_id))
        return _recalcular(con, venta_id)


def quitar_item(con, venta_id, item_id) -> dict:
    return cambiar_cantidad(con, venta_id, item_id, 0)


def ver_venta(con, venta_id) -> dict:
    v = _venta(con, venta_id)
    lineas = _filas(con.execute(
        "SELECT i.id AS item_id, va.sku, m.nombre AS marca, p.modelo, va.talla, va.color, i.cantidad, "
        "i.precio_unit_usd_cents, i.cantidad*i.precio_unit_usd_cents AS importe_usd_cents "
        "FROM venta_items i JOIN variantes va ON va.id=i.variante_id JOIN productos p ON p.id=va.producto_id "
        "JOIN marcas m ON m.id=p.marca_id WHERE i.venta_id=? ORDER BY i.id", (venta_id,)))
    res = PG.resumen_pagos(con, venta_id)
    return {"venta": v, "lineas": lineas, "pagado_usd_cents": res["neto_usd_cents"],
            "pendiente_usd_cents": res["pendiente_usd_cents"], "exceso_usd_cents": res["exceso_usd_cents"],
            "numero": f"V-{v['id']:06d}"}


def anular_venta_abierta(con, usuario, venta_id, motivo):
    """Solo carritos abandonados y sin pagos. Una venta cerrada se revierte con devolución (nota de crédito)."""
    if not (motivo or "").strip():
        raise PosError("El motivo es obligatorio")
    with transaccion(con):
        _exigir_abierta(con, venta_id)
        if con.execute("SELECT 1 FROM pagos WHERE venta_id=? AND anulado=0", (venta_id,)).fetchone():
            raise PosError("La venta tiene pagos registrados: no se puede anular")
        con.execute("UPDATE ventas SET estado='anulada', anulada_motivo=? WHERE id=?", (motivo.strip(), venta_id))
        auditar(con, usuario["id"], "venta_anulada", f"{venta_id}: {motivo.strip()}")


# ---------------- cierre ----------------
def cerrar_venta(con, venta_id, usuario_id=None) -> dict:
    """Atómico: estado + salida de stock + outbox. Si algo falla (o se va la luz) la venta sigue abierta e intacta."""
    with transaccion(con):
        v = _exigir_abierta(con, venta_id)
        items = _filas(con.execute("SELECT i.id, i.variante_id, i.cantidad, i.precio_unit_usd_cents FROM venta_items i WHERE i.venta_id=? ORDER BY i.id", (venta_id,)))
        if not items:
            raise PosError("La venta no tiene productos")
        res = PG.resumen_pagos(con, venta_id)
        if res["sin_verificar"]:
            raise PosError("Falta confirmar un pago digital (revise que haya llegado al banco)")
        if res["neto_usd_cents"] < v["total_usd_cents"]:
            raise PosError(f"Falta cobrar {res['pendiente_usd_cents']} centavos USD de la venta")
        if res["exceso_usd_cents"] > PG.TOLERANCIA_USD_CENTS:
            raise PosError(f"Falta registrar el vuelto ({res['exceso_usd_cents']} centavos USD)")
        pagos = _filas(con.execute("SELECT metodo, moneda, monto_cents, equiv_usd_cents, referencia, banco FROM pagos "
                                   "WHERE venta_id=? AND anulado=0 ORDER BY id", (venta_id,)))
        vueltos = _filas(con.execute("SELECT metodo, moneda, monto_cents, equiv_usd_cents, referencia FROM vueltos "
                                     "WHERE venta_id=? AND anulado=0 ORDER BY id", (venta_id,)))
        ahora = _iso(datetime.now(timezone.utc))
        con.execute("UPDATE ventas SET estado='cerrada', cerrada_en=? WHERE id=?", (ahora, venta_id))
        for it in items:
            inventario.registrar_movimiento(con, v["sucursal_id"], it["variante_id"], "venta", it["cantidad"],
                                            f"venta {v['uuid'][:8]}", usuario_id, f"venta #{venta_id}",
                                            uuid_=f"{v['uuid']}:{it['id']}")
        detalle = {r[0]: r[1:] for r in con.execute(
            "SELECT va.id, va.sku, m.nombre, p.modelo, va.talla, va.color FROM variantes va JOIN productos p ON p.id=va.producto_id "
            "JOIN marcas m ON m.id=p.marca_id WHERE va.id IN (SELECT variante_id FROM venta_items WHERE venta_id=?)", (venta_id,))}
        items_nube = [{**it, **dict(zip(("sku", "marca", "modelo", "talla", "color"), detalle.get(it["variante_id"], (None,) * 5)))} for it in items]
        payload = json.dumps({"venta": {k: v[k] for k in ("uuid", "sucursal_id", "tasa_micro", "iva_modo", "iva_bp",
                              "subtotal_usd_cents", "iva_usd_cents", "total_usd_cents", "total_bs_centimos")},
                              "items": items_nube, "pagos": pagos, "vueltos": vueltos}, ensure_ascii=False)
        con.execute("INSERT INTO outbox(uuid,entidad,operacion,payload,ocurrido_en) VALUES(?,?,?,?,?)",
                    (f"venta:{v['uuid']}:cierre", "venta", "insert", payload, ahora))
        from core import fiscal                       # la factura queda en cola en la misma transacción; emitirla no frena la venta
        fiscal.encolar_factura(con, venta_id)
        auditar(con, usuario_id, "venta_cerrada", str(venta_id))
    return ver_venta(con, venta_id)


# ---------------- devoluciones ----------------
def devolver(con, actor, venta_id, items, motivo) -> dict:
    """items: [(sku, cantidad)]. Solo el dueño autoriza. Regresa stock a la MISMA variante y calcula el
    monto con los precios, IVA y tasa de la venta original."""
    require_rol(actor, "dueno")
    if not (motivo or "").strip():
        raise PosError("El motivo es obligatorio")
    pedido = {}
    for sku, n in items:
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            raise PosError("Las cantidades devueltas deben ser enteros positivos")
        r = con.execute("SELECT id FROM variantes WHERE sku=?", ((sku or "").strip().upper(),)).fetchone()
        if not r:
            raise PosError(f"SKU desconocido: {sku}")
        pedido[r[0]] = pedido.get(r[0], 0) + n
    if not pedido:
        raise PosError("No hay productos para devolver")
    with transaccion(con):
        v = _venta(con, venta_id)
        if v["estado"] != "cerrada":
            raise PosError("Solo se puede devolver sobre una venta cerrada")
        vendidas = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT variante_id, SUM(cantidad), MAX(precio_unit_usd_cents) FROM venta_items WHERE venta_id=? GROUP BY variante_id", (venta_id,))}
        devueltas = {r[0]: r[1] for r in con.execute(
            "SELECT di.variante_id, SUM(di.cantidad) FROM devolucion_items di JOIN devoluciones d ON d.id=di.devolucion_id "
            "WHERE d.venta_id=? GROUP BY di.variante_id", (venta_id,))}
        for vid, n in pedido.items():
            if vid not in vendidas:
                raise PosError("Ese producto (talla/color) no fue parte de esta venta")
            if n > vendidas[vid][0] - devueltas.get(vid, 0):
                raise PosError("Se intenta devolver más de lo vendido")
        completa = all(vendidas[vid][0] - devueltas.get(vid, 0) - pedido.get(vid, 0) == 0 for vid in vendidas)
        t = PR.calcular_totales([(vendidas[vid][1], n) for vid, n in pedido.items()], v["iva_bp"], v["iva_modo"], v["tasa_micro"])
        if completa:   # la última devolución cierra la cuenta exacta (sin deriva por redondeo)
            prev = con.execute("SELECT COALESCE(SUM(subtotal_usd_cents),0), COALESCE(SUM(iva_usd_cents),0), "
                               "COALESCE(SUM(total_usd_cents),0), COALESCE(SUM(total_bs_centimos),0) FROM devoluciones WHERE venta_id=?",
                               (venta_id,)).fetchone()
            t = {"subtotal_usd_cents": v["subtotal_usd_cents"] - prev[0], "iva_usd_cents": v["iva_usd_cents"] - prev[1],
                 "total_usd_cents": v["total_usd_cents"] - prev[2], "total_bs_centimos": v["total_bs_centimos"] - prev[3]}
        du = str(_uuid.uuid4())
        ahora = _iso(datetime.now(timezone.utc))
        did = con.execute("INSERT INTO devoluciones(uuid,venta_id,usuario_id,motivo,subtotal_usd_cents,iva_usd_cents,total_usd_cents,total_bs_centimos,tasa_micro,creado_en) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (du, venta_id, actor["id"], motivo.strip(), t["subtotal_usd_cents"], t["iva_usd_cents"],
                           t["total_usd_cents"], t["total_bs_centimos"], v["tasa_micro"], ahora)).lastrowid
        for vid, n in pedido.items():
            con.execute("INSERT INTO devolucion_items(devolucion_id,variante_id,cantidad,precio_unit_usd_cents) VALUES(?,?,?,?)",
                        (did, vid, n, vendidas[vid][1]))
            inventario.registrar_movimiento(con, v["sucursal_id"], vid, "devolucion", n, f"devolución: {motivo.strip()}",
                                            actor["id"], f"devolucion #{did}", uuid_=f"{du}:{vid}")
        from core import fiscal
        fiscal.encolar_nota_credito(con, did)
        con.execute("INSERT INTO outbox(uuid,entidad,operacion,payload,ocurrido_en) VALUES(?,?,?,?,?)",
                    (f"devolucion:{du}", "devolucion", "insert",
                     json.dumps({"venta_uuid": v["uuid"], "items": pedido, "motivo": motivo.strip(), **t}, ensure_ascii=False),
                     ahora))
        auditar(con, actor["id"], "devolucion", f"venta {venta_id}: {did}")
    return {"devolucion_id": did, "completa": completa, **t}
