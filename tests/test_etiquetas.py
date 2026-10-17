import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.db import connect, migrate
from core import auth, code128, productos as P, inventario as I, pricing as PR, etiquetas as E
from core.etiquetas import EtiquetaError, FORMATOS

QL, R300, R203 = FORMATOS["QL800_62x100"], FORMATOS["ROLLO_40x30_300"], FORMATOS["ROLLO_40x30_203"]
IT = {"sku": "GAMO-FLOR-38-NEG", "marca": "Gamo", "modelo": "Sandalia Flor Ñandú", "talla": "38",
      "color": "negro", "precio_usd_cents": 1499}


# ---- decodificador independiente: lee píxeles, usa la tabla de reportlab (no la de core/code128) ----
def _tabla_ref():
    from reportlab.graphics.barcode.code128 import _patterns
    return {tuple(ord(c.lower()) - 96 for c in _patterns[i]): i for i in range(107)}


def decodificar_fila(bits):
    """bits: lista de 0/1 (1 = barra). Devuelve (texto, zona_blanca_izq_en_px, modulo_estimado) o lanza ValueError."""
    runs, prev, n = [], None, 0
    for b in bits:
        if b == prev: n += 1
        else:
            if prev is not None: runs.append((prev, n))
            prev, n = b, 1
    runs.append((prev, n))
    if runs[0][0] == 0:
        izq, runs = runs[0][1], runs[1:]
    else:
        izq = 0
    if runs and runs[-1][0] == 0:
        runs = runs[:-1]
    w = [r[1] for r in runs]
    if len(w) < 6 * 3 + 7 or (len(w) - 7) % 6:
        raise ValueError("estructura inválida")
    tabla, vals = _tabla_ref(), []
    for i in range(0, len(w) - 7, 6):
        ch = w[i:i + 6]; s = sum(ch)
        vals.append(tabla[tuple(max(1, round(x * 11 / s)) for x in ch)])
    st = w[-7:]; s = sum(st)
    if tuple(max(1, round(x * 13 / s)) for x in st) != (2, 3, 3, 1, 1, 1, 2):
        raise ValueError("stop inválido")
    if vals[0] != 104:
        raise ValueError("no es Code128-B")
    datos, chk = vals[1:-1], vals[-1]
    if (104 + sum(i * v for i, v in enumerate(datos, 1))) % 103 != chk:
        raise ValueError("checksum")
    mod = sum(w[:6]) / 11
    return "".join(chr(v + 32) for v in datos), izq, mod


def decodificar_imagen(img, desde_abajo=True):
    g = img.convert("L"); W, H = g.size
    px = g.load()
    for y in (range(H - 1, -1, -1) if desde_abajo else range(H)):
        try:
            return decodificar_fila([1 if px[x, y] < 128 else 0 for x in range(W)])
        except (ValueError, KeyError, IndexError):
            continue
    raise AssertionError("no se pudo decodificar ningún renglón")


class Code128(unittest.TestCase):
    def test_valores_conocidos(self):
        self.assertEqual(code128.valores("A"), [104, 33, 34, 106])     # checksum (104+33) % 103 = 34
        self.assertEqual(sum(code128.anchos("GAMO-FLOR-38-NEG")), code128.n_modulos("GAMO-FLOR-38-NEG"))

    def test_tabla_coincide_con_reportlab(self):
        ref = _tabla_ref()
        for v, pat in enumerate(code128.PATRONES):
            self.assertEqual(ref[tuple(int(c) for c in pat)], v)

    def test_caracteres_invalidos(self):
        for t in ("", "ñandú", "A\tB"):
            with self.assertRaises(code128.Code128Error):
                code128.valores(t)


class Render(unittest.TestCase):
    def test_png_se_decodifica_ql_y_rollo_300(self):
        for fmt in (QL, R300):
            img = E.render_png(IT, fmt)
            self.assertEqual(img.size, fmt.px)
            texto, izq, mod = decodificar_imagen(img)
            self.assertEqual(texto, IT["sku"])
            self.assertGreaterEqual(izq, 10 * mod)              # zona en blanco >= 10 módulos
            self.assertEqual(mod, int(mod))                      # módulo entero en píxeles (sin reescalar)
            self.assertGreaterEqual(mod, E.MIN_MODULO_PX)

    def test_png_monocromo_sigue_siendo_legible(self):
        img = E.render_png(IT, R300, monocromo=True)
        self.assertEqual(img.mode, "1")
        self.assertEqual(decodificar_imagen(img)[0], IT["sku"])

    def test_pdf_vectorial_se_decodifica_y_tamano_exacto(self):
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as d:
            for fmt in (QL, R300, R203):
                it = IT if fmt is not R203 else dict(IT, sku="GAMO-1-38")    # SKU corto para 203 dpi
                ruta = E.exportar_pdf([it, it], fmt, os.path.join(d, "x.pdf"))
                r = PdfReader(ruta)
                self.assertEqual(len(r.pages), 2)
                mb = r.pages[0].mediabox
                self.assertAlmostEqual(float(mb.width) / 72 * 25.4, fmt.papel_mm[0], delta=0.05)
                self.assertAlmostEqual(float(mb.height) / 72 * 25.4, fmt.papel_mm[1], delta=0.05)
                doc = pdfium.PdfDocument(ruta)
                img = doc[0].render(scale=600 / 72).to_pil()
                doc.close()
                self.assertEqual(decodificar_imagen(img)[0], it["sku"])

    def test_sku_largo_no_cabe_en_rollo_203_y_el_error_es_claro(self):
        with self.assertRaisesRegex(EtiquetaError, "máximo 9 caracteres"):
            E.render_png(IT, R203)
        img = E.render_png(dict(IT, sku="GAMO-1-38"), R203)             # 9 caracteres sí
        self.assertEqual(decodificar_imagen(img)[0], "GAMO-1-38")

    def test_modelo_largo_se_ajusta_sin_desbordar(self):
        it = dict(IT, modelo="Sandalia plataforma tejida con tiras cruzadas edición especial")
        lay = E.construir_layout(it, R300, {"formato": "ROLLO_40x30_300", "campos": ["marca", "modelo", "precio_usd"]})
        for t in lay["textos"]:
            ancho = E._ancho(t["texto"], lay["familia"], t["negrita"], t["size"])
            self.assertLessEqual(ancho, R300.area_mm[0] + 1e-6)
        self.assertTrue(any(t["texto"].endswith("…") or t["size"] < 2.6 for t in lay["textos"]))

    def test_etiqueta_por_defecto_es_ref_usd_mas_codigo_de_barras(self):
        for fmt in (QL, R300):
            lay = E.construir_layout(dict(IT, precio_usd_cents=5400), fmt)
            self.assertEqual([t["texto"] for t in lay["textos"]], ["Ref: 54$", IT["sku"]])
        self.assertEqual(E.fmt_ref(1499), "Ref: 14,99$")
        self.assertEqual(E.fmt_ref(123400), "Ref: 1.234$")
        self.assertEqual(E.fmt_ref(5), "Ref: 0,05$")
        con_iva = E.construir_layout(IT, R300, {"formato": "ROLLO_40x30_300", "leyenda_iva": True})
        self.assertEqual(con_iva["textos"][0]["texto"], "Ref: 14,99$ +IVA")

    def test_nada_se_sale_del_area(self):
        for fmt in (QL, R300):
            lay = E.construir_layout(IT, fmt)
            h = fmt.area_mm[1]
            for t in lay["textos"]:
                self.assertLessEqual(t["y"], h + 1e-6)
            b = lay["barcode"]
            self.assertLessEqual(b["y_mm"] + b["alto_mm"], h)

    def test_contenido_que_no_cabe_se_rechaza(self):
        cfg = {"formato": "ROLLO_40x30_300", "campos": ["marca", "modelo", "talla", "color", "precio_usd", "precio_bs"],
               "escala_texto": 2.0}
        with self.assertRaises(EtiquetaError):
            E.construir_layout(IT, R300, cfg, tasa_micro=40_500_000)

    def test_precio_bs_exige_tasa_y_formatea(self):
        cfg = {"formato": "QL800_62x100", "campos": ["marca", "precio_usd", "precio_bs"]}
        with self.assertRaisesRegex(EtiquetaError, "tasa"):
            E.construir_layout(IT, QL, cfg)
        lay = E.construir_layout(IT, QL, cfg, tasa_micro=40_500_000)
        self.assertIn("Bs 607,10", [t["texto"] for t in lay["textos"]])   # 14,99 x 40,50 = 607,095 -> 607,10 (half-up)
        self.assertEqual(E.fmt_usd(123456), "$1.234,56")

    def test_formato_de_dinero(self):
        self.assertEqual(E.fmt_usd(5), "$0,05")
        self.assertEqual(E.fmt_bs(469800), "Bs 4.698,00")


class Plantillas(unittest.TestCase):
    def test_validacion(self):
        base = {"formato": "QL800_62x100"}
        for malo in ({"campos": []}, {"campos": ["marca", "marca"]}, {"campos": ["foo"]}, {"fuente": "Comic"},
                     {"escala_texto": 5}, {"color_texto": "rojo"}, {"color_texto": "#ffff00"}, {"alineacion": "derecha"}):
            with self.assertRaises(EtiquetaError):
                E.validar_plantilla({**base, **malo})
        with self.assertRaises(EtiquetaError):
            E.validar_plantilla({"formato": "otro"})
        ok = E.validar_plantilla({**base, "color_acento": "#B00020", "fuente": "Poppins", "alineacion": "izquierda"})
        self.assertEqual(ok["fuente"], "Poppins")

    def test_tipografias_y_colores_se_aplican_y_siguen_escaneando(self):
        for fuente in E.FUENTES:
            cfg = {"formato": "ROLLO_40x30_300", "fuente": fuente, "color_acento": "#B00020", "alineacion": "izquierda"}
            img = E.render_png(IT, R300, cfg)
            self.assertEqual(decodificar_imagen(img)[0], IT["sku"])
            self.assertTrue(any(c[1][0] > 120 and c[1][1] < 60 for c in img.getcolors(maxcolors=100000)))      # hay píxeles rojizos


class ConBD(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.d.name, "e.db")); migrate(self.con)
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')")
        did = auth.crear_usuario(self.con, 1, "Marta", "dueno", "4821")
        self.dueno = {"id": did, "rol": "dueno"}
        cid = auth.crear_usuario(self.con, 1, "Luis", "cajero", "7305", actor=self.dueno)
        self.cajero = {"id": cid, "rol": "cajero"}
        p1 = P.crear_producto(self.con, "Sandalias", "Gamo", "Flor", "dama")
        p2 = P.crear_producto(self.con, "Botas", "Rs21", "Trek", "niño")
        self.v1 = P.crear_variantes(self.con, p1, ["37", "38"], ["negro", "rojo"], 1000)["creadas"]
        self.v2 = P.crear_variantes(self.con, p2, ["30"], ["marrón"], 2000)["creadas"]
        PR.actualizar_margen_categoria(self.con, self.dueno, 1, 4000)
        PR.actualizar_margen_categoria(self.con, self.dueno, 2, 5000)
        PR.aplicar_recalculo(self.con, self.dueno, PR.previsualizar_recalculo(self.con, modo=".99"), "inicial")

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def test_filtros(self):
        s = lambda **k: [i["sku"] for i in E.seleccionar_variantes(self.con, **k)]
        self.assertEqual(len(s()), 5)
        self.assertEqual(len(s(marca_id=1)), 4)
        self.assertEqual(len(s(talla="38")), 2)
        self.assertEqual(len(s(color="ROJO")), 2)
        self.assertEqual(s(genero="nino"), ["RS21-TREK-30-MAR"])
        self.assertEqual(len(s(categoria_id=2)), 1)
        self.assertEqual(s(skus=["rs21-trek-30-mar"]), ["RS21-TREK-30-MAR"])
        self.assertEqual(s(marca_id=1, talla="38", color="negro"), ["GAMO-FLOR-38-NEG"])
        with self.assertRaises(EtiquetaError):
            E.seleccionar_variantes(self.con, solo_con_stock=True)

    def test_copias_por_stock_y_solo_con_stock(self):
        I.registrar_movimiento(self.con, 1, self.v1[0], "compra", 3, "c")
        I.registrar_movimiento(self.con, 1, self.v2[0], "compra", 2, "c")
        I.registrar_movimiento(self.con, 1, self.v1[1], "venta", 1, "v")        # stock negativo: 0 etiquetas
        items = E.seleccionar_variantes(self.con, sucursal_id=1, solo_con_stock=True)
        self.assertEqual(len(items), 2)
        lote = E.armar_lote(self.con, E.seleccionar_variantes(self.con), copias="stock", sucursal_id=1)
        self.assertEqual(len(lote), 5)
        self.assertEqual(len(E.armar_lote(self.con, items, copias=2)), 4)
        self.assertEqual(len(E.armar_lote(self.con, items, copias={self.v1[0]: 7})), 7)
        with self.assertRaises(EtiquetaError):
            E.armar_lote(self.con, items, copias="stock")
        with self.assertRaises(EtiquetaError):
            E.armar_lote(self.con, items, copias=-1)

    def test_protecciones_de_lote(self):
        todo = E.seleccionar_variantes(self.con)
        with self.assertRaisesRegex(EtiquetaError, "máximo 10"):
            E.armar_lote(self.con, todo, copias=3, max_etiquetas=10)
        self.con.execute("UPDATE variantes SET precio_usd_cents=0 WHERE id=?", (self.v1[0],))
        with self.assertRaisesRegex(EtiquetaError, "sin precio"):
            E.armar_lote(self.con, E.seleccionar_variantes(self.con))
        self.assertEqual(len(E.armar_lote(self.con, E.seleccionar_variantes(self.con), permitir_sin_precio=True)), 5)

    def test_plantillas_guardadas_solo_dueno(self):
        cfg = {"formato": "ROLLO_40x30_300", "fuente": "Poppins"}
        with self.assertRaises(auth.AuthError):
            E.guardar_plantilla(self.con, self.cajero, "chica", cfg)
        E.guardar_plantilla(self.con, self.dueno, "chica", cfg)
        E.guardar_plantilla(self.con, self.dueno, "CHICA", {**cfg, "fuente": "DejaVu Condensada"})   # actualiza
        self.assertEqual(E.listar_plantillas(self.con), ["chica"])
        self.assertEqual(E.cargar_plantilla(self.con, "chica")["fuente"], "DejaVu Condensada")
        with self.assertRaises(EtiquetaError):
            E.cargar_plantilla(self.con, "nada")

    def test_lote_completo_a_pdf_y_pngs_escaneables(self):
        import pypdfium2 as pdfium
        I.registrar_movimiento(self.con, 1, self.v1[0], "compra", 2, "c")
        I.registrar_movimiento(self.con, 1, self.v2[0], "compra", 1, "c")
        lote = E.armar_lote(self.con, E.seleccionar_variantes(self.con, sucursal_id=1, solo_con_stock=True),
                            copias="stock", sucursal_id=1)
        self.assertEqual(len(lote), 3)
        pdf = E.exportar_pdf(lote, QL, os.path.join(self.d.name, "lote.pdf"))
        doc = pdfium.PdfDocument(pdf)
        self.assertEqual(len(doc), 3)
        leidos = [decodificar_imagen(doc[i].render(scale=600 / 72).to_pil())[0] for i in range(3)]
        doc.close()
        self.assertEqual(leidos, [i["sku"] for i in lote])
        rutas = E.exportar_pngs(lote, R300, os.path.join(self.d.name, "png"))
        from PIL import Image
        self.assertEqual([decodificar_imagen(Image.open(r))[0] for r in rutas], [i["sku"] for i in lote])
        with self.assertRaises(EtiquetaError):
            E.exportar_pdf([], QL, os.path.join(self.d.name, "v.pdf"))

    def test_calibracion_y_preview(self):
        r = E.guardar_preview(E.item_calibracion(), QL, os.path.join(self.d.name, "cal.png"))
        from PIL import Image
        im = Image.open(r)
        self.assertEqual(im.size, QL.px)
        self.assertEqual(decodificar_imagen(im)[0], "CAL-0001")

    def test_imprimir_fuera_de_windows_da_error_claro(self):
        if sys.platform != "win32":
            with self.assertRaisesRegex(EtiquetaError, "Windows"):
                E.imprimir_pdf("x.pdf", "Brother QL-800")


if __name__ == "__main__":
    unittest.main()
