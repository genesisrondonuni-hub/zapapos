import json, os, sqlite3, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.db import connect, migrate
from core import auth, pricing as PR, productos as P, pos, pagos as PG
from core.pagos import PagosError
from core.pos import PosError


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.d.name, "pg.db")); migrate(self.con)
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')")
        did = auth.crear_usuario(self.con, 1, "Marta", "dueno", "4821")
        auth.crear_usuario(self.con, 1, "Luis", "cajero", "7305", actor={"id": did, "rol": "dueno"})
        self.dueno, self.luis = auth.login(self.con, 1, "Marta", "4821"), auth.login(self.con, 1, "Luis", "7305")
        p1 = P.crear_producto(self.con, "Sandalias", "Gamo", "Flor", "dama")
        p2 = P.crear_producto(self.con, "Botas", "Rs21", "Trek", "niño")
        P.crear_variantes(self.con, p1, ["38"], ["negro"], 1000); P.crear_variantes(self.con, p2, ["30"], ["marrón"], 2000)
        PR.actualizar_margen_categoria(self.con, self.dueno, 1, 4000); PR.actualizar_margen_categoria(self.con, self.dueno, 2, 5000)
        PR.registrar_tasa(self.con, "bcv", "40,50", self.dueno["id"])
        PR.aplicar_recalculo(self.con, self.dueno, PR.previsualizar_recalculo(self.con, modo=".99"), "inicial")

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def carrito(self, usuario=None):          # 2 x 14,99 + 30,99 = 70,73 USD = Bs 2.864,57 a 40,50
        vid = pos.abrir_venta(self.con, usuario or self.luis)["venta_id"]
        pos.escanear(self.con, vid, "GAMO-FLOR-38-NEG", 2); pos.escanear(self.con, vid, "RS21-TREK-30-MAR")
        return vid

    def pagar(self, vid, metodo, monto, ref=None, banco=None, usuario=None):
        return PG.registrar_pago(self.con, usuario or self.luis, vid, metodo, monto, ref, banco)

    def resumen(self, vid):
        return PG.resumen_pagos(self.con, vid)

    # ---------- escenario VE: pago_dividido_40_60 ----------
    def test_pago_dividido_40_pago_movil_y_60_usd(self):
        vid = self.carrito()
        self.assertEqual(self.resumen(vid)["total_usd_cents"], 7073)
        usd40 = round(7073 * 0.4)                                        # 2829
        bs40 = PG.convertir_usd_a_bs(self.con, vid, usd40)               # 114.575 céntimos de Bs
        self.assertEqual((usd40, bs40), (2829, 114575))
        r = self.pagar(vid, "pago_movil", bs40, "12345678", "0102")
        self.assertEqual((r["equiv_usd_cents"], r["requiere_verificacion"], r["resumen"]["pendiente_usd_cents"]), (2829, True, 4244))
        r2 = self.pagar(vid, "efectivo_usd", 4244)
        self.assertEqual(r2["resumen"]["pendiente_usd_cents"], 0)
        with self.assertRaisesRegex(PosError, "confirmar"):               # falta verificar el Pago Móvil
            pos.cerrar_venta(self.con, vid)
        PG.confirmar_pago(self.con, self.luis, r["pago_id"])
        PG.confirmar_pago(self.con, self.luis, r["pago_id"])               # idempotente
        v = pos.cerrar_venta(self.con, vid, self.luis["id"])
        self.assertEqual(v["venta"]["estado"], "cerrada")
        carga = json.loads(self.con.execute("SELECT payload FROM outbox WHERE entidad='venta'").fetchone()[0])
        self.assertEqual([(p["metodo"], p["banco"]) for p in carga["pagos"]], [("pago_movil", "0102"), ("efectivo_usd", None)])
        self.assertEqual(carga["vueltos"], [])

    def test_pagar_exacto_en_bs_cierra_en_cero(self):
        a = self.carrito()
        self.assertEqual(self.pagar(a, "efectivo_bs", self.resumen(a)["pendiente_bs_centimos"])["resumen"]["pendiente_usd_cents"], 0)
        b = self.carrito()
        self.pagar(b, "zelle", 3000, "ZEL00001")
        r = self.pagar(b, "efectivo_bs", self.resumen(b)["pendiente_bs_centimos"])      # resto exacto en Bs
        self.assertEqual((r["equiv_usd_cents"], r["resumen"]["pendiente_usd_cents"]), (4073, 0))

    def test_los_bs_se_convierten_con_la_tasa_de_la_venta(self):
        vid = self.carrito()
        PR.registrar_tasa(self.con, "bcv", "50", self.dueno["id"])          # la tasa sube después de abrir
        r = self.pagar(vid, "efectivo_bs", 286457)
        self.assertEqual(r["resumen"]["pendiente_usd_cents"], 0)             # con 50 no habría cuadrado

    def test_usdt_y_binance_a_paridad_y_monedas(self):
        vid = self.carrito()
        self.pagar(vid, "usdt", 1000, "TX000001"); self.pagar(vid, "binance_pay", 1000, "BNB00001")
        self.pagar(vid, "punto_debito", 40500, "VOUCH001")
        m = {p["metodo"]: (p["moneda"], p["equiv_usd_cents"], p["verificado"]) for p in PG.listar_pagos(self.con, vid)}
        self.assertEqual(m, {"usdt": ("USDT", 1000, 0), "binance_pay": ("USDT", 1000, 0), "punto_debito": ("BS", 1000, 1)})

    # ---------- vuelto ----------
    def test_vuelto_en_bs_por_pago_en_usd(self):
        vid = self.carrito()
        r = self.pagar(vid, "efectivo_usd", 10000)
        self.assertEqual(r["vuelto_sugerido"], {"usd_cents": 2927, "bs_centimos": 118544})
        with self.assertRaisesRegex(PosError, "vuelto"):
            pos.cerrar_venta(self.con, vid)
        with self.assertRaisesRegex(PagosError, "excede"):
            PG.registrar_vuelto(self.con, self.luis, vid, "efectivo_usd", 3000)
        PG.registrar_vuelto(self.con, self.luis, vid, "efectivo_bs", 118544)
        self.assertEqual(self.resumen(vid)["exceso_usd_cents"], 0)
        pos.cerrar_venta(self.con, vid)
        carga = json.loads(self.con.execute("SELECT payload FROM outbox WHERE entidad='venta'").fetchone()[0])
        self.assertEqual([(x["metodo"], x["monto_cents"]) for x in carga["vueltos"]], [("efectivo_bs", 118544)])

    def test_vuelto_exacto_cuando_pagan_de_mas_en_bs(self):
        vid = self.carrito()
        r = self.pagar(vid, "efectivo_bs", 300000)
        self.assertEqual((r["equiv_usd_cents"], r["vuelto_sugerido"]), (7407, {"usd_cents": 334, "bs_centimos": 13543}))
        PG.registrar_vuelto(self.con, self.luis, vid, "efectivo_bs", 13543)
        self.assertEqual(self.resumen(vid)["neto_usd_cents"], 7073)
        pos.cerrar_venta(self.con, vid)

    def test_reglas_de_vuelto_y_anulacion(self):
        vid = self.carrito()
        with self.assertRaisesRegex(PagosError, "No hay vuelto"):
            PG.registrar_vuelto(self.con, self.luis, vid, "efectivo_usd", 100)
        self.pagar(vid, "efectivo_usd", 8000)
        for args in (("zelle", 100), ("efectivo_usd", 0), ("efectivo_usd", True)):
            with self.assertRaises(PagosError):
                PG.registrar_vuelto(self.con, self.luis, vid, *args)
        v = PG.registrar_vuelto(self.con, self.luis, vid, "efectivo_usd", 927)["vuelto_id"]
        with self.assertRaises(auth.AuthError):
            PG.anular_vuelto(self.con, self.luis, v, "error")
        with self.assertRaises(PagosError):
            PG.anular_vuelto(self.con, self.dueno, v, " ")
        PG.anular_vuelto(self.con, self.dueno, v, "monto mal digitado")
        self.assertEqual(self.resumen(vid)["exceso_usd_cents"], 927)
        with self.assertRaises(PagosError):
            PG.anular_vuelto(self.con, self.dueno, v, "otra vez")
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("UPDATE vueltos SET monto_cents=1")

    # ---------- validaciones ----------
    def test_validaciones_de_pago(self):
        vid = self.carrito()
        casos = [("raro", 100, "ABCD1234", None), ("zelle", 0, "ABCD1234", None), ("zelle", -5, "ABCD1234", None),
                 ("zelle", 10.5, "ABCD1234", None), ("zelle", True, "ABCD1234", None), ("zelle", 100, None, None),
                 ("zelle", 100, "12", None), ("pago_movil", 1000, "ABCD1234", None), ("pago_movil", 1000, "ABCD1234", "  "),
                 ("punto_credito", 1000, None, None)]
        for m, monto, ref, banco in casos:
            with self.assertRaises(PagosError, msg=(m, monto, ref, banco)):
                self.pagar(vid, m, monto, ref, banco)
        self.assertEqual(self.pagar(vid, "zelle", 100, "ab cd 1234", "ignorado")["equiv_usd_cents"], 100)
        p = PG.listar_pagos(self.con, vid)[0]
        self.assertEqual((p["referencia"], p["banco"]), ("ABCD1234", None))            # normaliza y descarta banco ajeno
        self.pagar(vid, "efectivo_bs", 1000)                                            # efectivo sin referencia

    def test_pago_electronico_no_puede_exceder_lo_pendiente_y_venta_cubierta(self):
        vid = self.carrito()
        with self.assertRaisesRegex(PagosError, "exceder"):
            self.pagar(vid, "zelle", 7074, "ZEL00002")
        with self.assertRaisesRegex(PagosError, "demasiado pequeño"):
            self.pagar(vid, "efectivo_bs", 1)
        self.pagar(vid, "efectivo_usd", 7073)
        with self.assertRaisesRegex(PagosError, "cubierta"):
            self.pagar(vid, "efectivo_usd", 100)

    # ---------- escenario VE: pago_movil_duplicado ----------
    def test_referencia_duplicada(self):
        a, b = self.carrito(), self.carrito()
        r = self.pagar(a, "pago_movil", 50000, "ABC12345", "0102")
        with self.assertRaisesRegex(PagosError, f"V-{a:06d}"):
            self.pagar(b, "pago_movil", 50000, "abc12345", "0102")                      # misma ref en otra venta
        self.assertTrue(self.pagar(b, "pago_movil", 50000, "ABC12345", "0105")["pago_id"])   # otro banco: permitido
        dup = self.pagar(a, "pago_movil", 50000, "ABC12345", "0102")                     # doble clic en la misma venta
        self.assertEqual((dup["duplicado"], dup["pago_id"]), (True, r["pago_id"]))
        self.assertEqual(len([p for p in PG.listar_pagos(self.con, a)]), 1)
        self.pagar(a, "zelle", 500, "ZEL77777")
        with self.assertRaises(PagosError):
            self.pagar(b, "zelle", 500, "zel77777")
        self.pagar(a, "punto_debito", 1000, "VOUCH123"); self.pagar(b, "punto_debito", 1000, "VOUCH123")   # los puntos reciclan números
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM pagos WHERE referencia='VOUCH123'").fetchone()[0], 2)

    def test_anular_pago_libera_la_referencia_y_deja_rastro(self):
        a, b = self.carrito(), self.carrito()
        p = self.pagar(a, "zelle", 1000, "ZEL00099")["pago_id"]
        with self.assertRaises(PagosError):
            PG.anular_pago(self.con, self.luis, p, "  ")
        PG.anular_pago(self.con, self.luis, p, "me equivoqué de monto")                   # sin verificar: el cajero puede
        self.assertEqual(self.resumen(a)["pagado_usd_cents"], 0)
        self.assertTrue(self.pagar(b, "zelle", 1000, "ZEL00099")["pago_id"])                # la referencia queda libre
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM pagos WHERE anulado=1").fetchone()[0], 1)   # rastro
        with self.assertRaises(PagosError):
            PG.anular_pago(self.con, self.luis, p, "x")
        with self.assertRaises(PagosError):
            PG.confirmar_pago(self.con, self.luis, p)
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("UPDATE pagos SET verificado=1 WHERE id=?", (p,))           # anulado es definitivo

    def test_permisos_para_anular(self):
        a = self.carrito()
        efec = self.pagar(a, "efectivo_usd", 500)["pago_id"]
        ver = self.pagar(a, "zelle", 500, "ZEL10001")["pago_id"]
        PG.confirmar_pago(self.con, self.luis, ver)
        for pid in (efec, ver):
            with self.assertRaises(auth.AuthError):
                PG.anular_pago(self.con, self.luis, pid, "x")
            PG.anular_pago(self.con, self.dueno, pid, "corrección autorizada")
        self.assertEqual(self.resumen(a)["pagado_usd_cents"], 0)

    # ---------- integridad ----------
    def test_pagos_cerrados_y_campos_monetarios_son_inmutables(self):
        a = self.carrito()
        p = self.pagar(a, "efectivo_usd", 7073)["pago_id"]
        for sql in ("UPDATE pagos SET monto_cents=1", "UPDATE pagos SET equiv_usd_cents=1", "UPDATE pagos SET referencia='X'", "DELETE FROM pagos"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.con.execute(sql)
        pos.cerrar_venta(self.con, a)
        for f in (lambda: self.pagar(a, "efectivo_usd", 1), lambda: PG.anular_pago(self.con, self.dueno, p, "x"),
                  lambda: PG.registrar_vuelto(self.con, self.luis, a, "efectivo_usd", 1)):
            with self.assertRaises(PagosError):
                f()
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("UPDATE pagos SET anulado=1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("INSERT INTO vueltos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents) VALUES(%d,'efectivo_usd','USD',1,1)" % a)

    def test_anulados_no_viajan_en_el_cierre(self):
        a = self.carrito()
        mal = self.pagar(a, "zelle", 999, "ZEL20001")["pago_id"]
        PG.anular_pago(self.con, self.luis, mal, "error")
        self.pagar(a, "efectivo_usd", 7073)
        pos.cerrar_venta(self.con, a)
        carga = json.loads(self.con.execute("SELECT payload FROM outbox WHERE entidad='venta'").fetchone()[0])
        self.assertEqual([p["metodo"] for p in carga["pagos"]], ["efectivo_usd"])

    def test_no_se_cambia_la_tasa_ni_se_anula_con_pagos_vigentes(self):
        a = self.carrito()
        p = self.pagar(a, "efectivo_usd", 100)["pago_id"]
        with self.assertRaisesRegex(PosError, "pagos"):
            pos.actualizar_tasa_venta(self.con, a)
        with self.assertRaisesRegex(PosError, "pagos"):
            pos.anular_venta_abierta(self.con, self.luis, a, "x")
        PG.anular_pago(self.con, self.dueno, p, "prueba")                                  # sin pagos vigentes ya se puede
        pos.anular_venta_abierta(self.con, self.luis, a, "cliente se fue")

    def test_a_cents_y_resumen(self):
        for txt, esperado in (("12,50", 1250), ("1.234,56", 123456), ("$ 12.5", 1250), ("Bs 1.000,00", 100000), (7, 700)):
            self.assertEqual(PG.a_cents(txt), esperado)
        for malo in ("0", "-3", "abc", ""):
            with self.assertRaises(PagosError):
                PG.a_cents(malo)
        r = self.resumen(self.carrito())
        self.assertEqual((r["pendiente_usd_cents"], r["pendiente_bs_centimos"], r["sin_verificar"]), (7073, 286457, []))
        with self.assertRaises(PagosError):
            PG.resumen_pagos(self.con, 9999)


if __name__ == "__main__":
    unittest.main()
