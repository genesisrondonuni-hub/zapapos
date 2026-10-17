"""Productos y variantes (talla x color). SKU único por variante, legible y compatible con Code128."""
import csv, io, re, unicodedata, uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from tools.db import transaccion

GENEROS = {
    "dama": "dama", "mujer": "dama", "damas": "dama",
    "caballero": "caballero", "hombre": "caballero", "caballeros": "caballero",
    "niño": "nino", "nino": "nino", "niños": "nino", "ninos": "nino",
    "niña": "nina", "nina": "nina", "niñas": "nina", "ninas": "nina",
    "unisex": "unisex",
}


class ProductoError(Exception):
    pass


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def _limpio(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _talla(s: str) -> str:
    return _limpio(s).replace(",", ".")


def generar_sku(con, marca, modelo, talla, color) -> str:
    t = _norm(_talla(talla).replace(".", "P"))
    base = f"{_norm(marca)[:4]}-{_norm(modelo)[:6]}-{t}-{_norm(color)[:3]}"
    sku, n = base, 1
    while con.execute("SELECT 1 FROM variantes WHERE sku=?", (sku,)).fetchone():
        n += 1
        sku = f"{base}-{n}"
    return sku


def _get_or_create(con, tabla, nombre):
    nombre = _limpio(nombre)
    if not nombre:
        raise ProductoError(f"{tabla[:-1]} vacía")
    r = con.execute(f"SELECT id FROM {tabla} WHERE nombre=? COLLATE NOCASE", (nombre,)).fetchone()
    return r[0] if r else con.execute(f"INSERT INTO {tabla}(nombre) VALUES(?)", (nombre,)).lastrowid


def crear_producto(con, categoria, marca, modelo, genero, margen_bp=None) -> int:
    g = GENEROS.get(_limpio(genero).casefold())
    if not g:
        raise ProductoError(f"Género no reconocido: {genero!r}")
    modelo = _limpio(modelo)
    if not modelo:
        raise ProductoError("El modelo es obligatorio")
    with transaccion(con):
        cid, mid = _get_or_create(con, "categorias", categoria), _get_or_create(con, "marcas", marca)
        r = con.execute("SELECT id FROM productos WHERE categoria_id=? AND marca_id=? AND modelo=? COLLATE NOCASE AND genero=?",
                        (cid, mid, modelo, g)).fetchone()
        if r:
            return r[0]
        return con.execute("INSERT INTO productos(categoria_id,marca_id,modelo,genero,margen_bp) VALUES(?,?,?,?,?)",
                           (cid, mid, modelo, g, margen_bp)).lastrowid


def crear_variantes(con, producto_id, tallas, colores, costo_usd_cents=0):
    """Matriz tallas x colores. Idempotente: no duplica ni cambia las que ya existen."""
    creadas, existentes = [], 0
    with transaccion(con):
        p = con.execute("SELECT m.nombre, p.modelo FROM productos p JOIN marcas m ON m.id=p.marca_id WHERE p.id=?",
                        (producto_id,)).fetchone()
        if not p:
            raise ProductoError("Producto inexistente")
        for t in tallas:
            for c in colores:
                t2, c2 = _talla(str(t)), _limpio(c).lower()
                if not t2 or not c2:
                    raise ProductoError("Talla y color son obligatorios")
                if con.execute("SELECT 1 FROM variantes WHERE producto_id=? AND talla=? AND color=?",
                               (producto_id, t2, c2)).fetchone():
                    existentes += 1
                    continue
                sku = generar_sku(con, p[0], p[1], t2, c2)
                vid = con.execute("INSERT INTO variantes(producto_id,sku,talla,color,costo_usd_cents) VALUES(?,?,?,?,?)",
                                  (producto_id, sku, t2, c2, costo_usd_cents)).lastrowid
                creadas.append(vid)
    return {"creadas": creadas, "existentes": existentes}


def buscar_por_sku(con, sku):
    """Búsqueda exacta para lector de código de barras."""
    r = con.execute(
        "SELECT v.id, v.sku, m.nombre, p.modelo, v.talla, v.color, v.precio_usd_cents, v.activo "
        "FROM variantes v JOIN productos p ON p.id=v.producto_id JOIN marcas m ON m.id=p.marca_id "
        "WHERE v.sku=? AND v.activo=1 AND p.activo=1", ((sku or "").strip().upper(),)).fetchone()
    if not r:
        return None
    return dict(zip(("variante_id", "sku", "marca", "modelo", "talla", "color", "precio_usd_cents", "activo"), r))


def _plano(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def buscar(con, texto, limite=30):
    """Búsqueda sin distinguir mayúsculas ni acentos (el LIKE de SQLite no maneja Ñ/á)."""
    q = _plano(_limpio(texto))
    cur = con.execute(
        "SELECT v.sku, m.nombre, p.modelo, v.talla, v.color FROM variantes v "
        "JOIN productos p ON p.id=v.producto_id JOIN marcas m ON m.id=p.marca_id "
        "WHERE v.activo=1 AND p.activo=1 ORDER BY m.nombre, p.modelo, v.talla")
    out = []
    for r in cur:
        if q in _plano(" ".join(r)):
            out.append(dict(zip(("sku", "marca", "modelo", "talla", "color"), r)))
            if len(out) >= limite:
                break
    return out


def desactivar_variante(con, variante_id):
    """Nunca se borra: se oculta (el kardex y las ventas históricas deben seguir resolviendo)."""
    con.execute("UPDATE variantes SET activo=0 WHERE id=?", (variante_id,))


def verificar_unicidad_sku(con) -> list:
    """Devuelve lista de problemas (vacía = OK)."""
    p = []
    for sku, n in con.execute("SELECT sku, COUNT(*) FROM variantes GROUP BY sku HAVING COUNT(*)>1"):
        p.append(f"SKU duplicado {sku} x{n}")
    for r in con.execute("SELECT sku FROM variantes"):
        if not re.fullmatch(r"[A-Z0-9\-]+", r[0]):
            p.append(f"SKU con caracteres no válidos para Code128: {r[0]}")
    return p


def _dinero_a_cents(txt):
    s = re.sub(r"[\s$]", "", txt or "")
    if "," in s and "." in s:           # 1.234,50 -> 1234.50
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ProductoError(f"Costo inválido: {txt!r}")
    if d < 0:
        raise ProductoError("El costo no puede ser negativo")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def importar_csv(con, texto, sucursal_id, usuario_id=None) -> dict:
    """Todo o nada. Columnas: categoria,marca,modelo,genero,talla,color,costo_usd[,cantidad]
    Acepta separador , ; o tab. Si hay errores no se escribe nada y se informan por línea."""
    texto = texto.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(texto.split("\n", 1)[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rd = csv.DictReader(io.StringIO(texto), dialect=dialect)
    rd.fieldnames = [(h or "").strip().lower() for h in (rd.fieldnames or [])]
    req = {"categoria", "marca", "modelo", "genero", "talla", "color"}
    falt = req - set(rd.fieldnames)
    if falt or not ({"costo_usd", "costo"} & set(rd.fieldnames)):
        return {"ok": False, "errores": [(1, f"Faltan columnas: {sorted(falt) or ['costo_usd']}")]}
    filas, errores, vistos = [], [], set()
    for i, f in enumerate(rd, start=2):
        try:
            g = GENEROS.get(_limpio(f["genero"]).casefold())
            if not g:
                raise ProductoError(f"Género no reconocido: {f['genero']!r}")
            for c in ("categoria", "marca", "modelo", "talla", "color"):
                if not _limpio(f[c]):
                    raise ProductoError(f"Falta {c}")
            costo = _dinero_a_cents(f.get("costo_usd") or f.get("costo"))
            cant = (f.get("cantidad") or "").strip()
            cant = int(cant) if cant else 0
            if cant < 0:
                raise ProductoError("Cantidad negativa")
            clave = (_limpio(f["marca"]).casefold(), _limpio(f["modelo"]).casefold(), g,
                     _talla(f["talla"]), _limpio(f["color"]).lower())
            if clave in vistos:
                raise ProductoError("Variante repetida en el archivo")
            vistos.add(clave)
            filas.append((f, costo, cant))
        except (ProductoError, ValueError) as e:
            errores.append((i, str(e) if isinstance(e, ProductoError) else "Cantidad inválida"))
    if errores:
        return {"ok": False, "errores": errores}
    nuevas = existentes = 0
    with transaccion(con):
        for f, costo, cant in filas:
            pid = crear_producto(con, f["categoria"], f["marca"], f["modelo"], f["genero"])
            r = crear_variantes(con, pid, [f["talla"]], [f["color"]], costo)
            if r["creadas"]:
                nuevas += 1
                if cant:
                    con.execute(
                        "INSERT INTO inventario_movimientos(uuid,sucursal_id,variante_id,tipo,cantidad,motivo,usuario_id) "
                        "VALUES(?,?,?,'compra',?,'carga inicial por CSV',?)",
                        (str(uuid.uuid4()), sucursal_id, r["creadas"][0], cant, usuario_id))
            else:
                existentes += 1
    return {"ok": True, "variantes_creadas": nuevas, "variantes_existentes": existentes, "errores": []}
