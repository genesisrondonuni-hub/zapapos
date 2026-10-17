import csv, os, sqlite3, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.db import connect, migrate
from core import auth, inventario as I, pricing as PR, productos as P, pos, pagos as PG, reportes as R
from core.pagos import PagosError
from core.reportes import ReporteError

NEG, ROJ, TREK = "GAMO-FLOR-38-NEG", "GAMO-FLOR-38-ROJ", "RS21-TREK-30-MAR"
Z = lambda s: s + ":00.000Z"


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.d.name, "r.db")); migrate(self.con)
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','Principal'),('S2','Segunda')")
        did = auth.crear_usuario(self.con, 1, "Marta", "dueno", "4821")
        auth.crear_usuario(self.con, 1, "Luis", "cajero", "7305", actor={"id": did, "rol": "dueno"})
        self.dueno, self.luis = auth.login(self.con, 1, "Marta", "4821"), auth.login(self.con, 1, "Luis", "7305")
        p1 = P.crear_producto(self.con, "Sandalias", "Gamo", "Flor", "dama")
        p2 = P.crear_producto(self.con, "Botas", "Rs21", "Trek", "niño")
        P.crear_variantes(self.con, p1, ["38"], ["negro", "rojo"], 1000); P.crear_variantes(self.con, p2, ["30"], ["marrón"], 2000)
        PR.actualizar_margen_categoria(self.con, self.dueno, 1, 4000); PR.actualizar_margen_categoria(self.con, self.dueno, 2, 5000)
        PR.registrar_tasa(self.con, "bcv", "40,50", self.dueno["id"])
        PR.aplicar_recalculo(self.con, self.dueno, PR.previsualizar_recalculo(self.con, modo=".99"), "inicial")   # 14,99 / 14,99 / 30,99
        for sku in (NEG, ROJ, TREK):
            vid = self.con.execute("SELECT id FROM variantes WHERE sku=?", (sku,)).fetchone()[0]
            I.registrar_movimiento(self.con, 1, vid, "compra", 10, "stock inicial")

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def _sin_triggers(self, nombres, sqls):
        trig = dict(self.con.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'"))
        for n in nombres:
            self.con.execute(f"DROP TRIGGER {n}")
        try:
            for sql, args in sqls:
                self.con.execute(sql, args)
        finally:
            for n in nombres:
                self.con.execute(trig[n])

    def retro(self, venta_id, utc):               # mueve una venta y sus cobros a otra fecha (solo para pruebas)
        self._sin_triggers(["trg_venta_cerrada_inmutable", "trg_pago_update_limitado", "trg_vuelto_update_limitado"],
                           [("UPDATE ventas SET cerrada_en=?, creado_en=? WHERE id=?", (utc, utc, venta_id)),
                            ("UPDATE pagos SET creado_en=? WHERE venta_id=?", (utc, venta_id)),
                            ("UPDATE vueltos SET creado_en=? WHERE venta_id=?", (utc, venta_id))])

    def retro_dev(self, dev_id, utc):
        self._sin_triggers(["trg_dev_no_update"], [("UPDATE devoluciones SET creado_en=? WHERE id=?", (utc, dev_id))])

    def retro_remb(self, remb_id, utc):
        self._sin_triggers(["trg_remb_no_update"], [("UPDATE devolucion_reembolsos SET creado_en=? WHERE id=?", (utc, remb_id))])

    def carrito(self, usuario, items):
        vid = pos.abrir_venta(self.con, usuario)["venta_id"]
        for sku, n in items:
            pos.escanear(self.con, vid, sku, n)
        return vid

    def cobrar_usd_y_cerrar(self, vid, usuario):
        PG.registrar_pago(self.con, usuario, vid, "efectivo_usd", pos.ver_venta(self.con, vid)["pendiente_usd_cents"])
        pos.cerrar_venta(self.con, vid, usuario["id"])


class ConVentas(Base):
    """S1 (9-oct 11:00 local): 2 negras. S2 (9-oct 22:00 local): trek + roja por Pago Móvil. S3 (10-oct 01:00 local): 1 negra con vuelto en Bs.
    Devolución de 1 negra de S1 el 9-oct, reembolsada en efectivo USD."""
    def setUp(self):
        super().setUp()
        self.s1 = self.carrito(self.luis, [(NEG, 2)]); self.cobrar_usd_y_cerrar(self.s1, self.luis)
        self.retro(self.s1, Z("2026-10-09T15:00"))
        self.s2 = self.carrito(self.dueno, [(TREK, 1), (ROJ, 1)])
        pid = PG.registrar_pago(self.con, self.dueno, self.s2, "pago_movil", 216027, "PM000001", "0102")["pago_id"]
        PG.confirmar_pago(self.con, self.dueno, pid); pos.cerrar_venta(self.con, self.s2, self.dueno["id"])
        self.retro(self.s2, Z("2026-10-10T02:00"))
        self.s3 = self.carrito(self.luis, [(NEG, 1)])
        PG.registrar_pago(self.con, self.luis, self.s3, "efectivo_usd", 2000)
        PG.registrar_vuelto(self.con, self.luis, self.s3, "efectivo_bs", 10571)
        pos.cerrar_venta(self.con, self.s3, self.luis["id"]); self.retro(self.s3, Z("2026-10-10T05:00"))
        self.dev = pos.devolver(self.con, self.dueno, self.s1, [(NEG, 1)], "talla incorrecta")["devolucion_id"]
        self.retro_dev(self.dev, Z("2026-10-09T20:00"))
        self.remb = PG.registrar_reembolso(self.con, self.dueno, self.dev, "efectivo_usd", 1739)["reembolso_id"]
        self.retro_remb(self.remb, Z("2026-10-09T20:05"))

    # ---------- fechas ----------
    def test_rango_local_usa_utc_menos_4_y_es_configurable(self):
        self.assertEqual(R.rango_local(self.con, "2026-10-09"), ("2026-10-09T04:00:00.000Z", "2026-10-10T04:00:00.000Z"))
        self.assertEqual(R.rango_local(self.con, "2026-10-09", "2026-10-10")[1], "2026-10-11T04:00:00.000Z")
        self.con.execute("UPDATE config SET valor='0' WHERE clave='utc_offset_min'")
        self.assertEqual(R.rango_local(self.con, "2026-10-09")[0], "2026-10-09T00:00:00.000Z")
        for malo in (("no-fecha",), ("2026-10-10", "2026-10-09")):
            with self.assertRaises(ReporteError):
                R.rango_local(self.con, *malo)

    # ---------- ventas ----------
    def test_resumen_del_dia_respeta_la_hora_local(self):
        r = R.resumen_ventas(self.con, self.dueno, "2026-10-09")        # S2 cerró 22:00 hora local: cuenta el 9
        self.assertEqual(r["ventas"], {"n": 2, "subtotal_usd_cents": 7596, "iva_usd_cents": 1216, "total_usd_cents": 8812, "total_bs_centimos": 356886})
        self.assertEqual((r["devoluciones"]["n"], r["devoluciones"]["total_usd_cents"], r["devoluciones"]["total_bs_centimos"]), (1, 1739, 70430))
        self.assertEqual((r["neto_usd_cents"], r["neto_bs_centimos"], r["ticket_promedio_usd_cents"]), (7073, 286456, 4406))
        self.assertEqual([(u["usuario"], u["ventas"], u["total_usd_cents"]) for u in r["por_usuario"]], [("Marta", 1, 5334), ("Luis", 1, 3478)])
        r10 = R.resumen_ventas(self.con, self.dueno, "2026-10-10")      # S3 cerró 01:00 local del 10
        self.assertEqual((r10["ventas"]["n"], r10["ventas"]["total_usd_cents"], r10["devoluciones"]["n"]), (1, 1739, 0))
        self.assertEqual(R.resumen_ventas(self.con, self.dueno, "2026-10-09", "2026-10-10")["ventas"]["n"], 3)
        self.assertEqual(R.resumen_ventas(self.con, self.dueno, "2026-10-09", sucursal_id=2)["ventas"]["n"], 0)
        self.assertEqual(R.resumen_ventas(self.con, self.dueno, "2026-10-09", sucursal_id=1)["ventas"]["n"], 2)
        vacio = R.resumen_ventas(self.con, self.dueno, "2026-10-20")
        self.assertEqual((vacio["ventas"]["n"], vacio["ticket_promedio_usd_cents"], vacio["por_usuario"]), (0, 0, []))

    def test_ventas_por_dia_con_ceros(self):
        s = R.ventas_por_dia(self.con, self.dueno, "2026-10-09", "2026-10-11")
        self.assertEqual([(d["fecha"], d["ventas"], d["total_usd_cents"], d["devoluciones_usd_cents"], d["neto_usd_cents"]) for d in s],
                         [("2026-10-09", 2, 8812, 1739, 7073), ("2026-10-10", 1, 1739, 0, 1739), ("2026-10-11", 0, 0, 0, 0)])

    def test_ventas_abiertas_o_anuladas_no_cuentan(self):
        self.carrito(self.luis, [(NEG, 1)])
        anul = self.carrito(self.luis, [(NEG, 1)]); pos.anular_venta_abierta(self.con, self.luis, anul, "x")
        self.assertEqual(R.resumen_ventas(self.con, self.dueno, "2026-10-09", "2026-10-10")["ventas"]["n"], 3)   # sigue igual

    # ---------- cobros ----------
    def test_cobros_por_metodo_neto_de_vueltos_y_reembolsos(self):
        c = {(x["metodo"], x["moneda"]): x for x in R.cobros_del_dia(self.con, self.dueno, "2026-10-09")}
        self.assertEqual(set(c), {("efectivo_usd", "USD"), ("pago_movil", "BS")})
        e = c[("efectivo_usd", "USD")]
        self.assertEqual((e["cobrado_cents"], e["devuelto_cents"], e["neto_cents"], e["neto_equiv_usd_cents"]), (3478, 1739, 1739, 1739))
        p = c[("pago_movil", "BS")]
        self.assertEqual((p["neto_cents"], p["neto_equiv_usd_cents"]), (216027, 5334))
        c10 = {x["metodo"]: x for x in R.cobros_del_dia(self.con, self.dueno, "2026-10-10")}
        self.assertEqual(c10["efectivo_usd"]["neto_cents"], 2000)
        self.assertEqual((c10["efectivo_bs"]["neto_cents"], c10["efectivo_bs"]["neto_equiv_usd_cents"]), (-10571, -261))

    # ---------- margen y top ----------
    def test_margen_neto_de_devoluciones(self):
        m = R.margen_periodo(self.con, self.dueno, "2026-10-09")
        self.assertEqual((m["ingresos_base_usd_cents"], m["costo_usd_cents"], m["margen_usd_cents"], m["margen_bp"]), (6097, 4000, 2097, 3439))
        self.assertEqual(m["lineas_sin_costo"], 0)

    def test_el_costo_queda_congelado_en_la_venta(self):
        self.con.execute("UPDATE variantes SET costo_usd_cents=1800 WHERE sku=?", (NEG,))
        self.assertEqual(R.margen_periodo(self.con, self.dueno, "2026-10-09")["margen_usd_cents"], 2097)

    def test_lineas_sin_costo_se_reportan_aparte(self):
        v = self.carrito(self.luis, [(NEG, 1)])
        self.con.execute("UPDATE venta_items SET costo_unit_usd_cents=NULL WHERE venta_id=?", (v,))
        self.cobrar_usd_y_cerrar(v, self.luis); self.retro(v, Z("2026-10-09T16:00"))
        m = R.margen_periodo(self.con, self.dueno, "2026-10-09")
        self.assertEqual((m["margen_usd_cents"], m["lineas_sin_costo"], m["ingresos_sin_costo_usd_cents"]), (2097, 1, 1499))

    def test_margen_con_iva_incluido_usa_la_base(self):
        self.con.execute("UPDATE config SET valor='incluido' WHERE clave='iva_modo_default'")
        v = self.carrito(self.luis, [(NEG, 1)]); self.cobrar_usd_y_cerrar(v, self.luis); self.retro(v, Z("2026-10-11T16:00"))
        m = R.margen_periodo(self.con, self.dueno, "2026-10-11")
        self.assertEqual((m["ingresos_base_usd_cents"], m["margen_usd_cents"]), (1292, 292))      # 14,99 / 1,16 = 12,92

    def test_top_productos(self):
        t = R.top_productos(self.con, self.dueno, "2026-10-09", "2026-10-10")
        self.assertEqual([(x["clave"], x["unidades"], x["ingreso_base_usd_cents"], x["margen_usd_cents"]) for x in t],
                         [(NEG, 2, 2998, 998), (ROJ, 1, 1499, 499), (TREK, 1, 3099, 1099)])
        self.assertEqual([x["clave"] for x in R.top_productos(self.con, self.dueno, "2026-10-09", "2026-10-10", orden="ingreso")], [TREK, NEG, ROJ])
        self.assertEqual([x["clave"] for x in R.top_productos(self.con, self.dueno, "2026-10-09", "2026-10-10", orden="margen")], [TREK, NEG, ROJ])
        m = R.top_productos(self.con, self.dueno, "2026-10-09", "2026-10-10", por="modelo")
        self.assertEqual([(x["clave"], x["unidades"]) for x in m], [("Gamo Flor", 3), ("Rs21 Trek", 1)])
        self.assertEqual(len(R.top_productos(self.con, self.dueno, "2026-10-09", "2026-10-10", limite=1)), 1)
        for malo in ({"por": "color"}, {"orden": "peso"}):
            with self.assertRaises(ReporteError):
                R.top_productos(self.con, self.dueno, "2026-10-09", **malo)

    def test_los_reportes_con_costos_son_solo_del_dueno(self):
        for f in (lambda: R.resumen_ventas(self.con, self.luis, "2026-10-09"), lambda: R.margen_periodo(self.con, self.luis, "2026-10-09"),
                  lambda: R.top_productos(self.con, self.luis, "2026-10-09"), lambda: R.ventas_por_dia(self.con, self.luis, "2026-10-09", "2026-10-09"),
                  lambda: R.cobros_del_dia(self.con, self.luis, "2026-10-09"), lambda: R.sin_rotacion(self.con, self.luis, 1)):
            with self.assertRaises(auth.AuthError):
                f()

    # ---------- cierre de caja ----------
    def test_cierre_de_caja_dos_turnos_con_fondo_y_retiro(self):
        c1 = R.cerrar_caja(self.con, self.luis, 1, contado_usd_cents=1700, contado_bs_centimos=50000, retirado_usd_cents=1000,
                           fondo_bs_centimos=50000, ahora=datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc))
        self.assertEqual((c1["esperado_usd_cents"], c1["esperado_bs_centimos"]), (1739, 50000))     # 3478 cobrado - 1739 reembolso
        self.assertEqual((c1["diferencia_usd_cents"], c1["diferencia_bs_centimos"]), (-39, 0))      # faltante de $0,39
        self.assertEqual(c1["advertencias"], [])
        u = R.ultimo_cierre(self.con, 1)
        self.assertEqual((u["dejado_usd_cents"], u["dejado_bs_centimos"]), (700, 50000))
        c2 = R.cerrar_caja(self.con, self.luis, 1, 2700, 39429, ahora=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
        self.assertEqual((c2["esperado_usd_cents"], c2["esperado_bs_centimos"]), (2700, 39429))     # fondo 700 + 2000 ; 50000 - vuelto 10571
        self.assertEqual((c2["diferencia_usd_cents"], c2["diferencia_bs_centimos"]), (0, 0))
        self.assertEqual(c2["desde"], c1["hasta"])                                                    # sin huecos ni solapes
        with self.assertRaisesRegex(ReporteError, "posterior"):
            R.cerrar_caja(self.con, self.luis, 1, 0, 0, ahora=datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc))
        for sql in ("UPDATE cierres_caja SET contado_usd_cents=0", "DELETE FROM cierres_caja"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.con.execute(sql)

    def test_cierre_de_caja_validaciones(self):
        for args in ((-1, 0), (0, 1.5), (True, 0)):
            with self.assertRaises(ReporteError):
                R.cerrar_caja(self.con, self.luis, 1, *args)
        with self.assertRaisesRegex(ReporteError, "retirar más"):
            R.cerrar_caja(self.con, self.luis, 1, 100, 0, retirado_usd_cents=200)

    def test_cierre_de_caja_advierte_pendientes(self):
        # los cobros 'retro' llegan hasta el 10-oct: el cierre debe ser posterior a todo, sin depender del reloj real
        ya = max(datetime.now(timezone.utc), datetime(2026, 10, 11, tzinfo=timezone.utc))
        abierta = self.carrito(self.luis, [(NEG, 1)])
        PG.registrar_pago(self.con, self.luis, abierta, "efectivo_usd", 500)
        PG.registrar_pago(self.con, self.luis, abierta, "zelle", 500, "ZEL00001")
        v = self.carrito(self.luis, [(NEG, 1)]); self.cobrar_usd_y_cerrar(v, self.luis)
        pos.devolver(self.con, self.dueno, v, [(NEG, 1)], "defecto")                    # sin reembolso registrado
        c = R.cerrar_caja(self.con, self.luis, 1, 0, 0, fondo_usd_cents=0, fondo_bs_centimos=0, ahora=ya + timedelta(hours=1))
        self.assertEqual(len(c["advertencias"]), 3)
        self.assertTrue(any("abierta" in a for a in c["advertencias"]) and any("sin verificar" in a for a in c["advertencias"])
                        and any("reembolso" in a for a in c["advertencias"]))
        # el período cubre todo desde el último cierre (aquí, desde el inicio): S1 3478 - reembolso 1739 + S3 2000 + 500 + 1739
        self.assertEqual((c["esperado_usd_cents"], c["esperado_bs_centimos"]), (5978, -10571))

    # ---------- reembolsos ----------
    def test_reembolsos(self):
        self.assertEqual(PG.pendiente_reembolso(self.con, self.dev), 0)
        d2 = pos.devolver(self.con, self.dueno, self.s2, [(TREK, 1)], "no le gustó")["devolucion_id"]       # total 3595
        self.assertEqual(PG.pendiente_reembolso(self.con, d2), 3595)
        with self.assertRaises(auth.AuthError):
            PG.registrar_reembolso(self.con, self.luis, d2, "efectivo_usd", 100)
        for args in (("raro", 100, None), ("efectivo_usd", 0, None), ("efectivo_usd", True, None), ("zelle", 100, None), ("pago_movil", 100, "12")):
            with self.assertRaises(PagosError, msg=args):
                PG.registrar_reembolso(self.con, self.dueno, d2, *args)
        with self.assertRaisesRegex(PagosError, "excede"):
            PG.registrar_reembolso(self.con, self.dueno, d2, "efectivo_usd", 3597)
        self.assertEqual(PG.registrar_reembolso(self.con, self.dueno, d2, "zelle", 1000, "ZELLE-REF-1")["pendiente_usd_cents"], 2595)
        r = PG.registrar_reembolso(self.con, self.dueno, d2, "efectivo_bs", 105098)     # 2595 USD x 40,50 = Bs 1.050,98
        self.assertEqual(r["pendiente_usd_cents"], 0)
        with self.assertRaises(PagosError):
            PG.registrar_reembolso(self.con, self.dueno, 9999, "efectivo_usd", 100)
        with self.assertRaises(PagosError):
            PG.pendiente_reembolso(self.con, 9999)
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("DELETE FROM devolucion_reembolsos")

    # ---------- inventario ----------
    def test_stock_critico_y_sin_rotacion(self):
        rid = self.con.execute("SELECT id FROM variantes WHERE sku=?", (ROJ,)).fetchone()[0]
        I.establecer_stock_minimo(self.con, self.dueno, rid, 9)
        sc = R.stock_critico(self.con, 1)
        self.assertEqual([(a["sku"], a["stock"]) for a in sc["bajo_minimo"]], [(ROJ, 9)])
        self.assertEqual(sc["negativos"], [])
        self.assertEqual(sc["valor_inventario"]["pares"], 8 + 9 + 9)
        p = P.crear_producto(self.con, "Sandalias", "Gamo", "Nueva", "dama")
        nuevo = P.crear_variantes(self.con, p, ["36"], ["azul"], 1500)["creadas"][0]
        I.registrar_movimiento(self.con, 1, nuevo, "compra", 5, "c")
        cerca = R.sin_rotacion(self.con, self.dueno, 1, 60, ahora=datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual([(f["sku"], f["ultima_venta"]) for f in cerca], [("GAMO-NUEVA-36-AZU", None)])
        lejos = R.sin_rotacion(self.con, self.dueno, 1, 60, ahora=datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([(f["sku"], f["valor_costo_usd_cents"]) for f in lejos],
                         [(TREK, 18000), (ROJ, 9000), (NEG, 8000), ("GAMO-NUEVA-36-AZU", 7500)])   # capital detenido a costo

    # ---------- exportación ----------
    def test_exportar_csv_para_excel(self):
        filas = R.ventas_por_dia(self.con, self.dueno, "2026-10-09", "2026-10-10")
        ruta = R.exportar_csv(filas, os.path.join(self.d.name, "v.csv"), dinero=("total_usd_cents", "neto_usd_cents"))
        with open(ruta, "rb") as f:
            self.assertTrue(f.read().startswith(b"\xef\xbb\xbf"))                      # BOM: Excel respeta tildes
        with open(ruta, encoding="utf-8-sig") as f:
            filas_csv = list(csv.reader(f, delimiter=";"))
        self.assertEqual(filas_csv[0][:2], ["fecha", "ventas"])
        self.assertEqual((filas_csv[1][0], filas_csv[1][2], filas_csv[1][5]), ("2026-10-09", "88,12", "70,73"))
        with self.assertRaises(ReporteError):
            R.exportar_csv([], os.path.join(self.d.name, "x.csv"))


if __name__ == "__main__":
    unittest.main()
