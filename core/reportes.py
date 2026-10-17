"""Reportes. Las fechas son LOCALES (Venezuela UTC-4 por defecto, config 'utc_offset_min'); la base guarda UTC.
Ventas se cuentan por fecha de cierre; cobros y caja por fecha del movimiento. Margen con el costo congelado en cada línea."""
import csv, json
from datetime import date, datetime, time, timedelta, timezone
from tools.db import transaccion
from core import inventario
from core.auth import auditar, require_rol
from core.pricing import _round_half_up


class ReporteError(Exception):
    pass


def _iso(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _a_fecha(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(x)
    except (TypeError, ValueError):
        raise ReporteError("Fecha inválida (use AAAA-MM-DD)")


def _offset_min(con):
    r = con.execute("SELECT valor FROM config WHERE clave='utc_offset_min'").fetchone()
    return int(r[0]) if r else -240


def rango_local(con, desde, hasta=None):
    """Días locales [desde, hasta] -> (inicio_utc, fin_utc_exclusivo)."""
    d0, d1 = _a_fecha(desde), _a_fecha(hasta or desde)
    if d1 < d0:
        raise ReporteError("La fecha final es anterior a la inicial")
    off = timedelta(minutes=_offset_min(con))
    ini = datetime.combine(d0, time.min, tzinfo=timezone.utc) - off
    fin = datetime.combine(d1 + timedelta(days=1), time.min, tzinfo=timezone.utc) - off
    return _iso(ini), _iso(fin)


def _suc(alias, sucursal_id):
    return ("", []) if sucursal_id is None else (f" AND {alias}.sucursal_id=?", [sucursal_id])


# ---------------- ventas ----------------
def resumen_ventas(con, actor, desde, hasta=None, sucursal_id=None) -> dict:
    require_rol(actor, "dueno")
    ini, fin = rango_local(con, desde, hasta)
    sv, av = _suc("v", sucursal_id)
    n, sub, iva, tot, bs = con.execute(
        "SELECT COUNT(*),COALESCE(SUM(subtotal_usd_cents),0),COALESCE(SUM(iva_usd_cents),0),COALESCE(SUM(total_usd_cents),0),"
        "COALESCE(SUM(total_bs_centimos),0) FROM ventas v WHERE v.estado='cerrada' AND v.cerrada_en>=? AND v.cerrada_en<?" + sv,
        [ini, fin] + av).fetchone()
    dn, dsub, diva, dtot, dbs = con.execute(
        "SELECT COUNT(*),COALESCE(SUM(d.subtotal_usd_cents),0),COALESCE(SUM(d.iva_usd_cents),0),COALESCE(SUM(d.total_usd_cents),0),"
        "COALESCE(SUM(d.total_bs_centimos),0) FROM devoluciones d JOIN ventas v ON v.id=d.venta_id "
        "WHERE d.creado_en>=? AND d.creado_en<?" + sv, [ini, fin] + av).fetchone()
    por_usuario = [dict(zip(("usuario", "ventas", "total_usd_cents"), r)) for r in con.execute(
        "SELECT COALESCE(u.nombre,'(sin usuario)'), COUNT(*), SUM(v.total_usd_cents) FROM ventas v LEFT JOIN usuarios u ON u.id=v.usuario_id "
        "WHERE v.estado='cerrada' AND v.cerrada_en>=? AND v.cerrada_en<?" + sv + " GROUP BY v.usuario_id ORDER BY SUM(v.total_usd_cents) DESC",
        [ini, fin] + av)]
    return {"desde": str(_a_fecha(desde)), "hasta": str(_a_fecha(hasta or desde)),
            "ventas": {"n": n, "subtotal_usd_cents": sub, "iva_usd_cents": iva, "total_usd_cents": tot, "total_bs_centimos": bs},
            "devoluciones": {"n": dn, "subtotal_usd_cents": dsub, "iva_usd_cents": diva, "total_usd_cents": dtot, "total_bs_centimos": dbs},
            "neto_usd_cents": tot - dtot, "neto_bs_centimos": bs - dbs,
            "ticket_promedio_usd_cents": _round_half_up(tot, n) if n else 0, "por_usuario": por_usuario}


def ventas_por_dia(con, actor, desde, hasta, sucursal_id=None):
    """Serie diaria (con ceros en días sin ventas) para gráficos."""
    require_rol(actor, "dueno")
    ini, fin = rango_local(con, desde, hasta)
    mod = f"{_offset_min(con):+d} minutes"
    sv, av = _suc("v", sucursal_id)
    dias, d0 = {}, _a_fecha(desde)
    for k in range((_a_fecha(hasta) - d0).days + 1):
        dias[str(d0 + timedelta(days=k))] = {"fecha": str(d0 + timedelta(days=k)), "ventas": 0, "total_usd_cents": 0,
                                             "total_bs_centimos": 0, "devoluciones_usd_cents": 0, "neto_usd_cents": 0}
    for f, n, t, b in con.execute(
            "SELECT date(v.cerrada_en, ?), COUNT(*), SUM(v.total_usd_cents), SUM(v.total_bs_centimos) FROM ventas v "
            "WHERE v.estado='cerrada' AND v.cerrada_en>=? AND v.cerrada_en<?" + sv + " GROUP BY 1", [mod, ini, fin] + av):
        dias[f].update(ventas=n, total_usd_cents=t, total_bs_centimos=b)
    for f, t in con.execute(
            "SELECT date(d.creado_en, ?), SUM(d.total_usd_cents) FROM devoluciones d JOIN ventas v ON v.id=d.venta_id "
            "WHERE d.creado_en>=? AND d.creado_en<?" + sv + " GROUP BY 1", [mod, ini, fin] + av):
        dias[f]["devoluciones_usd_cents"] = t
    for d in dias.values():
        d["neto_usd_cents"] = d["total_usd_cents"] - d["devoluciones_usd_cents"]
    return list(dias.values())


def _lineas_netas(con, ini, fin, sucursal_id):
    """Líneas de ventas cerradas en el período, descontando lo devuelto de esas ventas."""
    sv, av = _suc("v", sucursal_id)
    cur = con.execute(
        "SELECT i.venta_id,i.variante_id,i.cantidad,i.precio_unit_usd_cents,i.costo_unit_usd_cents,v.iva_modo,v.iva_bp,"
        "va.sku,m.nombre,p.modelo,p.id,"
        "COALESCE((SELECT SUM(di.cantidad) FROM devolucion_items di JOIN devoluciones d ON d.id=di.devolucion_id "
        "WHERE d.venta_id=i.venta_id AND di.variante_id=i.variante_id),0) "
        "FROM venta_items i JOIN ventas v ON v.id=i.venta_id JOIN variantes va ON va.id=i.variante_id "
        "JOIN productos p ON p.id=va.producto_id JOIN marcas m ON m.id=p.marca_id "
        "WHERE v.estado='cerrada' AND v.cerrada_en>=? AND v.cerrada_en<?" + sv, [ini, fin] + av)
    out = []
    for vid, var, cant, precio, costo, modo, iva_bp, sku, marca, modelo, pid, dev in cur:
        neta = cant - dev
        if neta <= 0:
            continue
        bruto = precio * neta
        base = _round_half_up(bruto * 10000, 10000 + iva_bp) if modo == "incluido" else bruto
        out.append({"variante_id": var, "sku": sku, "marca": marca, "modelo": modelo, "producto_id": pid, "unidades": neta,
                    "ingreso_base_usd_cents": base, "costo_usd_cents": None if costo is None else costo * neta})
    return out


def margen_periodo(con, actor, desde, hasta=None, sucursal_id=None) -> dict:
    require_rol(actor, "dueno")
    ini, fin = rango_local(con, desde, hasta)
    ing = cos = sin_costo = n_sin = 0
    for l in _lineas_netas(con, ini, fin, sucursal_id):
        if l["costo_usd_cents"] is None:
            sin_costo += l["ingreso_base_usd_cents"]; n_sin += 1
        else:
            ing += l["ingreso_base_usd_cents"]; cos += l["costo_usd_cents"]
    return {"ingresos_base_usd_cents": ing, "costo_usd_cents": cos, "margen_usd_cents": ing - cos,
            "margen_bp": _round_half_up((ing - cos) * 10000, ing) if ing else 0,
            "lineas_sin_costo": n_sin, "ingresos_sin_costo_usd_cents": sin_costo}


def top_productos(con, actor, desde, hasta=None, sucursal_id=None, limite=10, por="variante", orden="unidades"):
    require_rol(actor, "dueno")
    if por not in ("variante", "modelo") or orden not in ("unidades", "ingreso", "margen"):
        raise ReporteError("Parámetros inválidos: por=variante|modelo, orden=unidades|ingreso|margen")
    ini, fin = rango_local(con, desde, hasta)
    acc = {}
    for l in _lineas_netas(con, ini, fin, sucursal_id):
        k = l["sku"] if por == "variante" else l["producto_id"]
        a = acc.setdefault(k, {"clave": l["sku"] if por == "variante" else f"{l['marca']} {l['modelo']}",
                               "unidades": 0, "ingreso_base_usd_cents": 0, "margen_usd_cents": 0})
        a["unidades"] += l["unidades"]; a["ingreso_base_usd_cents"] += l["ingreso_base_usd_cents"]
        if l["costo_usd_cents"] is not None:
            a["margen_usd_cents"] += l["ingreso_base_usd_cents"] - l["costo_usd_cents"]
    campo = {"unidades": "unidades", "ingreso": "ingreso_base_usd_cents", "margen": "margen_usd_cents"}[orden]
    filas = sorted(acc.values(), key=lambda a: (-a[campo], -a["unidades"], a["clave"]))
    return filas[:limite]


# ---------------- cobros y caja ----------------
def cobros_por_metodo(con, sucursal_id, ini, fin):
    """Flujo de dinero [ini, fin): pagos recibidos menos vueltos y reembolsos, por método y moneda."""
    sv, av = _suc("v", sucursal_id)
    claves = {}

    def acum(sql, campo_monto, campo_equiv):
        for metodo, moneda, monto, equiv, n in con.execute(sql, [ini, fin] + av):
            k = claves.setdefault((metodo, moneda), {"metodo": metodo, "moneda": moneda, "cobrado_cents": 0, "devuelto_cents": 0,
                                                    "cobrado_equiv_usd_cents": 0, "devuelto_equiv_usd_cents": 0, "movimientos": 0})
            k[campo_monto] += monto; k[campo_equiv] += equiv; k["movimientos"] += n
    acum("SELECT p.metodo,p.moneda,SUM(p.monto_cents),SUM(p.equiv_usd_cents),COUNT(*) FROM pagos p JOIN ventas v ON v.id=p.venta_id "
         "WHERE p.anulado=0 AND p.creado_en>=? AND p.creado_en<?" + sv + " GROUP BY 1,2", "cobrado_cents", "cobrado_equiv_usd_cents")
    acum("SELECT x.metodo,x.moneda,SUM(x.monto_cents),SUM(x.equiv_usd_cents),COUNT(*) FROM vueltos x JOIN ventas v ON v.id=x.venta_id "
         "WHERE x.anulado=0 AND x.creado_en>=? AND x.creado_en<?" + sv + " GROUP BY 1,2", "devuelto_cents", "devuelto_equiv_usd_cents")
    acum("SELECT r.metodo,r.moneda,SUM(r.monto_cents),SUM(r.equiv_usd_cents),COUNT(*) FROM devolucion_reembolsos r "
         "JOIN devoluciones d ON d.id=r.devolucion_id JOIN ventas v ON v.id=d.venta_id "
         "WHERE r.creado_en>=? AND r.creado_en<?" + sv + " GROUP BY 1,2", "devuelto_cents", "devuelto_equiv_usd_cents")
    out = []
    for k in sorted(claves.values(), key=lambda x: (x["metodo"], x["moneda"])):
        k["neto_cents"] = k["cobrado_cents"] - k["devuelto_cents"]
        k["neto_equiv_usd_cents"] = k["cobrado_equiv_usd_cents"] - k["devuelto_equiv_usd_cents"]
        out.append(k)
    return out


def cobros_del_dia(con, actor, desde, hasta=None, sucursal_id=None):
    require_rol(actor, "dueno")
    return cobros_por_metodo(con, sucursal_id, *rango_local(con, desde, hasta))


def _efectivo(cobros):
    usd = sum(c["neto_cents"] for c in cobros if c["metodo"] == "efectivo_usd")
    bs = sum(c["neto_cents"] for c in cobros if c["metodo"] == "efectivo_bs")
    return usd, bs


def ultimo_cierre(con, sucursal_id):
    r = con.execute("SELECT id,hasta,dejado_usd_cents,dejado_bs_centimos FROM cierres_caja WHERE sucursal_id=? ORDER BY hasta DESC, id DESC LIMIT 1",
                    (sucursal_id,)).fetchone()
    return dict(zip(("id", "hasta", "dejado_usd_cents", "dejado_bs_centimos"), r)) if r else None


def cerrar_caja(con, usuario, sucursal_id, contado_usd_cents, contado_bs_centimos, retirado_usd_cents=0, retirado_bs_centimos=0,
                fondo_usd_cents=None, fondo_bs_centimos=None, notas=None, ahora=None) -> dict:
    """Arqueo del efectivo (USD y Bs) desde el último cierre. Esperado = fondo + cobrado - vueltos - reembolsos.
    El fondo inicial es lo que se dejó en el cierre anterior (o el que se indique en el primer cierre)."""
    for v in (contado_usd_cents, contado_bs_centimos, retirado_usd_cents, retirado_bs_centimos):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ReporteError("Los montos contados y retirados deben ser enteros >= 0 (centavos)")
    if retirado_usd_cents > contado_usd_cents or retirado_bs_centimos > contado_bs_centimos:
        raise ReporteError("No se puede retirar más de lo contado")
    fin = _iso(ahora or datetime.now(timezone.utc))
    with transaccion(con):
        prev = ultimo_cierre(con, sucursal_id)
        ini = prev["hasta"] if prev else "0000-01-01T00:00:00.000Z"
        if fin <= ini:
            raise ReporteError("La hora del cierre debe ser posterior al cierre anterior")
        f_usd = fondo_usd_cents if fondo_usd_cents is not None else (prev["dejado_usd_cents"] if prev else 0)
        f_bs = fondo_bs_centimos if fondo_bs_centimos is not None else (prev["dejado_bs_centimos"] if prev else 0)
        cobros = cobros_por_metodo(con, sucursal_id, ini, fin)
        e_usd, e_bs = _efectivo(cobros)
        e_usd += f_usd; e_bs += f_bs
        adv = []
        n = con.execute("SELECT COUNT(DISTINCT v.id) FROM ventas v JOIN pagos p ON p.venta_id=v.id WHERE v.sucursal_id=? AND v.estado='abierta' AND p.anulado=0",
                        (sucursal_id,)).fetchone()[0]
        if n:
            adv.append(f"Hay {n} venta(s) abierta(s) con pagos ya recibidos: su dinero ya cuenta en este cierre")
        n = con.execute("SELECT COUNT(*) FROM pagos p JOIN ventas v ON v.id=p.venta_id WHERE v.sucursal_id=? AND p.anulado=0 AND p.verificado=0",
                        (sucursal_id,)).fetchone()[0]
        if n:
            adv.append(f"Hay {n} pago(s) digital(es) sin verificar")
        n = con.execute("SELECT COUNT(*) FROM devoluciones d JOIN ventas v ON v.id=d.venta_id WHERE v.sucursal_id=? AND d.creado_en>=? AND d.creado_en<? "
                        "AND d.total_usd_cents > COALESCE((SELECT SUM(r.equiv_usd_cents) FROM devolucion_reembolsos r WHERE r.devolucion_id=d.id),0) + 1",
                        (sucursal_id, ini, fin)).fetchone()[0]
        if n:
            adv.append(f"Hay {n} devolución(es) del período sin reembolso registrado")
        d_usd, d_bs = contado_usd_cents - e_usd, contado_bs_centimos - e_bs
        cid = con.execute(
            "INSERT INTO cierres_caja(sucursal_id,usuario_id,desde,hasta,fondo_usd_cents,fondo_bs_centimos,esperado_usd_cents,esperado_bs_centimos,"
            "contado_usd_cents,contado_bs_centimos,diferencia_usd_cents,diferencia_bs_centimos,retirado_usd_cents,retirado_bs_centimos,"
            "dejado_usd_cents,dejado_bs_centimos,notas,detalle) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sucursal_id, usuario["id"], ini, fin, f_usd, f_bs, e_usd, e_bs, contado_usd_cents, contado_bs_centimos, d_usd, d_bs,
             retirado_usd_cents, retirado_bs_centimos, contado_usd_cents - retirado_usd_cents, contado_bs_centimos - retirado_bs_centimos,
             (notas or "").strip() or None, json.dumps(cobros, ensure_ascii=False))).lastrowid
        auditar(con, usuario["id"], "cierre_caja", f"{cid}: dif USD {d_usd}, Bs {d_bs}")
    return {"cierre_id": cid, "desde": ini, "hasta": fin, "esperado_usd_cents": e_usd, "esperado_bs_centimos": e_bs,
            "diferencia_usd_cents": d_usd, "diferencia_bs_centimos": d_bs, "cobros": cobros, "advertencias": adv}


# ---------------- inventario ----------------
def stock_critico(con, sucursal_id) -> dict:
    return {"bajo_minimo": inventario.alertas_stock_minimo(con, sucursal_id), "negativos": inventario.stock_negativo(con, sucursal_id),
            "valor_inventario": inventario.resumen_stock(con, sucursal_id)}


def sin_rotacion(con, actor, sucursal_id, dias=60, ahora=None):
    """Variantes con stock que no se venden hace N días (capital detenido, a costo)."""
    require_rol(actor, "dueno")
    corte = _iso((ahora or datetime.now(timezone.utc)) - timedelta(days=dias))
    cur = con.execute(
        "SELECT va.sku, s.stock, va.costo_usd_cents, (SELECT MAX(v.cerrada_en) FROM venta_items i JOIN ventas v ON v.id=i.venta_id "
        "WHERE i.variante_id=va.id AND v.estado='cerrada' AND v.sucursal_id=?) FROM stock_actual s JOIN variantes va ON va.id=s.variante_id "
        "WHERE s.sucursal_id=? AND s.stock>0 AND va.activo=1", (sucursal_id, sucursal_id))
    filas = [{"sku": sku, "stock": st, "valor_costo_usd_cents": st * costo, "ultima_venta": ultima}
             for sku, st, costo, ultima in cur if ultima is None or ultima < corte]
    return sorted(filas, key=lambda f: (-f["valor_costo_usd_cents"], f["sku"]))


# ---------------- exportación ----------------
def _dinero(c):
    return f"{c // 100},{abs(c) % 100:02d}" if c >= 0 else f"-{abs(c) // 100},{abs(c) % 100:02d}"


def exportar_csv(filas, ruta, dinero=()):
    """CSV que abre bien en Excel en español (UTF-8 con BOM, separador ';'). Las columnas en 'dinero' (centavos) salen como 12,50."""
    if not filas:
        raise ReporteError("No hay datos para exportar")
    cols = list(filas[0].keys())
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cols)
        for fila in filas:
            w.writerow([_dinero(fila[c]) if c in dinero and fila[c] is not None else ("" if fila[c] is None else fila[c]) for c in cols])
    return str(ruta)
