"""Motor de precios: costo -> margen -> redondeo -> IVA -> Bs. Todo en enteros (sin floats).
Regla: el redondeo siempre sube (nunca queda por debajo del margen objetivo), salvo modo 'sin'."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from tools.db import transaccion
from core.auth import auditar, require_rol

MODOS = {"sin": None, "entero": (100, 0), ".50": (50, 0), ".99": (100, 99), ".95": (100, 95)}
TIPOS_TASA = ("bcv", "paralelo", "manual")


class PricingError(Exception):
    pass


def _ceil_div(a, b):
    return -(-a // b)


def _round_half_up(a, b):
    return (2 * a + b) // (2 * b)


def calcular_precio(costo_cents: int, margen_bp: int, modo: str = "sin") -> int:
    """precio_usd = costo x (1 + margen), redondeado hacia arriba según modo. Devuelve centavos."""
    if modo not in MODOS:
        raise PricingError(f"Modo de redondeo inválido: {modo!r}")
    if costo_cents < 0 or margen_bp < 0:
        raise PricingError("Costo y margen no pueden ser negativos")
    if costo_cents == 0:
        return 0
    num = costo_cents * (10000 + margen_bp)           # en 1/10000 de centavo
    if modo == "sin":
        return _round_half_up(num, 10000)
    step, off = MODOS[modo]
    k = _ceil_div(num - off * 10000, step * 10000)
    return k * step + off


def desglose_iva(precio_cents: int, iva_bp: int, modo: str):
    """Devuelve (base, iva, total) en centavos. 'sumado': el precio es la base. 'incluido': el precio es el total."""
    if modo == "sumado":
        iva = _round_half_up(precio_cents * iva_bp, 10000)
        return precio_cents, iva, precio_cents + iva
    if modo == "incluido":
        base = _round_half_up(precio_cents * 10000, 10000 + iva_bp)
        return base, precio_cents - base, precio_cents
    raise PricingError(f"Modo de IVA inválido: {modo!r}")


def usd_a_bs(usd_cents: int, tasa_micro: int) -> int:
    return _round_half_up(usd_cents * tasa_micro, 1_000_000)   # céntimos de Bs


def calcular_totales(items, iva_bp, modo_iva, tasa_micro) -> dict:
    """items: [(precio_unit_usd_cents, cantidad)]. El IVA se calcula sobre el subtotal (evita deriva por línea)."""
    if tasa_micro <= 0:
        raise PricingError("Tasa inválida")
    bruto = sum(p * q for p, q in items)
    base, iva, total = desglose_iva(bruto, iva_bp, modo_iva)
    return {"subtotal_usd_cents": base, "iva_usd_cents": iva, "total_usd_cents": total,
            "total_bs_centimos": usd_a_bs(total, tasa_micro), "tasa_micro": tasa_micro}


# ---------------- tasas ----------------
def a_micro(valor) -> int:
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except InvalidOperation:
        raise PricingError(f"Tasa inválida: {valor!r}")
    if d <= 0:
        raise PricingError("La tasa debe ser mayor que cero")
    return int((d * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def registrar_tasa(con, tipo, valor, usuario_id=None, vigente_desde=None) -> int:
    """La tasa se escribe a mano (o la trae un módulo que cite su fuente). Nunca se inventa."""
    if tipo not in TIPOS_TASA:
        raise PricingError(f"Tipo de tasa inválido: {tipo!r}")
    micro = a_micro(valor)
    with transaccion(con):
        if vigente_desde:
            tid = con.execute("INSERT INTO tasas_cambio(tipo,tasa_micro,vigente_desde,creado_por) VALUES(?,?,?,?)",
                              (tipo, micro, vigente_desde, usuario_id)).lastrowid
        else:
            tid = con.execute("INSERT INTO tasas_cambio(tipo,tasa_micro,creado_por) VALUES(?,?,?)",
                              (tipo, micro, usuario_id)).lastrowid
        auditar(con, usuario_id, "tasa_registrada", f"{tipo}={micro}")
        return tid


def tasa_vigente(con, tipo=None) -> dict:
    if tipo is None:
        tipo = con.execute("SELECT valor FROM config WHERE clave='tasa_default'").fetchone()[0]
    if tipo not in TIPOS_TASA:
        raise PricingError("La tasa por defecto no está definida")
    r = con.execute("SELECT id,tipo,tasa_micro,vigente_desde FROM tasas_cambio WHERE tipo=? "
                    "ORDER BY vigente_desde DESC, id DESC LIMIT 1", (tipo,)).fetchone()
    if not r:
        raise PricingError(f"No hay tasa {tipo.upper()} registrada: ingrésela antes de vender")
    return dict(zip(("id", "tipo", "tasa_micro", "vigente_desde"), r))


# ---------------- márgenes y recálculo masivo ----------------
def actualizar_margen_categoria(con, actor, categoria_id, margen_bp):
    require_rol(actor, "dueno")
    if not isinstance(margen_bp, int) or margen_bp < 0:
        raise PricingError("El margen debe ser un entero >= 0 (puntos base: 4000 = 40%)")
    with transaccion(con):
        if con.execute("UPDATE categorias SET margen_bp=? WHERE id=?", (margen_bp, categoria_id)).rowcount != 1:
            raise PricingError("Categoría inexistente")
        auditar(con, actor["id"], "margen_categoria", f"{categoria_id}={margen_bp}")


def previsualizar_recalculo(con, modo=None, categoria_id=None, marca_id=None, genero=None) -> dict:
    """No escribe nada. El margen del producto, si existe, manda sobre el de la categoría."""
    if modo is None:
        modo = con.execute("SELECT valor FROM config WHERE clave='redondeo_default'").fetchone()[0]
    sql = ("SELECT v.id, v.sku, v.costo_usd_cents, v.precio_usd_cents, COALESCE(p.margen_bp, c.margen_bp) "
           "FROM variantes v JOIN productos p ON p.id=v.producto_id JOIN categorias c ON c.id=p.categoria_id "
           "WHERE v.activo=1 AND p.activo=1")
    args = []
    for col, val in (("p.categoria_id", categoria_id), ("p.marca_id", marca_id), ("p.genero", genero)):
        if val is not None:
            sql += f" AND {col}=?"; args.append(val)
    filas = []
    for vid, sku, costo, actual, margen in con.execute(sql + " ORDER BY v.sku", args):
        nuevo = calcular_precio(costo, margen, modo)
        filas.append({"variante_id": vid, "sku": sku, "costo_usd_cents": costo, "margen_bp": margen,
                      "precio_actual": actual, "precio_nuevo": nuevo, "cambia": nuevo != actual,
                      "advertencia": "sin costo" if costo == 0 else None})
    return {"modo": modo, "filas": filas, "total": len(filas), "cambian": sum(f["cambia"] for f in filas)}


def aplicar_recalculo(con, actor, preview, motivo) -> int:
    """Aplica exactamente lo previsualizado; si un precio cambió desde la vista previa, aborta todo."""
    require_rol(actor, "dueno")
    if not (motivo or "").strip():
        raise PricingError("El motivo es obligatorio")
    try:
        tasa_id = tasa_vigente(con)["id"]
    except PricingError:
        tasa_id = None
    n = 0
    with transaccion(con):
        for f in preview["filas"]:
            if not f["cambia"]:
                continue
            actual = con.execute("SELECT precio_usd_cents FROM variantes WHERE id=?", (f["variante_id"],)).fetchone()[0]
            if actual != f["precio_actual"]:
                raise PricingError(f"El precio de {f['sku']} cambió desde la vista previa; vuelva a previsualizar")
            con.execute("UPDATE variantes SET precio_usd_cents=? WHERE id=?", (f["precio_nuevo"], f["variante_id"]))
            con.execute("INSERT INTO precios_historial(variante_id,costo_usd_cents,precio_usd_cents,tasa_id,motivo) VALUES(?,?,?,?,?)",
                        (f["variante_id"], f["costo_usd_cents"], f["precio_nuevo"], tasa_id, motivo.strip()))
            n += 1
        auditar(con, actor["id"], "recalculo_precios", f"{n} variantes, modo {preview['modo']}: {motivo.strip()}")
    return n


def historial_precios(con, variante_id):
    cur = con.execute("SELECT creado_en,costo_usd_cents,precio_usd_cents,tasa_id,motivo FROM precios_historial "
                      "WHERE variante_id=? ORDER BY id", (variante_id,))
    return [dict(zip(("creado_en", "costo_usd_cents", "precio_usd_cents", "tasa_id", "motivo"), r)) for r in cur]
