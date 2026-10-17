import os, sys, tempfile, unittest, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.db import connect, migrate
from core import auth, productos as P, inventario as I
from core.inventario import InventarioError


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.d.name, "i.db")); migrate(self.con)
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','Principal'),('S2','Segunda')")
        did = auth.crear_usuario(self.con, 1, "Marta", "dueno", "4821")
        self.dueno = {"id": did, "rol": "dueno"}
        cid = auth.crear_usuario(self.con, 1, "Luis", "cajero", "7305", actor=self.dueno)
        self.cajero = {"id": cid, "rol": "cajero"}
        pid = P.crear_producto(self.con, "Sandalias", "Gamo", "Flor", "dama")
        self.neg, self.rojo = P.crear_variantes(self.con, pid, ["38"], ["negro", "rojo"])["creadas"]

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def test_compra_y_stock(self):
        I.registrar_compra(self.con, self.dueno, 1, self.neg, 10)
        self.assertEqual(I.stock(self.con, 1, self.neg), 10)
        with self.assertRaises(auth.AuthError):
            I.registrar_compra(self.con, self.cajero, 1, self.neg, 1)

    def test_venta_nunca_se_bloquea_aunque_no_haya_stock(self):
        I.registrar_movimiento(self.con, 1, self.neg, "venta", 2, "venta #1")
        self.assertEqual(I.stock(self.con, 1, self.neg), -2)
        self.assertEqual([x["sku"] for x in I.stock_negativo(self.con, 1)], ["GAMO-FLOR-38-NEG"])

    def test_salidas_manuales_exigen_stock(self):
        I.registrar_movimiento(self.con, 1, self.neg, "compra", 3, "c")
        with self.assertRaises(InventarioError):
            I.registrar_movimiento(self.con, 1, self.neg, "ajuste_neg", 4, "merma")
        I.registrar_movimiento(self.con, 1, self.neg, "ajuste_neg", 3, "merma")
        self.assertEqual(I.stock(self.con, 1, self.neg), 0)

    def test_validaciones(self):
        for cant in (0, -1, 1.5, True):
            with self.assertRaises(InventarioError):
                I.registrar_movimiento(self.con, 1, self.neg, "compra", cant, "x")
        with self.assertRaises(InventarioError):
            I.registrar_movimiento(self.con, 1, self.neg, "compra", 1, "  ")
        with self.assertRaises(InventarioError):
            I.registrar_movimiento(self.con, 1, self.neg, "reversa", 1, "x")
        with self.assertRaises(InventarioError):
            I.registrar_movimiento(self.con, 1, 999, "compra", 1, "x")

    def test_idempotencia_por_uuid_para_sync(self):
        a = I.registrar_movimiento(self.con, 1, self.neg, "compra", 5, "c", uuid_="abc")
        b = I.registrar_movimiento(self.con, 1, self.neg, "compra", 5, "c", uuid_="abc")
        self.assertEqual(a, b)
        self.assertEqual(I.stock(self.con, 1, self.neg), 5)

    def test_reversa_contra_asiento(self):
        m = I.registrar_movimiento(self.con, 1, self.neg, "compra", 5, "error de carga")
        r = I.reversar_movimiento(self.con, self.dueno, m, "se cargó dos veces")
        self.assertEqual(I.stock(self.con, 1, self.neg), 0)
        with self.assertRaisesRegex(InventarioError, "ya fue reversado"):
            I.reversar_movimiento(self.con, self.dueno, m, "otra vez")
        with self.assertRaisesRegex(InventarioError, "no se puede reversar"):
            I.reversar_movimiento(self.con, self.dueno, r, "x")
        with self.assertRaises(auth.AuthError):
            I.reversar_movimiento(self.con, self.cajero, m, "x")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM inventario_movimientos").fetchone()[0], 2)  # nada se borró

    def test_reversar_compra_sin_stock_suficiente(self):
        m = I.registrar_movimiento(self.con, 1, self.neg, "compra", 5, "c")
        I.registrar_movimiento(self.con, 1, self.neg, "venta", 4, "v")
        with self.assertRaises(InventarioError):
            I.reversar_movimiento(self.con, self.dueno, m, "x")
        v = I.registrar_movimiento(self.con, 1, self.neg, "venta", 1, "v2")   # reversar una venta sí devuelve stock
        I.reversar_movimiento(self.con, self.dueno, v, "venta anulada")
        self.assertEqual(I.stock(self.con, 1, self.neg), 1)

    def test_ajuste_a_conteo_fisico(self):
        I.registrar_movimiento(self.con, 1, self.neg, "compra", 10, "c")
        self.assertIsNone(I.ajustar_a_conteo(self.con, self.dueno, 1, self.neg, 10, "conteo"))
        I.ajustar_a_conteo(self.con, self.dueno, 1, self.neg, 7, "faltan 3")
        self.assertEqual(I.stock(self.con, 1, self.neg), 7)
        I.registrar_movimiento(self.con, 1, self.rojo, "venta", 2, "v")          # stock -2
        I.ajustar_a_conteo(self.con, self.dueno, 1, self.rojo, 0, "se vendió lo que no estaba cargado")
        self.assertEqual(I.stock(self.con, 1, self.rojo), 0)
        with self.assertRaises(auth.AuthError):
            I.ajustar_a_conteo(self.con, self.cajero, 1, self.neg, 1, "x")
        with self.assertRaises(InventarioError):
            I.ajustar_a_conteo(self.con, self.dueno, 1, self.neg, -1, "x")

    def test_kardex_con_saldo_acumulado(self):
        I.registrar_movimiento(self.con, 1, self.neg, "compra", 10, "c")
        I.registrar_movimiento(self.con, 1, self.neg, "venta", 3, "v")
        I.registrar_movimiento(self.con, 1, self.neg, "devolucion", 1, "d")
        self.assertEqual([k["saldo"] for k in I.kardex(self.con, 1, self.neg)], [10, 7, 8])

    def test_devolucion_regresa_a_la_misma_variante(self):
        I.registrar_movimiento(self.con, 1, self.neg, "compra", 2, "c"); I.registrar_movimiento(self.con, 1, self.rojo, "compra", 2, "c")
        I.registrar_movimiento(self.con, 1, self.neg, "venta", 1, "v")
        I.registrar_movimiento(self.con, 1, self.neg, "devolucion", 1, "devolución de venta")
        self.assertEqual((I.stock(self.con, 1, self.neg), I.stock(self.con, 1, self.rojo)), (2, 2))

    def test_alertas_incluyen_variantes_sin_movimientos(self):
        I.establecer_stock_minimo(self.con, self.dueno, self.neg, 3)
        I.establecer_stock_minimo(self.con, self.dueno, self.rojo, 2)
        I.registrar_movimiento(self.con, 1, self.rojo, "compra", 5, "c")
        al = I.alertas_stock_minimo(self.con, 1)
        self.assertEqual([(a["sku"], a["stock"]) for a in al], [("GAMO-FLOR-38-NEG", 0)])
        I.registrar_movimiento(self.con, 1, self.rojo, "venta", 3, "v")        # baja a 2 = mínimo
        self.assertEqual(len(I.alertas_stock_minimo(self.con, 1)), 2)
        with self.assertRaises(auth.AuthError):
            I.establecer_stock_minimo(self.con, self.cajero, self.neg, 1)
        with self.assertRaises(InventarioError):
            I.establecer_stock_minimo(self.con, self.dueno, 999, 1)

    def test_traslado_entre_sucursales_atomico(self):
        I.registrar_movimiento(self.con, 1, self.neg, "compra", 5, "c")
        ref = I.trasladar(self.con, self.dueno, 1, 2, self.neg, 2, "reposición S2")
        self.assertEqual((I.stock(self.con, 1, self.neg), I.stock(self.con, 2, self.neg)), (3, 2))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM inventario_movimientos WHERE referencia=?", (ref,)).fetchone()[0], 2)
        with self.assertRaises(InventarioError):
            I.trasladar(self.con, self.dueno, 1, 2, self.neg, 99, "x")
        self.assertEqual((I.stock(self.con, 1, self.neg), I.stock(self.con, 2, self.neg)), (3, 2))  # sin cambios
        with self.assertRaises(InventarioError):
            I.trasladar(self.con, self.dueno, 1, 1, self.neg, 1, "x")

    def test_resumen_valorizado(self):
        self.con.execute("UPDATE variantes SET costo_usd_cents=1250 WHERE id=?", (self.neg,))
        I.registrar_movimiento(self.con, 1, self.neg, "compra", 4, "c")
        self.assertEqual(I.resumen_stock(self.con, 1), {"pares": 4, "valor_costo_usd_cents": 5000})


if __name__ == "__main__":
    unittest.main()
