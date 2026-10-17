"""Pagos: una venta = N pagos heterogéneos (Pago Móvil, Zelle, USDT, Binance Pay, efectivo Bs/USD, punto).
Los pagos en Bs se convierten con la tasa GUARDADA EN LA VENTA. Los pagos digitales exigen verificación del cajero.
Neto cobrado = pagos - vueltos. Nada se borra: un pago errado se anula con motivo y queda el rastro."""
import re, sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from tools.db import transaccion
from core.auth import auditar, require_rol, AuthError
from core.pricing import usd_a_bs, _round_half_up

TOLERANCIA_USD_CENTS = 1   # diferencia máxima aceptada por redondeo al cerrar

# metodo: (moneda, referencia obligatoria, digital (requiere verificación), efectivo)
METODOS = {
    "pago_movil": ("BS", True, True, False),
    "zelle": ("USD", True, True, False),
    "usdt": ("USDT", True, True, False),          # 1 USDT = 1 USD (paridad asumida; revisar si se desvía)
    "binance_pay": ("USDT", True, True, False),
    "efectivo_bs": ("BS", False, False, True),
    "efectivo_usd": ("USD", False, False, True),
    "punto_debito": ("BS", True, False, False),
    "punto_credito": ("BS", True, False, False),
}
VUELTOS = {"efectivo_usd": "USD", "efectivo_bs": "BS", "pago_movil": "BS"}


class PagosError(Exception):
    pass


def a_cents(valor) -> int:
    """'12,50' / '1.234,56' / '$ 12.5' -> centavos. Para pantallas que reciben texto."""
    s = re.sub(r"[\s$]|Bs", "", str(valor))
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise PagosError(f"Monto inválido: {valor!r}")
    if d <= 0:
        raise PagosError("El monto debe ser mayor que cero")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _venta(con, venta_id, abierta=True):
    r = con.execute("SELECT estado, total_usd_cents, total_bs_centimos, tasa_micro FROM ventas WHERE id=?", (venta_id,)).fetchone()
    if not r:
        raise PagosError("Venta inexistente")
    if abierta and r[0] != "abierta":
        raise PagosError(f"La venta está {r[0]}: ya no admite pagos")
    return {"estado": r[0], "total_usd": r[1], "total_bs": r[2], "tasa_micro": r[3]}


def resumen_pagos(con, venta_id) -> dict:
    v = _venta(con, venta_id, abierta=False)
    pagado = con.execute("SELECT COALESCE(SUM(equiv_usd_cents),0) FROM pagos WHERE venta_id=? AND anulado=0", (venta_id,)).fetchone()[0]
    vueltos = con.execute("SELECT COALESCE(SUM(equiv_usd_cents),0) FROM vueltos WHERE venta_id=? AND anulado=0", (venta_id,)).fetchone()[0]
    neto = pagado - vueltos
    pend, exc = max(v["total_usd"] - neto, 0), max(neto - v["total_usd"], 0)
    sin_ver = [r[0] for r in con.execute("SELECT id FROM pagos WHERE venta_id=? AND anulado=0 AND verificado=0 ORDER BY id", (venta_id,))]
    return {"total_usd_cents": v["total_usd"], "pagado_usd_cents": pagado, "vueltos_usd_cents": vueltos,
            "neto_usd_cents": neto, "pendiente_usd_cents": pend, "pendiente_bs_centimos": usd_a_bs(pend, v["tasa_micro"]),
            "exceso_usd_cents": exc, "exceso_bs_centimos": usd_a_bs(exc, v["tasa_micro"]), "sin_verificar": sin_ver}


def convertir_usd_a_bs(con, venta_id, usd_cents) -> int:
    """Cuántos Bs equivalen a esos USD con la tasa de ESA venta (para repartir pagos)."""
    return usd_a_bs(usd_cents, _venta(con, venta_id, abierta=False)["tasa_micro"])


def _equiv(moneda, monto, tasa_micro, pend_usd, pend_bs):
    if moneda != "BS":
        return monto
    if monto == pend_bs and pend_usd > 0:       # pagar exactamente lo pendiente en Bs cierra exacto en USD
        return pend_usd
    return _round_half_up(monto * 1_000_000, tasa_micro)


def _ref(texto):
    return re.sub(r"\s", "", texto or "").upper()


def registrar_pago(con, usuario, venta_id, metodo, monto_cents, referencia=None, banco=None) -> dict:
    if metodo not in METODOS:
        raise PagosError(f"Método de pago inválido: {metodo!r}")
    if not isinstance(monto_cents, int) or isinstance(monto_cents, bool) or monto_cents <= 0:
        raise PagosError("El monto debe ser un entero positivo (centavos)")
    moneda, ref_obl, digital, efectivo = METODOS[metodo]
    ref = _ref(referencia)
    if ref_obl and len(re.sub(r"[^A-Z0-9]", "", ref)) < 4:
        raise PagosError("La referencia es obligatoria (mínimo 4 caracteres)")
    banco = (banco or "").strip() or None
    if metodo == "pago_movil" and not banco:
        raise PagosError("Indique el banco del Pago Móvil")
    if metodo != "pago_movil":
        banco = None
    try:
        with transaccion(con):
            v = _venta(con, venta_id)
            res = resumen_pagos(con, venta_id)
            if res["pendiente_usd_cents"] == 0:
                raise PagosError("La venta ya está cubierta: no admite más pagos")
            pend, pend_bs = res["pendiente_usd_cents"], res["pendiente_bs_centimos"]
            equiv = _equiv(moneda, monto_cents, v["tasa_micro"], pend, pend_bs)
            if equiv <= 0:
                raise PagosError("Monto demasiado pequeño")
            if not efectivo and equiv > pend:
                raise PagosError(f"Un pago electrónico no puede exceder lo pendiente ({pend} centavos USD)")
            if ref:
                igual = con.execute("SELECT id FROM pagos WHERE venta_id=? AND metodo=? AND COALESCE(banco,'')=COALESCE(?,'') "
                                    "AND referencia=? AND monto_cents=? AND anulado=0", (venta_id, metodo, banco, ref, monto_cents)).fetchone()
                if igual:    # doble clic / reintento: no se duplica
                    return {"pago_id": igual[0], "duplicado": True, "equiv_usd_cents": equiv, "resumen": res}
                if digital:
                    otro = con.execute("SELECT venta_id FROM pagos WHERE metodo=? AND COALESCE(banco,'')=COALESCE(?,'') "
                                       "AND referencia=? AND anulado=0", (metodo, banco, ref)).fetchone()
                    if otro:
                        raise PagosError(f"Esa referencia ya fue usada en la venta V-{otro[0]:06d}")
            pid = con.execute("INSERT INTO pagos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents,referencia,banco,verificado,usuario_id) "
                              "VALUES(?,?,?,?,?,?,?,?,?)",
                              (venta_id, metodo, moneda, monto_cents, equiv, ref or None, banco, 0 if digital else 1, usuario["id"])).lastrowid
            auditar(con, usuario["id"], "pago_registrado", f"venta {venta_id}: {metodo} {monto_cents} {moneda}")
            nuevo = resumen_pagos(con, venta_id)
    except sqlite3.IntegrityError as e:
        raise PagosError("Esa referencia ya fue registrada") from e
    out = {"pago_id": pid, "duplicado": False, "equiv_usd_cents": equiv, "requiere_verificacion": digital, "resumen": nuevo}
    if efectivo and equiv > pend:
        out["vuelto_sugerido"] = {"usd_cents": equiv - pend,
                                  "bs_centimos": monto_cents - pend_bs if moneda == "BS" else usd_a_bs(equiv - pend, v["tasa_micro"])}
    return out


def confirmar_pago(con, usuario, pago_id):
    """El cajero confirma que el dinero se vio reflejado en el banco / app (Pago Móvil, Zelle, USDT, Binance)."""
    with transaccion(con):
        r = con.execute("SELECT venta_id, verificado, anulado FROM pagos WHERE id=?", (pago_id,)).fetchone()
        if not r:
            raise PagosError("Pago inexistente")
        if r[2]:
            raise PagosError("Ese pago fue anulado")
        _venta(con, r[0])
        if not r[1]:
            con.execute("UPDATE pagos SET verificado=1, verificado_por=?, verificado_en=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                        (usuario["id"], pago_id))
            auditar(con, usuario["id"], "pago_verificado", str(pago_id))


def anular_pago(con, actor, pago_id, motivo):
    """Corrige un error de digitación. Un pago ya verificado o en efectivo solo lo anula el dueño."""
    if not (motivo or "").strip():
        raise PagosError("El motivo es obligatorio")
    with transaccion(con):
        r = con.execute("SELECT venta_id, verificado, anulado, metodo FROM pagos WHERE id=?", (pago_id,)).fetchone()
        if not r:
            raise PagosError("Pago inexistente")
        if r[2]:
            raise PagosError("Ese pago ya está anulado")
        _venta(con, r[0])
        if r[1] and actor["rol"] != "dueno":
            raise AuthError("Solo el dueño puede anular un pago ya verificado o en efectivo")
        con.execute("UPDATE pagos SET anulado=1, anulado_motivo=? WHERE id=?", (motivo.strip(), pago_id))
        auditar(con, actor["id"], "pago_anulado", f"{pago_id}: {motivo.strip()}")


def registrar_vuelto(con, usuario, venta_id, metodo, monto_cents, referencia=None) -> dict:
    if metodo not in VUELTOS:
        raise PagosError(f"Método de vuelto inválido: {metodo!r}")
    if not isinstance(monto_cents, int) or isinstance(monto_cents, bool) or monto_cents <= 0:
        raise PagosError("El monto debe ser un entero positivo (centavos)")
    moneda = VUELTOS[metodo]
    with transaccion(con):
        v = _venta(con, venta_id)
        res = resumen_pagos(con, venta_id)
        if res["exceso_usd_cents"] <= 0:
            raise PagosError("No hay vuelto pendiente por devolver")
        equiv = _round_half_up(monto_cents * 1_000_000, v["tasa_micro"]) if moneda == "BS" else monto_cents
        if equiv > res["exceso_usd_cents"] + TOLERANCIA_USD_CENTS:
            raise PagosError("El vuelto excede lo que se debe devolver")
        vid = con.execute("INSERT INTO vueltos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents,referencia,usuario_id) VALUES(?,?,?,?,?,?,?)",
                          (venta_id, metodo, moneda, monto_cents, equiv, _ref(referencia) or None, usuario["id"])).lastrowid
        auditar(con, usuario["id"], "vuelto_registrado", f"venta {venta_id}: {metodo} {monto_cents} {moneda}")
        return {"vuelto_id": vid, "resumen": resumen_pagos(con, venta_id)}


def anular_vuelto(con, actor, vuelto_id, motivo):
    require_rol(actor, "dueno")
    if not (motivo or "").strip():
        raise PagosError("El motivo es obligatorio")
    with transaccion(con):
        r = con.execute("SELECT venta_id, anulado FROM vueltos WHERE id=?", (vuelto_id,)).fetchone()
        if not r:
            raise PagosError("Vuelto inexistente")
        if r[1]:
            raise PagosError("Ese vuelto ya está anulado")
        _venta(con, r[0])
        con.execute("UPDATE vueltos SET anulado=1, anulado_motivo=? WHERE id=?", (motivo.strip(), vuelto_id))
        auditar(con, actor["id"], "vuelto_anulado", f"{vuelto_id}: {motivo.strip()}")


def listar_pagos(con, venta_id):
    cur = con.execute("SELECT id, metodo, moneda, monto_cents, equiv_usd_cents, referencia, banco, verificado, anulado FROM pagos "
                      "WHERE venta_id=? ORDER BY id", (venta_id,))
    cols = ("id", "metodo", "moneda", "monto_cents", "equiv_usd_cents", "referencia", "banco", "verificado", "anulado")
    return [dict(zip(cols, r)) for r in cur]


REEMBOLSOS = {"efectivo_usd": "USD", "efectivo_bs": "BS", "pago_movil": "BS", "zelle": "USD", "usdt": "USDT"}


def pendiente_reembolso(con, devolucion_id) -> int:
    r = con.execute("SELECT total_usd_cents FROM devoluciones WHERE id=?", (devolucion_id,)).fetchone()
    if not r:
        raise PagosError("Devolución inexistente")
    pagado = con.execute("SELECT COALESCE(SUM(equiv_usd_cents),0) FROM devolucion_reembolsos WHERE devolucion_id=?", (devolucion_id,)).fetchone()[0]
    return max(r[0] - pagado, 0)


def registrar_reembolso(con, actor, devolucion_id, metodo, monto_cents, referencia=None) -> dict:
    """Dinero que SALE hacia el cliente por una devolución (afecta el cierre de caja). Solo el dueño."""
    require_rol(actor, "dueno")
    if metodo not in REEMBOLSOS:
        raise PagosError(f"Método de reembolso inválido: {metodo!r}")
    if not isinstance(monto_cents, int) or isinstance(monto_cents, bool) or monto_cents <= 0:
        raise PagosError("El monto debe ser un entero positivo (centavos)")
    moneda = REEMBOLSOS[metodo]
    ref = _ref(referencia)
    if metodo != "efectivo_usd" and metodo != "efectivo_bs" and len(re.sub(r"[^A-Z0-9]", "", ref)) < 4:
        raise PagosError("La referencia del reembolso es obligatoria (mínimo 4 caracteres)")
    with transaccion(con):
        d = con.execute("SELECT total_usd_cents, tasa_micro FROM devoluciones WHERE id=?", (devolucion_id,)).fetchone()
        if not d:
            raise PagosError("Devolución inexistente")
        equiv = _round_half_up(monto_cents * 1_000_000, d[1]) if moneda == "BS" else monto_cents
        pend = pendiente_reembolso(con, devolucion_id)
        if equiv > pend + TOLERANCIA_USD_CENTS:
            raise PagosError(f"El reembolso excede lo pendiente ({pend} centavos USD)")
        rid = con.execute("INSERT INTO devolucion_reembolsos(devolucion_id,metodo,moneda,monto_cents,equiv_usd_cents,referencia,usuario_id) "
                          "VALUES(?,?,?,?,?,?,?)", (devolucion_id, metodo, moneda, monto_cents, max(equiv, 1), ref or None, actor["id"])).lastrowid
        auditar(con, actor["id"], "reembolso", f"devolucion {devolucion_id}: {metodo} {monto_cents} {moneda}")
        return {"reembolso_id": rid, "pendiente_usd_cents": pendiente_reembolso(con, devolucion_id)}
