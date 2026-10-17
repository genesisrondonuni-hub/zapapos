import os, subprocess, sys, tempfile, textwrap, unittest, sqlite3
from datetime import datetime, timedelta, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tools.db import connect, migrate
from core import auth, inventario as I, pricing as PR, productos as P, pos
from core.pos import PosError


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.d.name, "pos.db")
        self.con = connect(self.path); migrate(self.con)
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','Principal'),('S2','Segunda')")
        did = auth.crear_usuario(self.con, 1, "Marta", "dueno", "4821")
        self.dueno_row = {"id": did, "rol": "dueno"}
        auth.crear_usuario(self.con, 1, "Luis", "cajero", "7305", actor=self.dueno_row)
        auth.crear_usuario(self.con, 2, "Ana", "cajero", "5192", actor=self.dueno_row)
        self.dueno = auth.login(self.con, 1, "Marta", "4821")
        self.luis = auth.login(self.con, 1, "Luis", "7305")
        self.ana = auth.login(self.con, 2, "Ana", "5192")
        p1 = P.crear_producto(self.con, "Sandalias", "Gamo", "Flor", "dama")
        p2 = P.crear_producto(self.con, "Botas", "Rs21", "Trek", "niño")
        v = P.crear_variantes(self.con, p1, ["37", "38"], ["negro", "rojo"], 1000)["creadas"]
        self.n37, self.r37, self.n38, self.r38 = v    # orden: 37 negro, 37 rojo, 38 negro, 38 rojo
        self.trek = P.crear_variantes(self.con, p2, ["30"], ["marrón"], 2000)["creadas"][0]
        PR.actualizar_margen_categoria(self.con, self.dueno, 1, 4000)
        PR.actualizar_margen_categoria(self.con, self.dueno, 2, 5000)
        PR.registrar_tasa(self.con, "bcv", "40,50", self.dueno["id"])
        PR.aplicar_recalculo(self.con, self.dueno, PR.previsualizar_recalculo(self.con, modo=".99"), "precios iniciales")
        self.NEG, self.ROJ, self.TREK = "GAMO-FLOR-38-NEG", "GAMO-FLOR-38-ROJ", "RS21-TREK-30-MAR"

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def venta(self, usuario=None):
        return pos.abrir_venta(self.con, usuario or self.luis)["venta_id"]

    def pagar_todo(self, vid, metodo="efectivo_usd"):
        total = pos.ver_venta(self.con, vid)["pendiente_usd_cents"]      # paga lo que falte
        self.con.execute("INSERT INTO pagos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents) VALUES(?,?,?,?,?)",
                         (vid, metodo, "USD", total, total))

    # ---------- apertura ----------
    def test_abrir_guarda_snapshot_de_tasa_e_iva(self):
        r = pos.abrir_venta(self.con, self.luis)
        v = pos.ver_venta(self.con, r["venta_id"])["venta"]
        self.assertEqual((v["estado"], v["tasa_micro"], v["iva_bp"], v["iva_modo"]), ("abierta", 40_500_000, 1600, "sumado"))
        self.assertEqual(r["advertencias"], [])
        viejo = pos.abrir_venta(self.con, self.luis, ahora=datetime.now(timezone.utc) + timedelta(hours=30))
        self.assertTrue(any("horas" in a for a in viejo["advertencias"]))

    def test_sin_tasa_o_sin_iva_definido_no_abre(self):
        c = connect(os.path.join(self.d.name, "v.db")); migrate(c)
        c.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')")
        uid = auth.crear_usuario(c, 1, "Marta", "dueno", "4821")
        u = auth.login(c, 1, "Marta", "4821")
        with self.assertRaises(PR.PricingError):
            pos.abrir_venta(c, u)
        c.close()
        self.con.execute("UPDATE config SET valor='POR_DEFINIR' WHERE clave='iva_modo_default'")
        with self.assertRaisesRegex(PosError, "IVA"):
            pos.abrir_venta(self.con, self.luis)

    # ---------- escaneo ----------
    def test_escaneo_tolerante(self):
        vid = self.venta()
        for texto in (self.NEG, " gamo-flor-38-neg \n", "GAMOFLOR38NEG"):
            pos.escanear(self.con, vid, texto)
        lin = pos.ver_venta(self.con, vid)["lineas"]
        self.assertEqual([(l["sku"], l["cantidad"]) for l in lin], [(self.NEG, 3)])
        for malo in ("NOEXISTE", "", "   "):
            with self.assertRaises(PosError):
                pos.escanear(self.con, vid, malo)

    def test_codigo_ambiguo_y_variante_inactiva_y_sin_precio(self):
        self.con.execute("INSERT INTO variantes(producto_id,sku,talla,color) VALUES(1,'ZZ-A-B','X1','a')")
        self.con.execute("INSERT INTO variantes(producto_id,sku,talla,color) VALUES(1,'ZZA-B','X2','b')")
        vid = self.venta()
        with self.assertRaisesRegex(PosError, "ambiguo"):
            pos.escanear(self.con, vid, "ZZAB")
        with self.assertRaisesRegex(PosError, "no tiene precio"):
            pos.escanear(self.con, vid, "ZZ-A-B")
        P.desactivar_variante(self.con, self.n37)
        with self.assertRaisesRegex(PosError, "no encontrado"):
            pos.escanear(self.con, vid, "GAMO-FLOR-37-NEG")
        with self.assertRaises(PosError):
            pos.escanear(self.con, vid, self.NEG, cantidad=0)

    # ---------- carrito y totales ----------
    def test_totales_iva_sumado_y_bs(self):
        vid = self.venta()
        pos.escanear(self.con, vid, self.NEG); pos.escanear(self.con, vid, self.NEG); pos.escanear(self.con, vid, self.TREK)
        v = pos.ver_venta(self.con, vid)["venta"]       # 2 x 14,99 + 30,99
        self.assertEqual((v["subtotal_usd_cents"], v["iva_usd_cents"], v["total_usd_cents"], v["total_bs_centimos"]),
                         (6097, 976, 7073, 286457))

    def test_cambiar_cantidad_y_quitar(self):
        vid = self.venta()
        item = pos.escanear(self.con, vid, self.NEG)["item_id"]
        pos.cambiar_cantidad(self.con, vid, item, 4)
        self.assertEqual(pos.ver_venta(self.con, vid)["lineas"][0]["cantidad"], 4)
        with self.assertRaises(PosError):
            pos.cambiar_cantidad(self.con, vid, item, -1)
        otra = self.venta(); otro_item = pos.escanear(self.con, otra, self.ROJ)["item_id"]
        with self.assertRaisesRegex(PosError, "no pertenece"):
            pos.cambiar_cantidad(self.con, vid, otro_item, 1)
        t = pos.quitar_item(self.con, vid, item)
        self.assertEqual((t["total_usd_cents"], len(pos.ver_venta(self.con, vid)["lineas"])), (0, 0))

    def test_la_linea_conserva_el_precio_con_que_se_agrego(self):
        vid = self.venta()
        pos.escanear(self.con, vid, self.NEG)
        self.con.execute("UPDATE variantes SET precio_usd_cents=2000 WHERE sku=?", (self.NEG,))
        pos.escanear(self.con, vid, self.NEG)
        l = pos.ver_venta(self.con, vid)["lineas"][0]
        self.assertEqual((l["cantidad"], l["precio_unit_usd_cents"]), (2, 1499))

    def test_stock_insuficiente_advierte_pero_no_bloquea(self):
        vid = self.venta()
        r = pos.escanear(self.con, vid, self.NEG)
        self.assertTrue(r["advertencias"] and "insuficiente" in r["advertencias"][0])
        self.assertEqual(len(pos.ver_venta(self.con, vid)["lineas"]), 1)

    def test_iva_incluido(self):
        self.con.execute("UPDATE config SET valor='incluido' WHERE clave='iva_modo_default'")
        vid = self.venta(); pos.escanear(self.con, vid, self.NEG)
        v = pos.ver_venta(self.con, vid)["venta"]
        self.assertEqual((v["subtotal_usd_cents"], v["iva_usd_cents"], v["total_usd_cents"]), (1292, 207, 1499))

    # ---------- cierre ----------
    def test_cierre_exige_items_y_pago_completo_y_es_inmutable(self):
        I.registrar_movimiento(self.con, 1, self.n38, "compra", 5, "c")
        vid = self.venta()
        with self.assertRaisesRegex(PosError, "no tiene productos"):
            pos.cerrar_venta(self.con, vid)
        pos.escanear(self.con, vid, self.NEG, 2)
        with self.assertRaisesRegex(PosError, "Falta cobrar"):
            pos.cerrar_venta(self.con, vid)
        self.con.execute("INSERT INTO pagos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents) VALUES(?,?,?,?,?)", (vid, "zelle", "USD", 1000, 1000))
        with self.assertRaisesRegex(PosError, "Falta cobrar"):
            pos.cerrar_venta(self.con, vid)
        self.pagar_todo(vid)                     # el resto: pago dividido
        r = pos.cerrar_venta(self.con, vid, self.luis["id"])
        self.assertEqual((r["venta"]["estado"], r["pendiente_usd_cents"]), ("cerrada", 0))
        self.assertEqual(I.stock(self.con, 1, self.n38), 3)
        ob = self.con.execute("SELECT entidad, enviado_en FROM outbox WHERE uuid LIKE 'venta:%'").fetchall()
        self.assertEqual(ob, [("venta", None)])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM inventario_movimientos WHERE uuid LIKE ?", (r["venta"]["uuid"] + ":%",)).fetchone()[0], 1)
        for f in (lambda: pos.cerrar_venta(self.con, vid), lambda: pos.escanear(self.con, vid, self.ROJ),
                  lambda: pos.cambiar_cantidad(self.con, vid, 1, 9)):
            with self.assertRaises(PosError):
                f()
        for sql in ("UPDATE venta_items SET cantidad=9", "DELETE FROM venta_items", "UPDATE ventas SET total_usd_cents=1",
                    "UPDATE ventas SET estado='abierta'", "INSERT INTO pagos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents) VALUES(%d,'zelle','USD',1,1)" % vid,
                    "INSERT INTO venta_items(venta_id,variante_id,cantidad,precio_unit_usd_cents) VALUES(%d,1,1,1)" % vid):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.con.execute(sql)

    # ---------- escenario VE: corte_luz_mid_venta (flujo POS completo) ----------
    def test_corte_de_luz_durante_el_cierre_no_deja_venta_a_medias(self):
        I.registrar_movimiento(self.con, 1, self.n38, "compra", 5, "c"); I.registrar_movimiento(self.con, 1, self.r38, "compra", 5, "c")
        vid = self.venta()
        pos.escanear(self.con, vid, self.NEG, 2); pos.escanear(self.con, vid, self.ROJ, 1)
        self.pagar_todo(vid)
        codigo = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {ROOT!r})
            from tools.db import connect
            import core.inventario as I
            from core import pos
            orig = I.registrar_movimiento
            n = {{"c": 0}}
            def boom(*a, **k):
                r = orig(*a, **k); n["c"] += 1
                if n["c"] == 1: os._exit(1)      # se va la luz tras descontar la primera línea
                return r
            I.registrar_movimiento = boom
            pos.cerrar_venta(connect({self.path!r}), {vid})
        """)
        subprocess.run([sys.executable, "-c", codigo], check=False)
        c2 = connect(self.path)
        self.assertEqual(c2.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(c2.execute("SELECT estado FROM ventas WHERE id=?", (vid,)).fetchone()[0], "abierta")
        self.assertEqual(I.stock(c2, 1, self.n38), 5)           # nada descontado
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM outbox WHERE entidad='venta'").fetchone()[0], 0)
        self.assertEqual([x["id"] for x in pos.ventas_abiertas(c2, 1)], [vid])   # el carrito sigue ahí
        self.assertEqual(len(pos.ver_venta(c2, vid)["lineas"]), 2)
        pos.cerrar_venta(c2, vid)                               # al volver la luz se cierra bien, una sola vez
        self.assertEqual((I.stock(c2, 1, self.n38), I.stock(c2, 1, self.r38)), (3, 4))
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM outbox WHERE entidad='venta'").fetchone()[0], 1)
        c2.close()

    # ---------- escenario VE: cambio_tasa_mid_turno ----------
    def test_cambio_de_tasa_a_mitad_de_turno(self):
        a = self.venta(); pos.escanear(self.con, a, self.NEG)
        bs_a = pos.ver_venta(self.con, a)["venta"]["total_bs_centimos"]
        PR.registrar_tasa(self.con, "bcv", "42", self.dueno["id"])          # sube la tasa con A abierta
        self.assertEqual(pos.ver_venta(self.con, a)["venta"]["total_bs_centimos"], bs_a)   # A conserva la suya
        b = self.venta(); pos.escanear(self.con, b, self.NEG)
        self.assertEqual(pos.ver_venta(self.con, b)["venta"]["tasa_micro"], 42_000_000)
        pos.actualizar_tasa_venta(self.con, a)                              # el cajero la re-aplica a propósito
        va = pos.ver_venta(self.con, a)["venta"]
        self.assertEqual((va["tasa_micro"], va["total_usd_cents"]), (42_000_000, 1739))
        self.assertEqual(va["total_bs_centimos"], PR.usd_a_bs(1739, 42_000_000))
        self.assertGreater(va["total_bs_centimos"], bs_a)
        self.pagar_todo(b)
        with self.assertRaisesRegex(PosError, "pagos"):
            pos.actualizar_tasa_venta(self.con, b)
        pos.cerrar_venta(self.con, b)
        PR.registrar_tasa(self.con, "bcv", "45", self.dueno["id"])
        with self.assertRaises(PosError):
            pos.actualizar_tasa_venta(self.con, b)                          # una venta cerrada nunca cambia
        self.assertEqual(pos.ver_venta(self.con, b)["venta"]["tasa_micro"], 42_000_000)

    # ---------- escenario VE: devolucion_variante ----------
    def vender_cerrada(self):
        I.registrar_movimiento(self.con, 1, self.n38, "compra", 5, "c"); I.registrar_movimiento(self.con, 1, self.r38, "compra", 5, "c")
        vid = self.venta()
        pos.escanear(self.con, vid, self.NEG, 2); pos.escanear(self.con, vid, self.ROJ, 1)
        self.pagar_todo(vid)
        pos.cerrar_venta(self.con, vid)
        return vid

    def test_devolucion_regresa_a_la_misma_variante_y_cuadra_al_centavo(self):
        vid = self.vender_cerrada()
        self.assertEqual((I.stock(self.con, 1, self.n38), I.stock(self.con, 1, self.r38)), (3, 4))
        r1 = pos.devolver(self.con, self.dueno, vid, [(self.NEG, 1)], "talla incorrecta")
        self.assertEqual((I.stock(self.con, 1, self.n38), I.stock(self.con, 1, self.r38)), (4, 4))   # solo el negro
        self.assertEqual((r1["subtotal_usd_cents"], r1["iva_usd_cents"], r1["total_usd_cents"], r1["total_bs_centimos"], r1["completa"]),
                         (1499, 240, 1739, 70430, False))
        with self.assertRaisesRegex(PosError, "más de lo vendido"):
            pos.devolver(self.con, self.dueno, vid, [(self.NEG, 2)], "x")
        with self.assertRaisesRegex(PosError, "no fue parte"):
            pos.devolver(self.con, self.dueno, vid, [(self.TREK, 1)], "x")
        r2 = pos.devolver(self.con, self.dueno, vid, [(self.NEG, 1), (self.ROJ, 1)], "cliente desiste")
        self.assertTrue(r2["completa"])
        self.assertEqual((I.stock(self.con, 1, self.n38), I.stock(self.con, 1, self.r38)), (5, 5))
        v = pos.ver_venta(self.con, vid)["venta"]
        tot = self.con.execute("SELECT SUM(subtotal_usd_cents),SUM(iva_usd_cents),SUM(total_usd_cents),SUM(total_bs_centimos) FROM devoluciones").fetchone()
        self.assertEqual(tot, (v["subtotal_usd_cents"], v["iva_usd_cents"], v["total_usd_cents"], v["total_bs_centimos"]))
        with self.assertRaises(PosError):
            pos.devolver(self.con, self.dueno, vid, [(self.NEG, 1)], "otra vez")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox WHERE entidad='devolucion'").fetchone()[0], 2)

    def test_reglas_de_devolucion(self):
        vid = self.vender_cerrada()
        with self.assertRaises(auth.AuthError):
            pos.devolver(self.con, self.luis, vid, [(self.NEG, 1)], "x")        # el cajero no autoriza
        for args in (([(self.NEG, 1)], "  "), ([], "x"), ([(self.NEG, 0)], "x"), ([("NOEXISTE", 1)], "x")):
            with self.assertRaises(PosError):
                pos.devolver(self.con, self.dueno, vid, *args)
        abierta = self.venta(); pos.escanear(self.con, abierta, self.NEG)
        with self.assertRaisesRegex(PosError, "cerrada"):
            pos.devolver(self.con, self.dueno, abierta, [(self.NEG, 1)], "x")
        pos.devolver(self.con, self.dueno, vid, [(self.NEG, 1)], "devolución válida")
        for sql in ("DELETE FROM devoluciones", "UPDATE devoluciones SET total_usd_cents=0", "DELETE FROM devolucion_items"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.con.execute(sql)

    # ---------- anulación, recuperación, sucursales ----------
    def test_anular_carrito_abandonado(self):
        vid = self.venta(); pos.escanear(self.con, vid, self.NEG)
        with self.assertRaises(PosError):
            pos.anular_venta_abierta(self.con, self.luis, vid, "  ")
        pos.anular_venta_abierta(self.con, self.luis, vid, "cliente se fue")
        self.assertEqual(pos.ver_venta(self.con, vid)["venta"]["estado"], "anulada")
        with self.assertRaises(PosError):
            pos.escanear(self.con, vid, self.NEG)
        con_pago = self.venta(); pos.escanear(self.con, con_pago, self.NEG); self.pagar_todo(con_pago)
        with self.assertRaisesRegex(PosError, "pagos"):
            pos.anular_venta_abierta(self.con, self.luis, con_pago, "x")
        cerrada = self.vender_cerrada()
        with self.assertRaises(PosError):
            pos.anular_venta_abierta(self.con, self.dueno, cerrada, "x")       # cerrada: solo devolución

    def test_multi_sucursal_stock_y_carritos_separados(self):
        I.registrar_movimiento(self.con, 2, self.n38, "compra", 4, "c")
        a = pos.abrir_venta(self.con, self.ana)["venta_id"]; l = self.venta()
        pos.escanear(self.con, a, self.NEG, 2); self.pagar_todo(a)
        pos.cerrar_venta(self.con, a, self.ana["id"])
        self.assertEqual((I.stock(self.con, 2, self.n38), I.stock(self.con, 1, self.n38)), (2, 0))
        self.assertEqual([x["id"] for x in pos.ventas_abiertas(self.con, 1)], [l])
        self.assertEqual(pos.ventas_abiertas(self.con, 2), [])

    def test_ver_venta(self):
        vid = self.venta(); pos.escanear(self.con, vid, self.TREK)
        r = pos.ver_venta(self.con, vid)
        self.assertEqual(r["numero"], f"V-{vid:06d}")
        l = r["lineas"][0]
        self.assertEqual((l["marca"], l["modelo"], l["talla"], l["color"], l["importe_usd_cents"]), ("Rs21", "Trek", "30", "marrón", 3099))
        self.assertEqual(r["pendiente_usd_cents"], r["venta"]["total_usd_cents"])
        with self.assertRaises(PosError):
            pos.ver_venta(self.con, 9999)


if __name__ == "__main__":
    unittest.main()
