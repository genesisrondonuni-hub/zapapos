import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.db import connect, migrate
from core import productos as P
from core.productos import ProductoError


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.d.name, "p.db")); migrate(self.con)
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')")

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def prod(self, modelo="Sandalia Ñandú", marca="Gamo"):
        return P.crear_producto(self.con, "Sandalias", marca, modelo, "Mujer")

    def test_matriz_talla_color_sin_colision(self):
        r = P.crear_variantes(self.con, self.prod(), ["37", "38", "39"], ["Negro", "Rojo"])
        self.assertEqual(len(r["creadas"]), 6)
        self.assertEqual(P.verificar_unicidad_sku(self.con), [])
        self.assertEqual(self.con.execute("SELECT COUNT(DISTINCT sku) FROM variantes").fetchone()[0], 6)

    def test_idempotente_y_color_sin_distinguir_mayusculas(self):
        pid = self.prod()
        P.crear_variantes(self.con, pid, ["38"], ["Negro"])
        r = P.crear_variantes(self.con, pid, ["38"], ["NEGRO"])
        self.assertEqual((len(r["creadas"]), r["existentes"]), (0, 1))

    def test_sku_sin_acentos_apto_code128(self):
        P.crear_variantes(self.con, self.prod(), ["38,5"], ["Azul Marino"])
        sku = self.con.execute("SELECT sku FROM variantes").fetchone()[0]
        self.assertEqual(sku, "GAMO-SANDAL-38P5-AZU")
        self.assertEqual(P.verificar_unicidad_sku(self.con), [])

    def test_modelos_con_mismo_prefijo_no_chocan(self):
        a = self.prod("Sandalia Alta"); b = self.prod("Sandalia Alba")
        P.crear_variantes(self.con, a, ["38"], ["negro"]); P.crear_variantes(self.con, b, ["38"], ["negro"])
        skus = [r[0] for r in self.con.execute("SELECT sku FROM variantes ORDER BY id")]
        self.assertEqual(len(set(skus)), 2)
        self.assertTrue(skus[1].endswith("-2"))

    def test_buscar_por_sku_y_texto(self):
        P.crear_variantes(self.con, self.prod(), ["38"], ["negro"])
        r = P.buscar_por_sku(self.con, " gamo-sandal-38-neg ")
        self.assertEqual((r["talla"], r["color"]), ("38", "negro"))
        self.assertIsNone(P.buscar_por_sku(self.con, "NOEXISTE"))
        self.assertEqual(len(P.buscar(self.con, "ñandú")), 1)

    def test_desactivar_oculta_no_borra(self):
        P.crear_variantes(self.con, self.prod(), ["38"], ["negro"])
        P.desactivar_variante(self.con, 1)
        self.assertIsNone(P.buscar_por_sku(self.con, "GAMO-SANDAL-38-NEG"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM variantes").fetchone()[0], 1)

    def test_genero_invalido(self):
        with self.assertRaises(ProductoError):
            P.crear_producto(self.con, "X", "Y", "Z", "robot")

    CSV = ("categoria;marca;modelo;genero;talla;color;costo_usd;cantidad\n"
           "Sandalias;Gamo;Flor;dama;37;negro;12,50;4\n"
           "Sandalias;Gamo;Flor;dama;38;negro;12.5;0\n"
           "Botas;Rs21;Trek;niño;30;marrón;$1.234,56;2\n")

    def test_importar_csv_ok_con_stock_inicial(self):
        r = P.importar_csv(self.con, self.CSV, 1)
        self.assertEqual((r["ok"], r["variantes_creadas"]), (True, 3))
        self.assertEqual(self.con.execute("SELECT costo_usd_cents FROM variantes WHERE talla='30'").fetchone()[0], 123456)
        self.assertEqual(self.con.execute("SELECT SUM(stock) FROM stock_actual").fetchone()[0], 6)
        r2 = P.importar_csv(self.con, self.CSV, 1)      # reimportar no duplica ni suma stock
        self.assertEqual((r2["variantes_creadas"], r2["variantes_existentes"]), (0, 3))
        self.assertEqual(self.con.execute("SELECT SUM(stock) FROM stock_actual").fetchone()[0], 6)

    def test_importar_csv_con_errores_no_escribe_nada(self):
        malo = self.CSV + "Botas;Rs21;Trek;alien;31;negro;10;1\nBotas;Rs21;Trek;niño;30;marrón;9;1\n"
        r = P.importar_csv(self.con, malo, 1)
        self.assertFalse(r["ok"])
        self.assertEqual([e[0] for e in r["errores"]], [5, 6])   # números de línea del archivo
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM variantes").fetchone()[0], 0)

    def test_importar_csv_columnas_faltantes(self):
        r = P.importar_csv(self.con, "marca,modelo\nA,B\n", 1)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
