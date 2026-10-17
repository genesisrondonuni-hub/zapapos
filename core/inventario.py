"""Inventario: kardex append-only. Las ventas NUNCA se bloquean por stock (el conteo físico manda);
las salidas manuales (ajuste negativo, traslado) sí exigen stock suficiente."""
import json, uuid as _uuid
from tools.db import transaccion
from core.auth import auditar, require_rol

SIGNO = {"compra": 1, "devolucion": 1, "ajuste_pos": 1, "traslado_in": 1,
         "venta": -1, "ajuste_neg": -1, "traslado_out": -1}


class InventarioError(Exception):
    pass


def stock(con, sucursal_id, variante_id) -> int:
    return con.execute("SELECT COALESCE(SUM(cantidad),0) FROM inventario_movimientos WHERE sucursal_id=? AND variante_id=?",
                       (sucursal_id, variante_id)).fetchone()[0]


def _evento(con, mov_id):
    """Cada movimiento viaja a la nube como evento (idempotente por su uuid): la nube calcula el stock sumándolos."""
    r = con.execute("SELECT m.uuid,m.sucursal_id,v.sku,m.tipo,m.cantidad,m.motivo,m.referencia,m.creado_en FROM inventario_movimientos m "
                    "JOIN variantes v ON v.id=m.variante_id WHERE m.id=?", (mov_id,)).fetchone()
    con.execute("INSERT OR IGNORE INTO outbox(uuid,entidad,operacion,payload) VALUES(?,?,?,?)",
                (f"mov:{r[0]}", "movimiento", "insert", json.dumps(dict(zip(("uuid", "sucursal_id", "sku", "tipo", "cantidad", "motivo", "referencia", "creado_en"), r)), ensure_ascii=False)))


def registrar_movimiento(con, sucursal_id, variante_id, tipo, cantidad, motivo,
                         usuario_id=None, referencia=None, uuid_=None) -> int:
    """cantidad siempre positiva; el signo lo da el tipo. uuid_ repetido = reintento de sync (idempotente)."""
    if tipo not in SIGNO:
        raise InventarioError(f"Tipo de movimiento inválido: {tipo!r}")
    if not isinstance(cantidad, int) or isinstance(cantidad, bool) or cantidad <= 0:
        raise InventarioError("La cantidad debe ser un entero positivo")
    if not (motivo or "").strip():
        raise InventarioError("El motivo es obligatorio")
    uuid_ = uuid_ or str(_uuid.uuid4())
    with transaccion(con):
        previo = con.execute("SELECT id FROM inventario_movimientos WHERE uuid=?", (uuid_,)).fetchone()
        if previo:
            return previo[0]
        if not con.execute("SELECT 1 FROM variantes WHERE id=?", (variante_id,)).fetchone():
            raise InventarioError("Variante inexistente")
        if SIGNO[tipo] < 0 and tipo != "venta" and stock(con, sucursal_id, variante_id) < cantidad:
            raise InventarioError("Stock insuficiente para esta salida")
        mid = con.execute(
            "INSERT INTO inventario_movimientos(uuid,sucursal_id,variante_id,tipo,cantidad,motivo,usuario_id,referencia) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uuid_, sucursal_id, variante_id, tipo, SIGNO[tipo] * cantidad, motivo.strip(), usuario_id, referencia)).lastrowid
        _evento(con, mid)
        return mid


def registrar_compra(con, actor, sucursal_id, variante_id, cantidad, motivo="compra a proveedor", referencia=None):
    require_rol(actor, "dueno")
    return registrar_movimiento(con, sucursal_id, variante_id, "compra", cantidad, motivo, actor["id"], referencia)


def reversar_movimiento(con, actor, movimiento_id, motivo) -> int:
    """Contra-asiento: nada se borra. Un movimiento solo se reversa una vez y una reversa no se reversa."""
    require_rol(actor, "dueno")
    if not (motivo or "").strip():
        raise InventarioError("El motivo es obligatorio")
    with transaccion(con):
        m = con.execute("SELECT sucursal_id,variante_id,tipo,cantidad FROM inventario_movimientos WHERE id=?",
                        (movimiento_id,)).fetchone()
        if not m:
            raise InventarioError("Movimiento inexistente")
        suc, var, tipo, cant = m
        if tipo == "reversa":
            raise InventarioError("Una reversa no se puede reversar")
        if con.execute("SELECT 1 FROM inventario_movimientos WHERE reversa_de=?", (movimiento_id,)).fetchone():
            raise InventarioError("Este movimiento ya fue reversado")
        if cant > 0 and stock(con, suc, var) < cant:
            raise InventarioError("No se puede reversar: el stock actual no alcanza")
        mid = con.execute(
            "INSERT INTO inventario_movimientos(uuid,sucursal_id,variante_id,tipo,cantidad,motivo,usuario_id,referencia,reversa_de) "
            "VALUES(?,?,?,'reversa',?,?,?,?,?)",
            (str(_uuid.uuid4()), suc, var, -cant, motivo.strip(), actor["id"], f"reversa de #{movimiento_id}", movimiento_id)).lastrowid
        _evento(con, mid)
        auditar(con, actor["id"], "reversa_movimiento", str(movimiento_id))
        return mid


def ajustar_a_conteo(con, actor, sucursal_id, variante_id, conteo_fisico, motivo):
    """Deja el sistema igual al conteo físico. Devuelve id del ajuste o None si ya coincidía."""
    require_rol(actor, "dueno")
    if not isinstance(conteo_fisico, int) or conteo_fisico < 0:
        raise InventarioError("El conteo debe ser un entero >= 0")
    with transaccion(con):
        diff = conteo_fisico - stock(con, sucursal_id, variante_id)
        if diff == 0:
            return None
        return registrar_movimiento(con, sucursal_id, variante_id, "ajuste_pos" if diff > 0 else "ajuste_neg",
                                    abs(diff), motivo, actor["id"], "conteo físico")


def trasladar(con, actor, origen_id, destino_id, variante_id, cantidad, motivo):
    require_rol(actor, "dueno")
    if origen_id == destino_id:
        raise InventarioError("Origen y destino deben ser distintos")
    ref = f"traslado-{_uuid.uuid4().hex[:8]}"
    with transaccion(con):
        registrar_movimiento(con, origen_id, variante_id, "traslado_out", cantidad, motivo, actor["id"], ref)
        registrar_movimiento(con, destino_id, variante_id, "traslado_in", cantidad, motivo, actor["id"], ref)
    return ref


def kardex(con, sucursal_id, variante_id):
    cur = con.execute(
        "SELECT id,creado_en,tipo,cantidad,motivo,usuario_id,referencia,reversa_de,SUM(cantidad) OVER (ORDER BY id) "
        "FROM inventario_movimientos WHERE sucursal_id=? AND variante_id=? ORDER BY id", (sucursal_id, variante_id))
    return [dict(zip(("id", "creado_en", "tipo", "cantidad", "motivo", "usuario_id", "referencia", "reversa_de", "saldo"), r))
            for r in cur]


def establecer_stock_minimo(con, actor, variante_id, minimo):
    require_rol(actor, "dueno")
    if not isinstance(minimo, int) or minimo < 0:
        raise InventarioError("El mínimo debe ser un entero >= 0")
    with transaccion(con):
        if con.execute("UPDATE variantes SET stock_minimo=? WHERE id=?", (minimo, variante_id)).rowcount != 1:
            raise InventarioError("Variante inexistente")
        auditar(con, actor["id"], "stock_minimo", f"{variante_id}={minimo}")


def alertas_stock_minimo(con, sucursal_id):
    """Incluye variantes sin ningún movimiento (stock 0)."""
    cur = con.execute(
        "SELECT v.id, v.sku, v.stock_minimo, COALESCE(s.stock,0) AS st FROM variantes v "
        "JOIN productos p ON p.id=v.producto_id "
        "LEFT JOIN stock_actual s ON s.variante_id=v.id AND s.sucursal_id=? "
        "WHERE v.activo=1 AND p.activo=1 AND v.stock_minimo>0 AND COALESCE(s.stock,0)<=v.stock_minimo "
        "ORDER BY st, v.sku", (sucursal_id,))
    return [dict(zip(("variante_id", "sku", "stock_minimo", "stock"), r)) for r in cur]


def stock_negativo(con, sucursal_id):
    cur = con.execute("SELECT v.id, v.sku, s.stock FROM stock_actual s JOIN variantes v ON v.id=s.variante_id "
                      "WHERE s.sucursal_id=? AND s.stock<0 ORDER BY s.stock", (sucursal_id,))
    return [dict(zip(("variante_id", "sku", "stock"), r)) for r in cur]


def resumen_stock(con, sucursal_id) -> dict:
    pares, valor = con.execute(
        "SELECT COALESCE(SUM(s.stock),0), COALESCE(SUM(s.stock*v.costo_usd_cents),0) FROM stock_actual s "
        "JOIN variantes v ON v.id=s.variante_id WHERE s.sucursal_id=? AND s.stock>0", (sucursal_id,)).fetchone()
    return {"pares": pares, "valor_costo_usd_cents": valor}
