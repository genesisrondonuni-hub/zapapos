import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.db import connect, migrate
from core import auth, pricing as PR, productos as P
from core.pricing import PricingError, calcular_precio


class Puro(unittest.TestCase):
    def test_modos_de_redondeo(self):
        self.assertEqual(calcular_precio(1000, 4000, "sin"), 1400)
        self.assertEqual(calcular_precio(1001, 4000, "sin"), 1401)       # 1401.4
        self.assertEqual(calcular_precio(1000, 4500, "entero"), 1500)    # 14.50 -> 15
        self.assertEqual(calcular_precio(1000, 4000, "entero"), 1400)    # exacto no sube
        self.assertEqual(calcular_precio(1000, 4500, ".50"), 1450)
        self.assertEqual(calcular_precio(1036, 4000, ".50"), 1500)       # 14.504 -> 15.00
        self.assertEqual(calcular_precio(1000, 4500, ".99"), 1499)
        self.assertEqual(calcular_precio(1000, 0, ".99"), 1099)          # 10.00 -> 10.99
        self.assertEqual(calcular_precio(1000, 4500, ".95"), 1495)
        self.assertEqual(calcular_precio(0, 4000, ".99"), 0)

    def test_redondeo_nunca_baja_del_margen(self):
        for costo in range(1, 3000, 37):
            for m in (0, 2500, 4000, 6666):
                num = costo * (10000 + m)
                for modo, (step, _) in ((k, v) for k, v in PR.MODOS.items() if v):
                    r = calcular_precio(costo, m, modo)
                    self.assertGreaterEqual(r * 10000, num, (costo, m, modo))
                    self.assertLess(r * 10000 - num, step * 10000 + 1, (costo, m, modo))
                self.assertLessEqual(abs(calcular_precio(costo, m, "sin") * 10000 - num), 5000)

    def test_entradas_invalidas(self):
        for args in ((1000, 4000, "raro"), (-1, 0, "sin"), (1000, -5, "sin")):
            with self.assertRaises(PricingError):
                calcular_precio(*args)

    def test_iva_sumado_e_incluido(self):
        self.assertEqual(PR.desglose_iva(10000, 1600, "sumado"), (10000, 1600, 11600))
        self.assertEqual(PR.desglose_iva(1005, 1600, "sumado"), (1005, 161, 1166))   # 160.8 -> 161
        self.assertEqual(PR.desglose_iva(11600, 1600, "incluido"), (10000, 1600, 11600))
        with self.assertRaises(PricingError):
            PR.desglose_iva(100, 1600, "raro")

    def test_totales_y_bs_con_tasa(self):
        items = [(5000, 2), (1999, 1)]
        t = PR.calcular_totales(items, 1600, "sumado", 40_500_000)
        self.assertEqual((t["subtotal_usd_cents"], t["iva_usd_cents"], t["total_usd_cents"]), (11999, 1920, 13919))
        self.assertEqual(t["total_bs_centimos"], 563720)            # 13919 x 40,50 = 563.719,50 -> half up
        self.assertEqual(PR.usd_a_bs(11600, 40_500_000), 469800)    # coincide con la prueba de la BD
        with self.assertRaises(PricingError):
            PR.calcular_totales(items, 1600, "sumado", 0)

    def test_cambio_de_tasa_cambia_bs_no_usd(self):
        a = PR.calcular_totales([(5000, 1)], 1600, "sumado", 40_500_000)
        b = PR.calcular_totales([(5000, 1)], 1600, "sumado", 45_000_000)
        self.assertEqual(a["total_usd_cents"], b["total_usd_cents"])
        self.assertNotEqual(a["total_bs_centimos"], b["total_bs_centimos"])

    def test_a_micro(self):
        self.assertEqual(PR.a_micro("40,5"), 40_500_000)
        self.assertEqual(PR.a_micro("40.123456"), 40_123_456)
        for x in ("0", "-3", "abc"):
            with self.assertRaises(PricingError):
                PR.a_micro(x)


class ConBD(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.d.name, "p.db")); migrate(self.con)
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')")
        did = auth.crear_usuario(self.con, 1, "Marta", "dueno", "4821")
        self.dueno = {"id": did, "rol": "dueno"}
        cid = auth.crear_usuario(self.con, 1, "Luis", "cajero", "7305", actor=self.dueno)
        self.cajero = {"id": cid, "rol": "cajero"}

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def variantes(self, marca="Gamo", modelo="Flor", genero="dama", costo=1000):
        pid = P.crear_producto(self.con, "Sandalias", marca, modelo, genero)
        return pid, P.crear_variantes(self.con, pid, ["37", "38"], ["negro"], costo)["creadas"]

    def test_tasas(self):
        with self.assertRaisesRegex(PricingError, "BCV"):
            PR.tasa_vigente(self.con)                       # default bcv pero sin registrar
        PR.registrar_tasa(self.con, "bcv", "40,50", self.dueno["id"], "2026-10-09T08:00:00.000Z")
        PR.registrar_tasa(self.con, "bcv", "41,00", self.dueno["id"], "2026-10-09T14:00:00.000Z")
        PR.registrar_tasa(self.con, "paralelo", "50", self.dueno["id"])
        self.assertEqual(PR.tasa_vigente(self.con)["tasa_micro"], 41_000_000)        # la más reciente, BCV por defecto
        self.assertEqual(PR.tasa_vigente(self.con, "paralelo")["tasa_micro"], 50_000_000)
        self.con.execute("UPDATE config SET valor='POR_DEFINIR' WHERE clave='tasa_default'")
        with self.assertRaises(PricingError):
            PR.tasa_vigente(self.con)
        with self.assertRaises(PricingError):
            PR.registrar_tasa(self.con, "dolartoday", "40")

    def test_preview_no_escribe_y_margen_producto_manda(self):
        _, a = self.variantes("Gamo", "Flor")
        PR.actualizar_margen_categoria(self.con, self.dueno, 1, 4000)
        pid2, _ = self.variantes("Rs21", "Trek")
        self.con.execute("UPDATE productos SET margen_bp=6000 WHERE id=?", (pid2,))
        pv = PR.previsualizar_recalculo(self.con, modo=".99")
        self.assertEqual((pv["total"], pv["cambian"]), (4, 4))
        precios = {f["sku"]: f["precio_nuevo"] for f in pv["filas"]}
        self.assertEqual(sorted(set(precios.values())), [1499, 1699])   # 14.00->14.99 ; 16.00->16.99 (15.99 quedaría bajo el margen)
        self.assertEqual(self.con.execute("SELECT SUM(precio_usd_cents) FROM variantes").fetchone()[0], 0)
        self.assertEqual(PR.previsualizar_recalculo(self.con, marca_id=1, modo="sin")["total"], 2)  # filtro por marca
        self.assertEqual(PR.previsualizar_recalculo(self.con, genero="caballero")["total"], 0)

    def test_aplicar_con_historial_permiso_y_preview_vencida(self):
        _, vs = self.variantes()
        PR.actualizar_margen_categoria(self.con, self.dueno, 1, 4000)
        PR.registrar_tasa(self.con, "bcv", "40,5", self.dueno["id"])
        pv = PR.previsualizar_recalculo(self.con, modo="entero")
        with self.assertRaises(auth.AuthError):
            PR.aplicar_recalculo(self.con, self.cajero, pv, "x")
        with self.assertRaises(PricingError):
            PR.aplicar_recalculo(self.con, self.dueno, pv, "  ")
        self.assertEqual(PR.aplicar_recalculo(self.con, self.dueno, pv, "ajuste semanal"), 2)
        self.assertEqual(self.con.execute("SELECT precio_usd_cents FROM variantes WHERE id=?", (vs[0],)).fetchone()[0], 1400)
        h = PR.historial_precios(self.con, vs[0])
        self.assertEqual((len(h), h[0]["precio_usd_cents"], h[0]["motivo"]), (1, 1400, "ajuste semanal"))
        self.assertIsNotNone(h[0]["tasa_id"])
        self.assertEqual(PR.aplicar_recalculo(self.con, self.dueno, PR.previsualizar_recalculo(self.con, modo="entero"), "repetir"), 0)
        # vista previa vencida: alguien cambia el precio entre previsualizar y aplicar
        pv2 = PR.previsualizar_recalculo(self.con, modo=".99")
        self.con.execute("UPDATE variantes SET precio_usd_cents=777 WHERE id=?", (vs[0],))
        with self.assertRaisesRegex(PricingError, "vuelva a previsualizar"):
            PR.aplicar_recalculo(self.con, self.dueno, pv2, "tarde")
        self.assertEqual(self.con.execute("SELECT precio_usd_cents FROM variantes WHERE id=?", (vs[1],)).fetchone()[0], 1400)  # todo o nada

    def test_advertencia_sin_costo_y_margen_invalido(self):
        self.variantes(costo=0)
        self.assertEqual(PR.previsualizar_recalculo(self.con)["filas"][0]["advertencia"], "sin costo")
        with self.assertRaises(PricingError):
            PR.actualizar_margen_categoria(self.con, self.dueno, 1, -1)
        with self.assertRaises(PricingError):
            PR.actualizar_margen_categoria(self.con, self.dueno, 99, 100)
        with self.assertRaises(auth.AuthError):
            PR.actualizar_margen_categoria(self.con, self.cajero, 1, 100)


if __name__ == "__main__":
    unittest.main()
