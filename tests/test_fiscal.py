import http.client, json, os, sqlite3, sys, threading, unittest
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.server import crear_servidor
from core import auth, fiscal as F, negocio as N, pos, reportes as R
from core.fiscal import Adaptador, FiscalError
from tests.test_reportes import Base, NEG, ROJ

Z = lambda *a: datetime(*a, tzinfo=timezone.utc)


class Real(Adaptador):
    """Proveedor de prueba que NO es simulador: sus documentos sí entran al libro."""
    nombre = "real_prueba"

    def __init__(self, fechas=None, falla=0, repetir=None):
        self.n, self.fechas, self.falla, self.repetir = 0, list(fechas or []), falla, repetir

    def emitir(self, doc, afectada=None):
        if self.falla:
            self.falla -= 1; raise ConnectionError("sin conexión con el proveedor")
        self.n += 1
        return {"numero": self.repetir or f"00-{self.n:06d}", "numero_control": f"CTL-{self.n:06d}",
                "fecha_emision": self.fechas.pop(0) if self.fechas else Z(2026, 10, 10, 15), "respuesta": {"ok": True, "afectada": (afectada or {}).get("numero")}}


class Preparado(Base):
    def setUp(self):
        super().setUp()
        self.con.execute("UPDATE config SET valor='1' WHERE clave='fiscal_activo'")

    def vender(self, items=((NEG, 1),), cliente=None, usuario=None):
        usuario = usuario or self.luis
        vid = pos.abrir_venta(self.con, usuario)["venta_id"]
        for sku, n in items:
            pos.escanear(self.con, vid, sku, n)
        if cliente:
            F.asignar_cliente(self.con, vid, *cliente)
        self.cobrar_usd_y_cerrar(vid, usuario)
        return vid

    def docs(self):
        cur = self.con.execute("SELECT * FROM documentos_fiscales ORDER BY id")
        return [dict(zip([c[0] for c in cur.description], r)) for r in cur]


class Fiscal(Preparado):
    # ---------- cola ----------
    def test_cerrar_la_venta_encola_la_factura_con_montos_en_bs_que_cuadran(self):
        self.vender()
        d, = self.docs()
        self.assertEqual((d["tipo"], d["estado"], d["numero"], d["receptor_nombre"]), ("factura", "pendiente", None, None))   # consumidor final
        self.assertEqual((d["total_usd_cents"], d["iva_usd_cents"], d["iva_bp"]), (1739, 240, 1600))
        self.assertEqual((d["base_bs_centimos"], d["iva_bs_centimos"], d["total_bs_centimos"]), (60710, 9720, 70430))
        self.assertEqual(d["base_bs_centimos"] + d["iva_bs_centimos"], d["total_bs_centimos"])

    def test_con_fiscal_apagado_no_se_encola_nada(self):
        self.con.execute("UPDATE config SET valor='0' WHERE clave='fiscal_activo'")
        self.vender(); self.assertEqual(self.docs(), [])

    def test_la_venta_no_depende_del_proveedor(self):
        vid = self.vender()                                                        # sin adaptador configurado: la venta se cierra igual
        self.assertEqual(pos.ver_venta(self.con, vid)["venta"]["estado"], "cerrada")
        with self.assertRaisesRegex(FiscalError, "No hay proveedor"):
            F.emitir_pendientes(self.con)
        self.assertEqual(F.estado(self.con)["pendientes"], 1)

    def test_simulador_solo_en_modo_prueba_y_no_entra_al_libro(self):
        self.con.execute("UPDATE config SET valor='simulador' WHERE clave='fiscal_adaptador'")
        self.vender()
        with self.assertRaisesRegex(FiscalError, "modo de prueba"):
            F.emitir_pendientes(self.con)
        self.con.execute("UPDATE config SET valor='1' WHERE clave='fiscal_modo_prueba'")
        self.assertEqual(F.emitir_pendientes(self.con, self.dueno)["emitidos"], 1)
        d, = self.docs()
        self.assertEqual((d["simulado"], d["numero"][:6]), (1, "SIM-F-"))
        self.assertEqual(F.libro_ventas(self.con, self.dueno, "2026-01-01", "2030-01-01")["filas"], [])
        self.assertEqual(len(F.libro_ventas(self.con, self.dueno, "2026-01-01", "2030-01-01", incluir_simulados=True)["filas"]), 1)

    # ---------- emisión ----------
    def test_fallo_del_proveedor_deja_error_y_se_reintenta_sin_tocar_la_venta(self):
        self.vender()
        ad = Real(falla=1)
        self.assertEqual(F.emitir_pendientes(self.con, adaptador=ad), {"emitidos": 0, "errores": 1, "omitidos": 0})
        d, = self.docs(); self.assertEqual((d["estado"], d["intentos"], "sin conexión" in d["error"]), ("error", 1, True))
        self.assertEqual(F.emitir_pendientes(self.con, adaptador=ad)["emitidos"], 1)
        d, = self.docs(); self.assertEqual((d["estado"], d["numero"], d["numero_control"], d["error"], d["intentos"]), ("emitido", "00-000001", "CTL-000001", None, 2))

    def test_respuesta_invalida_o_numero_repetido_no_se_aceptan(self):
        a, b = self.vender(), self.vender()
        class Mudo(Adaptador):
            def emitir(self, doc, afectada=None): return {"numero": "", "fecha_emision": None}
        self.assertEqual(F.emitir_pendientes(self.con, adaptador=Mudo())["errores"], 2)
        r = F.emitir_pendientes(self.con, adaptador=Real(repetir="00-000777"))
        self.assertEqual((r["emitidos"], r["errores"]), (1, 1))                    # el segundo repite el número: se rechaza
        self.assertIn("ya usado", self.docs()[1]["error"])

    def test_emitido_es_inmutable_y_nada_se_borra(self):
        self.vender(); F.emitir_pendientes(self.con, adaptador=Real())
        for sql in ("UPDATE documentos_fiscales SET numero='X'", "UPDATE documentos_fiscales SET total_bs_centimos=1", "DELETE FROM documentos_fiscales"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.con.execute(sql)
        self.vender()
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("DELETE FROM documentos_fiscales WHERE estado='pendiente'")
        with self.assertRaises(sqlite3.IntegrityError):                            # una sola factura por venta
            self.con.execute("INSERT INTO documentos_fiscales(uuid,tipo,venta_id,base_bs_centimos,iva_bs_centimos,total_bs_centimos,base_usd_cents,iva_usd_cents,"
                             "total_usd_cents,iva_bp,tasa_micro) VALUES('u','factura',1,1,1,1,1,1,1,1600,1)")

    # ---------- notas de crédito ----------
    def test_devolucion_genera_nota_de_credito_que_espera_a_su_factura(self):
        vid = self.vender()
        pos.devolver(self.con, self.dueno, vid, [(NEG, 1)], "talla incorrecta")
        f, nc = self.docs()
        self.assertEqual((nc["tipo"], nc["estado"], nc["devolucion_id"], nc["total_bs_centimos"], nc["base_bs_centimos"], nc["receptor_nombre"]),
                         ("nota_credito", "pendiente", 1, 70430, 60710, None))
        ad = Real()
        class SoloNotas(Real):
            pass
        self.con.execute("UPDATE documentos_fiscales SET estado='error' WHERE id=?", (f["id"],))      # la factura aún no está emitida
        ad.falla = 1
        r = F.emitir_pendientes(self.con, adaptador=ad)
        self.assertEqual((r["errores"], r["omitidos"], r["emitidos"]), (1, 1, 0))                     # la nota se omite, no se emite sola
        r = F.emitir_pendientes(self.con, adaptador=ad)
        self.assertEqual((r["emitidos"], r["omitidos"]), (2, 0))                                      # primero factura, luego nota
        resp = json.loads(self.docs()[1]["respuesta"]); self.assertEqual(resp["afectada"], "00-000001")

    def test_devolucion_de_venta_sin_factura_fiscal_no_inventa_nota(self):
        self.con.execute("UPDATE config SET valor='0' WHERE clave='fiscal_activo'")
        vid = self.vender()
        self.con.execute("UPDATE config SET valor='1' WHERE clave='fiscal_activo'")
        pos.devolver(self.con, self.dueno, vid, [(NEG, 1)], "x")
        self.assertEqual(self.docs(), [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM auditoria WHERE accion='devolucion_sin_factura_fiscal'").fetchone()[0], 1)

    # ---------- cliente ----------
    def test_datos_del_cliente(self):
        vid = self.vender(cliente=("j123456789", " Calzados  Sol C.A. ", "Av. 1"))
        d, = self.docs(); self.assertEqual((d["receptor_rif"], d["receptor_nombre"], d["receptor_domicilio"]), ("J-12345678-9", "Calzados Sol C.A.", "Av. 1"))
        v2 = pos.abrir_venta(self.con, self.luis)["venta_id"]
        F.asignar_cliente(self.con, v2, "v-12345678", "Ana Pérez")
        self.assertEqual(self.con.execute("SELECT cliente_rif FROM ventas WHERE id=?", (v2,)).fetchone()[0], "V-12345678")
        F.asignar_cliente(self.con, v2, "", "")                                                       # vuelve a consumidor final
        self.assertIsNone(self.con.execute("SELECT cliente_nombre FROM ventas WHERE id=?", (v2,)).fetchone()[0])
        for mal in (("mal", "X"), ("J-12345678-9", ""), ("", "Solo nombre"), ("J-12345678-9", "x" * 121)):
            with self.assertRaises(FiscalError, msg=mal):
                F.asignar_cliente(self.con, v2, *mal)
        with self.assertRaisesRegex(FiscalError, "abierta"):
            F.asignar_cliente(self.con, vid, "V-1234567", "Tarde")                                    # ya cerrada: no se cambia

    # ---------- libro de ventas ----------
    def test_libro_de_ventas_con_notas_de_credito_negativas_y_hora_local(self):
        a = self.vender(cliente=("J-12345678-9", "Calzados Sol"))
        b = self.vender(((ROJ, 1),))
        pos.devolver(self.con, self.dueno, a, [(NEG, 1)], "defecto")
        F.emitir_pendientes(self.con, adaptador=Real([Z(2026, 10, 10, 15), Z(2026, 10, 10, 16), Z(2026, 10, 10, 20)]))
        L = F.libro_ventas(self.con, self.dueno, "2026-10-10")
        self.assertEqual([(f["tipo"], f["numero"], f["total_bs_centimos"]) for f in L["filas"]],
                         [("Factura", "00-000001", 70430), ("Factura", "00-000002", 70430), ("Nota de Crédito", "00-000003", -70430)])
        self.assertEqual((L["filas"][0]["cliente"], L["filas"][0]["rif"], L["filas"][1]["cliente"]), ("Calzados Sol", "J-12345678-9", "Consumidor final"))
        self.assertEqual((L["filas"][2]["documento_afectado"], L["filas"][0]["alicuota"], L["filas"][0]["fecha"]), ("00-000001", "16%", "10/10/2026"))
        self.assertEqual(L["totales"], {"base_bs_centimos": 60710, "iva_bs_centimos": 9720, "total_bs_centimos": 70430})   # neto: 2 facturas - 1 nota
        self.assertEqual(L["advertencias"], [])
        self.assertEqual(F.libro_ventas(self.con, self.dueno, "2026-10-11")["filas"], [])

    def test_el_libro_usa_el_dia_local_y_avisa_lo_que_falta(self):
        self.vender(); self.vender()
        F.emitir_pendientes(self.con, adaptador=Real([Z(2026, 10, 10, 2), Z(2026, 10, 10, 5)], falla=0), limite=1)    # solo una emitida
        self.assertEqual(len(F.libro_ventas(self.con, self.dueno, "2026-10-09")["filas"]), 1)         # 02:00 UTC = 22:00 del 9 en Venezuela
        self.assertEqual(F.libro_ventas(self.con, self.dueno, "2026-10-10")["filas"], [])
        hoy = datetime.now(timezone.utc).date().isoformat()
        adv = F.libro_ventas(self.con, self.dueno, "2026-10-09", hoy)["advertencias"]
        self.assertTrue(adv and "sin factura emitida" in adv[0])

    def test_libro_solo_dueno_y_csv_con_signos(self):
        a = self.vender(); pos.devolver(self.con, self.dueno, a, [(NEG, 1)], "x")
        F.emitir_pendientes(self.con, adaptador=Real())
        with self.assertRaises(auth.AuthError):
            F.libro_ventas(self.con, self.luis, "2026-10-10")
        ruta = os.path.join(self.d.name, "libro.csv")
        R.exportar_csv(F.libro_ventas(self.con, self.dueno, "2026-10-10")["filas"], ruta, dinero=("base_bs_centimos", "iva_bs_centimos", "total_bs_centimos"))
        with open(ruta, encoding="utf-8-sig") as f:
            txt = f.read()
        self.assertIn("704,30", txt); self.assertIn("-704,30", txt); self.assertIn("Nota de Crédito", txt)

    def test_configuracion_fiscal_validada(self):
        for mal in ({"fiscal_activo": "2"}, {"fiscal_adaptador": "inventado"}, {"fiscal_modo_prueba": "si"}):
            with self.assertRaises(N.NegocioError):
                N.actualizar_datos(self.con, self.dueno, mal)
        N.actualizar_datos(self.con, self.dueno, {"fiscal_adaptador": "simulador", "fiscal_modo_prueba": "1"})
        with self.assertRaises(auth.AuthError):
            N.actualizar_datos(self.con, self.luis, {"fiscal_activo": "0"})


class Servidor(Preparado):
    def setUp(self):
        super().setUp()
        self.con.execute("UPDATE config SET valor='simulador' WHERE clave='fiscal_adaptador'"); self.con.execute("UPDATE config SET valor='1' WHERE clave='fiscal_modo_prueba'")
        self.srv = crear_servidor(os.path.join(self.d.name, "r.db"), 0); threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def req(self, m, ruta, cuerpo=None, tok=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        c.request(m, ruta, json.dumps(cuerpo) if cuerpo is not None else None, {"X-Token": tok or "", "Content-Type": "application/json"})
        r = c.getresponse(); d = r.read(); c.close(); return r.status, json.loads(d)

    def test_endpoints_fiscales(self):
        dueno = self.req("POST", "/api/login", {"nombre": "Marta", "pin": "4821"})[1]["token"]
        luis = self.req("POST", "/api/login", {"nombre": "Luis", "pin": "7305"})[1]["token"]
        for m, r in (("GET", "/api/fiscal/estado"), ("POST", "/api/fiscal/emitir"), ("GET", "/api/fiscal/libro?desde=2026-10-10")):
            self.assertEqual(self.req(m, r, tok=luis)[0], 403, r)
        vid = self.req("POST", "/api/venta", tok=luis)[1]["venta_id"]
        self.req("POST", f"/api/venta/{vid}/escanear", {"codigo": NEG}, luis)
        self.assertEqual(self.req("POST", f"/api/venta/{vid}/cliente", {"rif": "mal", "nombre": "X"}, luis)[0], 400)
        st, v = self.req("POST", f"/api/venta/{vid}/cliente", {"rif": "V-12345678", "nombre": "Ana Pérez"}, luis)
        self.assertEqual((st, v["venta"]["cliente_nombre"]), (200, "Ana Pérez"))
        self.req("POST", f"/api/venta/{vid}/pago", {"metodo": "efectivo_usd", "monto": "17,39"}, luis)
        self.assertEqual(self.req("POST", f"/api/venta/{vid}/cerrar", tok=luis)[0], 200)
        self.assertEqual(self.req("GET", "/api/fiscal/estado", tok=dueno)[1]["pendientes"], 1)
        self.assertEqual(self.req("POST", "/api/fiscal/emitir", tok=dueno)[1]["emitidos"], 1)
        e = self.req("GET", "/api/fiscal/estado", tok=dueno)[1]
        self.assertEqual((e["emitidos"], e["adaptador"], e["modo_prueba"]), (1, "simulador", True))
        self.assertEqual(self.req("GET", "/api/fiscal/libro?desde=2026-01-01&hasta=2030-01-01", tok=dueno)[1]["filas"], [])   # simulados no cuentan


if __name__ == "__main__":
    unittest.main()
