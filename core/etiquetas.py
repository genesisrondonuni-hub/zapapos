"""Etiquetas con código de barras Code128 (desde el SKU único).
Un mismo diseño (layout en mm) se dibuja como PNG (módulos en píxeles enteros, para escaneo fiable)
y como PDF vectorial (tamaño exacto de papel). Nunca se reescala un código de barras."""
import json, os, re, sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from core import code128
from core.auth import auditar, require_rol
from tools.db import transaccion

FUENTES_DIR = Path(__file__).resolve().parent.parent / "fonts"
FUENTES = {"DejaVu Sans": ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
           "DejaVu Condensada": ("DejaVuSansCondensed.ttf", "DejaVuSansCondensed-Bold.ttf"),
           "Poppins": ("Poppins-Regular.ttf", "Poppins-Bold.ttf")}
MIN_MODULO_PX = 2          # módulo mínimo fiable para lectores estándar
LINEA = 1.15


class EtiquetaError(Exception):
    pass


@dataclass(frozen=True)
class Formato:
    nombre: str
    papel_mm: tuple      # tamaño real del papel (página del PDF)
    dpi: int
    px: tuple            # área imprimible en píxeles a ese dpi

    @property
    def area_mm(self):
        return (self.px[0] * 25.4 / self.dpi, self.px[1] * 25.4 / self.dpi)


# QL-800 rollo DK de 62x100 mm: área imprimible 696x1109 puntos a 300 dpi (verificar con página de calibración).
FORMATOS = {
    "QL800_62x100": Formato("QL800_62x100", (62, 100), 300, (696, 1109)),
    "ROLLO_40x30_203": Formato("ROLLO_40x30_203", (40, 30), 203, (320, 240)),
    "ROLLO_40x30_300": Formato("ROLLO_40x30_300", (40, 30), 300, (472, 354)),
}

# campo -> (tamaño en mm a escala 1, negrita)
TAM = {"marca": (3.0, True), "modelo": (2.6, False), "talla": (5.0, True), "color": (2.8, False),
       "talla_color": (4.0, True), "precio_usd": (4.5, True), "precio_bs": (3.0, False)}
ACENTO = {"talla", "talla_color", "precio_usd"}


# ---------------- plantillas ----------------
def plantilla_defecto(formato: str) -> dict:
    """Etiqueta mínima: 'Ref: 54$' y el código de barras (con el SKU legible debajo)."""
    if formato not in FORMATOS:
        raise EtiquetaError(f"Formato desconocido: {formato!r}")
    return {"formato": formato, "campos": ["precio_usd"], "fuente": "DejaVu Sans", "escala_texto": 1.5,
            "color_texto": "#000000", "color_acento": "#000000", "leyenda_iva": False, "alineacion": "centro"}


def _luminancia(hexcol):
    r, g, b = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def validar_plantilla(cfg: dict) -> dict:
    base = plantilla_defecto(cfg.get("formato", ""))
    base.update(cfg)
    if not base["campos"] or any(c not in TAM for c in base["campos"]) or len(set(base["campos"])) != len(base["campos"]):
        raise EtiquetaError(f"Campos inválidos. Permitidos: {sorted(TAM)}")
    if base["fuente"] not in FUENTES:
        raise EtiquetaError(f"Fuente no disponible. Opciones: {sorted(FUENTES)}")
    if not isinstance(base["escala_texto"], (int, float)) or not 0.5 <= base["escala_texto"] <= 2.0:
        raise EtiquetaError("escala_texto debe estar entre 0.5 y 2.0")
    for k in ("color_texto", "color_acento"):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(base[k])):
            raise EtiquetaError(f"{k} debe ser un color #RRGGBB")
        if _luminancia(base[k]) > 0.6:
            raise EtiquetaError(f"{k} es demasiado claro: no se vería impreso")
    if base["alineacion"] not in ("centro", "izquierda"):
        raise EtiquetaError("alineacion debe ser 'centro' o 'izquierda'")
    base["leyenda_iva"] = bool(base["leyenda_iva"])
    return base


def guardar_plantilla(con, actor, nombre, cfg):
    require_rol(actor, "dueno")
    cfg = validar_plantilla(cfg)
    nombre = (nombre or "").strip()
    if not nombre:
        raise EtiquetaError("La plantilla necesita un nombre")
    with transaccion(con):
        con.execute("INSERT INTO plantillas_etiqueta(nombre,formato,config) VALUES(?,?,?) "
                    "ON CONFLICT(nombre) DO UPDATE SET formato=excluded.formato, config=excluded.config, "
                    "actualizada_en=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                    (nombre, cfg["formato"], json.dumps(cfg, ensure_ascii=False)))
        auditar(con, actor["id"], "plantilla_etiqueta", nombre)


def cargar_plantilla(con, nombre) -> dict:
    r = con.execute("SELECT config FROM plantillas_etiqueta WHERE nombre=?", (nombre,)).fetchone()
    if not r:
        raise EtiquetaError(f"No existe la plantilla {nombre!r}")
    return validar_plantilla(json.loads(r[0]))


def listar_plantillas(con):
    return [r[0] for r in con.execute("SELECT nombre FROM plantillas_etiqueta ORDER BY nombre")]


# ---------------- textos y medidas ----------------
def fmt_usd(cents):
    return "$" + f"{cents // 100:,}".replace(",", ".") + f",{cents % 100:02d}"


def fmt_ref(cents):
    """'Ref: 54$' si es entero; 'Ref: 14,99$' si tiene centavos."""
    ent = f"{cents // 100:,}".replace(",", ".")
    return f"Ref: {ent}$" if cents % 100 == 0 else f"Ref: {ent},{cents % 100:02d}$"


def fmt_bs(centimos):
    return "Bs " + f"{centimos // 100:,}".replace(",", ".") + f",{centimos % 100:02d}"


@lru_cache(maxsize=None)
def _ruta_fuente(familia, negrita):
    return str(FUENTES_DIR / FUENTES[familia][1 if negrita else 0])


@lru_cache(maxsize=None)
def _medidor(familia, negrita):
    return ImageFont.truetype(_ruta_fuente(familia, negrita), 100)


def _ancho(texto, familia, negrita, size_mm):
    return _medidor(familia, negrita).getlength(texto) / 100 * size_mm


def _texto_campo(campo, it, cfg, tasa_micro):
    if campo == "marca":
        return it["marca"]
    if campo == "modelo":
        return it["modelo"]
    if campo == "talla":
        return f"Talla {it['talla']}"
    if campo == "color":
        return it["color"].capitalize()
    if campo == "talla_color":
        return f"{it['talla']} · {it['color']}"
    if campo == "precio_usd":
        return fmt_ref(it["precio_usd_cents"]) + (" +IVA" if cfg["leyenda_iva"] else "")
    if campo == "precio_bs":
        if not tasa_micro:
            raise EtiquetaError("La plantilla muestra precio en Bs: falta la tasa")
        from core.pricing import usd_a_bs
        return fmt_bs(usd_a_bs(it["precio_usd_cents"], tasa_micro))
    raise EtiquetaError(f"Campo desconocido: {campo}")


def construir_layout(it, formato: Formato, cfg=None, tasa_micro=None) -> dict:
    cfg = validar_plantilla(cfg or plantilla_defecto(formato.nombre))
    if cfg["formato"] != formato.nombre:
        raise EtiquetaError("La plantilla es de otro formato de etiqueta")
    w, h = formato.area_mm
    base = min(w / 40, h / 30)
    margen = 1.5 * base
    fam = cfg["fuente"]
    # --- código de barras (primero: es lo que no puede fallar) ---
    sku = it["sku"]
    mods = code128.n_modulos(sku)
    mod_px = formato.px[0] // (mods + 20)           # 10 módulos de zona en blanco por lado
    if mod_px < MIN_MODULO_PX:
        max_chars = (formato.px[0] // MIN_MODULO_PX - 20 - 13) // 11 - 2
        raise EtiquetaError(f"El SKU {sku} ({len(sku)} caracteres) no cabe escaneable en {formato.nombre}; "
                            f"máximo {max_chars} caracteres o use un rollo de mayor resolución")
    mod_mm = mod_px * 25.4 / formato.dpi
    largo_mm = mods * mod_mm
    zona = 0.34 * h
    sku_size = 2.2 * base
    while _ancho(sku, fam, False, sku_size) > w - 2 * margen:
        sku_size *= 0.95
    barras_h = zona - sku_size * LINEA - 0.5 - margen
    if barras_h < max(5.0, 0.15 * largo_mm):
        raise EtiquetaError("No hay altura suficiente para un código de barras escaneable en este formato")
    # --- filas de texto ---
    esc = base * cfg["escala_texto"]
    filas = []
    for campo in cfg["campos"]:
        size, bold = TAM[campo]
        size *= esc
        txt = _texto_campo(campo, it, cfg, tasa_micro)
        s0, maxw = size, w - 2 * margen
        while _ancho(txt, fam, bold, size) > maxw and size > s0 * 0.5:
            size *= 0.95
        while _ancho(txt, fam, bold, size) > maxw and len(txt) > 1:
            txt = txt[:-2].rstrip() + "…"
        color = cfg["color_acento"] if campo in ACENTO else cfg["color_texto"]
        filas.append((txt, bold, size, color))
    alto_texto = sum(s * LINEA for _, _, s, _ in filas)
    disponible = h - zona - 2 * margen
    if alto_texto > disponible + 1e-9:
        raise EtiquetaError(f"El contenido no cabe en la etiqueta ({alto_texto:.1f} mm > {disponible:.1f} mm): "
                            "quite campos o baje escala_texto")
    y = margen + (disponible - alto_texto) / 2
    textos = []
    for txt, bold, s, color in filas:
        tw = _ancho(txt, fam, bold, s)
        x = (w - tw) / 2 if cfg["alineacion"] == "centro" else margen
        textos.append({"x": x, "y": y + s * 0.92, "texto": txt, "negrita": bold, "size": s, "color": color})
        y += s * LINEA
    top_barras = h - zona + margen * 0 
    x0_px = (formato.px[0] - mods * mod_px) // 2
    sku_x = (w - _ancho(sku, fam, False, sku_size)) / 2
    textos.append({"x": sku_x, "y": top_barras + barras_h + 0.5 + sku_size * 0.92, "texto": sku, "negrita": False,
                   "size": sku_size, "color": "#000000"})
    return {"textos": textos, "familia": fam,
            "barcode": {"anchos": code128.anchos(sku), "modulo_px": mod_px, "modulo_mm": mod_mm,
                        "x0_px": x0_px, "x0_mm": x0_px * 25.4 / formato.dpi, "y_mm": top_barras, "alto_mm": barras_h,
                        "zona_blanca_px": x0_px}}


# ---------------- PNG ----------------
def render_png(it, formato: Formato, cfg=None, tasa_micro=None, monocromo=False) -> Image.Image:
    lay = construir_layout(it, formato, cfg, tasa_micro)
    img = Image.new("RGB", formato.px, "white")
    d = ImageDraw.Draw(img)
    k = formato.dpi / 25.4
    for t in lay["textos"]:
        f = ImageFont.truetype(_ruta_fuente(lay["familia"], t["negrita"]), max(1, round(t["size"] * k)))
        d.text((round(t["x"] * k), round(t["y"] * k)), t["texto"], font=f, fill=t["color"], anchor="ls")
    b = lay["barcode"]
    y0, y1 = round(b["y_mm"] * k), round((b["y_mm"] + b["alto_mm"]) * k) - 1
    x = b["x0_px"]
    for i, ancho in enumerate(b["anchos"]):
        w = ancho * b["modulo_px"]
        if i % 2 == 0:
            d.rectangle([x, y0, x + w - 1, y1], fill="black")
        x += w
    if monocromo:    # para impresoras térmicas: sin tonos de gris
        img = img.convert("L").point(lambda p: 255 if p >= 150 else 0).convert("1")
    img.info["dpi"] = (formato.dpi, formato.dpi)
    return img


def guardar_preview(it, formato, ruta, cfg=None, tasa_micro=None):
    img = render_png(it, formato, cfg, tasa_micro)
    img.save(ruta, dpi=(formato.dpi, formato.dpi))
    return ruta


# ---------------- PDF vectorial ----------------
_registradas = set()


def _registrar_fuentes(familia):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for neg in (False, True):
        nombre = f"{familia}|{'B' if neg else 'R'}"
        if nombre not in _registradas:
            pdfmetrics.registerFont(TTFont(nombre, _ruta_fuente(familia, neg)))
            _registradas.add(nombre)


def exportar_pdf(items, formato: Formato, ruta, cfg=None, tasa_micro=None):
    """Una etiqueta por página, página = tamaño real del papel; el área imprimible queda centrada."""
    from reportlab.lib.colors import HexColor, black
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    if not items:
        raise EtiquetaError("No hay etiquetas para exportar")
    cfg = validar_plantilla(cfg or plantilla_defecto(formato.nombre))
    pw, ph = formato.papel_mm
    aw, ah = formato.area_mm
    ox, oy = (pw - aw) / 2, (ph - ah) / 2
    _registrar_fuentes(cfg["fuente"])
    c = canvas.Canvas(str(ruta), pagesize=(pw * mm, ph * mm))
    c.setTitle("Etiquetas")
    for it in items:
        lay = construir_layout(it, formato, cfg, tasa_micro)
        for t in lay["textos"]:
            c.setFillColor(HexColor(t["color"]))
            c.setFont(f"{lay['familia']}|{'B' if t['negrita'] else 'R'}", t["size"] * 72 / 25.4)
            c.drawString((ox + t["x"]) * mm, (ph - oy - t["y"]) * mm, t["texto"])
        b = lay["barcode"]
        c.setFillColor(black)
        x = ox + b["x0_mm"]
        ytop = oy + b["y_mm"]
        for i, ancho in enumerate(b["anchos"]):
            w = ancho * b["modulo_mm"]
            if i % 2 == 0:
                c.rect(x * mm, (ph - ytop - b["alto_mm"]) * mm, w * mm, b["alto_mm"] * mm, stroke=0, fill=1)
            x += w
        c.showPage()
    c.save()
    return str(ruta)


def exportar_pngs(items, formato, carpeta, cfg=None, tasa_micro=None):
    """PNG monocromo a resolución nativa (para imprimir con el programa de Brother u otro)."""
    os.makedirs(carpeta, exist_ok=True)
    rutas = []
    for n, it in enumerate(items, start=1):
        r = os.path.join(carpeta, f"{n:04d}_{it['sku']}.png")
        render_png(it, formato, cfg, tasa_micro, monocromo=True).save(r, dpi=(formato.dpi, formato.dpi))
        rutas.append(r)
    return rutas


def imprimir_pdf(ruta_pdf, impresora=None):
    """Envía el PDF a la cola de impresión de Windows (requiere un lector de PDF con verbo 'print'/'printto')."""
    if sys.platform != "win32":
        raise EtiquetaError("La impresión directa solo está disponible en Windows; use el PDF o los PNG")
    if impresora:
        os.startfile(ruta_pdf, "printto", f'"{impresora}"')
    else:
        os.startfile(ruta_pdf, "print")


def item_calibracion():
    return {"sku": "CAL-0001", "marca": "CALIBRACION", "modelo": "Prueba de impresion", "talla": "00",
            "color": "negro", "precio_usd_cents": 1234}


# ---------------- selección por lotes ----------------
def seleccionar_variantes(con, marca_id=None, categoria_id=None, genero=None, talla=None, color=None,
                          skus=None, sucursal_id=None, solo_con_stock=False):
    sql = ("SELECT v.id, v.sku, m.nombre, p.modelo, v.talla, v.color, v.precio_usd_cents "
           "FROM variantes v JOIN productos p ON p.id=v.producto_id JOIN marcas m ON m.id=p.marca_id "
           "WHERE v.activo=1 AND p.activo=1")
    args = []
    for col, val in (("p.marca_id", marca_id), ("p.categoria_id", categoria_id), ("p.genero", genero),
                     ("v.talla", talla.replace(",", ".") if talla else None),
                     ("v.color", color.strip().lower() if color else None)):
        if val is not None:
            sql += f" AND {col}=?"; args.append(val)
    if skus:
        sql += f" AND v.sku IN ({','.join('?' * len(skus))})"; args += [s.strip().upper() for s in skus]
    if solo_con_stock:
        if sucursal_id is None:
            raise EtiquetaError("solo_con_stock necesita sucursal_id")
        sql += " AND COALESCE((SELECT stock FROM stock_actual s WHERE s.variante_id=v.id AND s.sucursal_id=?),0) > 0"
        args.append(sucursal_id)
    cur = con.execute(sql + " ORDER BY m.nombre, p.modelo, v.talla, v.color", args)
    return [dict(zip(("variante_id", "sku", "marca", "modelo", "talla", "color", "precio_usd_cents"), r)) for r in cur]


def armar_lote(con, items, copias=1, sucursal_id=None, max_etiquetas=500, permitir_sin_precio=False):
    """copias: entero fijo, 'stock' (una por par en existencia) o dict {variante_id: n}."""
    sin_precio = [i["sku"] for i in items if i["precio_usd_cents"] <= 0]
    if sin_precio and not permitir_sin_precio:
        raise EtiquetaError(f"Hay {len(sin_precio)} variantes sin precio (ej. {sin_precio[0]}): "
                            "calcule los precios antes de imprimir")
    out = []
    for it in items:
        if copias == "stock":
            if sucursal_id is None:
                raise EtiquetaError("copias='stock' necesita sucursal_id")
            n = max(con.execute("SELECT COALESCE(SUM(cantidad),0) FROM inventario_movimientos "
                                "WHERE sucursal_id=? AND variante_id=?", (sucursal_id, it["variante_id"])).fetchone()[0], 0)
        elif isinstance(copias, dict):
            n = int(copias.get(it["variante_id"], 0))
        else:
            n = int(copias)
        if n < 0:
            raise EtiquetaError("Las copias no pueden ser negativas")
        out += [it] * n
    if len(out) > max_etiquetas:
        raise EtiquetaError(f"El lote tiene {len(out)} etiquetas (máximo {max_etiquetas}): "
                            "afine los filtros o suba max_etiquetas a propósito")
    return out
