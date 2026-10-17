"""Ticket térmico de 58 mm (32 columnas) y comandos ESC/POS (impresión y cajón monedero).
Mientras no exista el módulo fiscal, el ticket dice DOCUMENTO SIN VALOR FISCAL. Imprimir nunca debe frenar una venta."""
import socket, textwrap
from datetime import datetime, timedelta
from core import pagos as PG, pos as POS

COLS = 32
RAYA = "-" * COLS
CODEPAGES = {"cp437": 0, "cp850": 2, "cp858": 19}     # valor de ESC t; en impresoras clon puede variar: configurable
CAJON = b"\x1bp\x00\x19\xfa"                            # ESC p 0 25 250: pulso al cajón monedero
ETIQUETA = {"pago_movil": "Pago Movil", "zelle": "Zelle", "usdt": "USDT", "binance_pay": "Binance Pay", "efectivo_bs": "Efectivo Bs",
            "efectivo_usd": "Efectivo USD", "punto_debito": "Punto Debito", "punto_credito": "Punto Credito"}


class TicketError(Exception):
    pass


def _cfg(con, clave, defecto=""):
    r = con.execute("SELECT valor FROM config WHERE clave=?", (clave,)).fetchone()
    return r[0] if r and r[0] else defecto


def _usd(c):
    return "$" + f"{c // 100:,}".replace(",", ".") + f",{c % 100:02d}"


def _bs(c):
    return "Bs " + f"{c // 100:,}".replace(",", ".") + f",{c % 100:02d}"


def _monto(moneda, c):
    return _bs(c) if moneda == "BS" else _usd(c) if moneda == "USD" else "USDT " + _usd(c)[1:]


def _tasa(t):
    ent, dec = divmod(t, 1_000_000)
    return f"{ent},{f'{dec:06d}'.rstrip('0').ljust(2, '0')}"


def _centrar(t):
    return t[:COLS].center(COLS).rstrip()


def _lr(izq, der):
    izq = str(izq)[:max(COLS - len(der) - 1, 0)]
    return izq + " " * (COLS - len(izq) - len(der)) + der


def lineas_ticket(con, venta_id, reimpresion=False) -> list:
    r = POS.ver_venta(con, venta_id)
    v = r["venta"]
    off = int(_cfg(con, "utc_offset_min", "-240"))
    dt = datetime.strptime((v["cerrada_en"] or v["creado_en"])[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(minutes=off)
    cajero = con.execute("SELECT nombre FROM usuarios WHERE id=?", (v["usuario_id"],)).fetchone()
    L = [_centrar(x) for x in textwrap.wrap(_cfg(con, "negocio_nombre", "SU ZAPATERIA"), COLS)]
    for clave, pref in (("negocio_rif", "RIF "), ("negocio_direccion", ""), ("negocio_telefono", "Tel ")):
        val = _cfg(con, clave)
        L += [_centrar(x) for x in textwrap.wrap(pref + val, COLS)] if val else []   # el RIF solo se imprime si está configurado
    L += [RAYA, _lr(f"Venta {r['numero']}", dt.strftime("%d/%m/%Y %H:%M")), f"Cajero: {cajero[0] if cajero else '-'}"]
    if reimpresion:
        L.append("*** REIMPRESION ***")
    L.append(RAYA)
    for l in r["lineas"]:
        L.append(f"{l['cantidad']} x {l['sku']}"[:COLS])
        L.append(_lr("  " + f"{l['marca']} {l['modelo']} {l['talla']} {l['color']}", _usd(l["importe_usd_cents"])))
    inc = v["iva_modo"] == "incluido"
    pct = f"{v['iva_bp'] // 100}%" if v["iva_bp"] % 100 == 0 else f"{v['iva_bp'] / 100:.2f}%"
    L += [RAYA, _lr("Subtotal", _usd(v["subtotal_usd_cents"])), _lr("IVA incluido " + pct if inc else "IVA " + pct, _usd(v["iva_usd_cents"])),
          _lr("TOTAL USD", _usd(v["total_usd_cents"])), _lr("TOTAL Bs", _bs(v["total_bs_centimos"])),
          _lr("Tasa", _tasa(v["tasa_micro"])), RAYA, "Pagos:"]
    for p in (x for x in PG.listar_pagos(con, venta_id) if not x["anulado"]):
        etiq = ETIQUETA[p["metodo"]] + (f" ({p['banco']})" if p["banco"] else "") + (f" ..{p['referencia'][-4:]}" if p["referencia"] else "")
        L.append(_lr(" " + etiq, _monto(p["moneda"], p["monto_cents"])))
    for vl in con.execute("SELECT metodo, moneda, monto_cents FROM vueltos WHERE venta_id=? AND anulado=0 ORDER BY id", (venta_id,)):
        L.append(_lr(" Vuelto " + ETIQUETA[vl[0]], _monto(vl[1], vl[2])))
    L += [RAYA, _centrar("DOCUMENTO SIN VALOR FISCAL")]
    pie = _cfg(con, "ticket_pie", "Gracias por su compra")
    return L + [_centrar(x) for x in textwrap.wrap(pie, COLS)]


def a_escpos(lineas, codepage="cp858", abrir_cajon=False, cortar=False) -> bytes:
    if codepage not in CODEPAGES:
        raise TicketError(f"Página de códigos no soportada: {codepage}")
    out = b"\x1b@" + b"\x1bt" + bytes([CODEPAGES[codepage]])
    out += ("\n".join(lineas) + "\n").encode(codepage, errors="replace") + b"\n\n\n"
    if cortar:
        out += b"\x1dV\x42\x00"
    return out + (CAJON if abrir_cajon else b"")


def enviar(datos: bytes, destino: str):
    """destino: 'archivo:RUTA' | 'red:IP:9100' | 'windows:NOMBRE' | 'serial:COM3'. (windows y serial: sin probar en este entorno)"""
    destino = (destino or "").strip()
    if not destino or destino == "ninguna":
        raise TicketError("No hay impresora de tickets configurada")
    tipo, _, arg = destino.partition(":")
    try:
        if tipo == "archivo":
            with open(arg, "ab") as f:
                f.write(datos)
        elif tipo == "red":
            host, _, puerto = arg.rpartition(":")
            with socket.create_connection((host, int(puerto)), timeout=3) as s:
                s.sendall(datos)
        elif tipo == "windows":
            try:
                import win32print
            except ImportError:
                raise TicketError("Falta el componente de impresión de Windows (pywin32)")
            h = win32print.OpenPrinter(arg)
            try:
                win32print.StartDocPrinter(h, 1, ("Ticket", None, "RAW"))
                win32print.StartPagePrinter(h)
                win32print.WritePrinter(h, datos)
                win32print.EndPagePrinter(h)
                win32print.EndDocPrinter(h)
            finally:
                win32print.ClosePrinter(h)
        elif tipo == "serial":
            try:
                import serial
            except ImportError:
                raise TicketError("Falta el componente de puerto serial (pyserial)")
            with serial.Serial(arg, 9600, timeout=3) as s:
                s.write(datos)
        else:
            raise TicketError(f"Destino de impresión desconocido: {tipo!r}")
    except (OSError, ValueError) as e:
        raise TicketError(f"No se pudo imprimir: {e}") from e


def imprimir_ticket(con, venta_id, reimpresion=False, abrir_cajon=None) -> dict:
    """Abre el cajón si hubo efectivo (config ticket_cajon=1). Lanza TicketError; quien llama decide (la venta ya está guardada)."""
    hubo_efectivo = any(p["metodo"].startswith("efectivo") and not p["anulado"] for p in PG.listar_pagos(con, venta_id))
    cajon = (_cfg(con, "ticket_cajon", "1") == "1" and hubo_efectivo and not reimpresion) if abrir_cajon is None else abrir_cajon
    datos = a_escpos(lineas_ticket(con, venta_id, reimpresion), _cfg(con, "ticket_codepage", "cp858"), cajon,
                     _cfg(con, "ticket_cortar", "0") == "1")
    enviar(datos, _cfg(con, "impresora_ticket"))
    return {"cajon": cajon}


def abrir_cajon(con):
    enviar(b"\x1b@" + CAJON, _cfg(con, "impresora_ticket"))
