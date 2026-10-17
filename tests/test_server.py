import http.client, json, os, sys, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.server import Sesiones, crear_servidor
from core import auth, ticket as T
from tests.test_reportes import Base

NEG = "GAMO-FLOR-38-NEG"


def leer(ruta):
    with open(ruta, "rb") as f:
        return f.read()


class S(Base):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.d.name, "r.db")
        self.impresora = os.path.join(self.d.name, "t.bin")
        self.con.execute("INSERT INTO config(clave,valor) VALUES('impresora_ticket',?)", (f"archivo:{self.impresora}",))
        auth.crear_usuario(self.con, 2, "Ana", "cajero", "5192", actor=self.dueno)
        self.srv = crear_servidor(self.db, 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_port

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        super().tearDown()

    def req(self, metodo, ruta, cuerpo=None, token=None, host=None, crudo=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Token"] = token
        c.request(metodo, ruta, crudo if crudo is not None else (json.dumps(cuerpo) if cuerpo is not None else None), h)
        r = c.getresponse(); datos = r.read(); c.close()
        try:
            return r.status, json.loads(datos)
        except ValueError:
            return r.status, datos

    def login(self, nombre="Luis", pin="7305", sucursal=1):
        st, r = self.req("POST", "/api/login", {"nombre": nombre, "pin": pin, "sucursal_id": sucursal})
        self.assertEqual(st, 200, r)
        return r["token"]

    def venta_cerrada(self, tok):
        _, v = self.req("POST", "/api/venta", token=tok); vid = v["venta_id"]
        self.req("POST", f"/api/venta/{vid}/escanear", {"codigo": NEG}, tok)
        _, z = self.req("POST", f"/api/venta/{vid}/pago", {"metodo": "zelle", "monto": "5,00", "referencia": "ZEL12345"}, tok)
        self.req("POST", f"/api/venta/{vid}/pago", {"metodo": "efectivo_usd", "monto": "12,39"}, tok)
        self.req("POST", f"/api/pago/{z['pago_id']}/confirmar", token=tok)
        st, r = self.req("POST", f"/api/venta/{vid}/cerrar", token=tok)
        self.assertEqual(st, 200, r)
        return vid, r

    # ---------- seguridad ----------
    def test_acceso_y_sesion(self):
        self.assertEqual(self.req("GET", "/api/estado")[0], 401)
        self.assertEqual(self.req("GET", "/api/estado", host="evil.com")[0], 403)             # protege contra páginas externas
        self.assertEqual(self.req("GET", "/api/estado", host=f"localhost:{self.port}", token=self.login())[0], 200)
        self.assertEqual(self.req("POST", "/api/login", {"nombre": "Luis", "pin": "0000"})[0], 401)
        self.assertEqual(self.req("POST", "/api/login", {"nombre": "Nadie", "pin": "7305"})[0], 401)
        tok = self.login()
        self.assertEqual(self.req("POST", "/api/logout", token=tok)[0], 200)
        self.assertEqual(self.req("GET", "/api/estado", token=tok)[0], 401)

    def test_bloqueo_por_intentos(self):
        for _ in range(5):
            self.req("POST", "/api/login", {"nombre": "Luis", "pin": "9999"})
        st, r = self.req("POST", "/api/login", {"nombre": "Luis", "pin": "7305"})
        self.assertEqual(st, 403); self.assertIn("bloqueado", r["error"])

    def test_sesiones_vencen_por_inactividad(self):
        t = [0.0]
        s = Sesiones(ttl=100, reloj=lambda: t[0])
        tok = s.crear({"id": 1})
        t[0] = 90; self.assertEqual(s.obtener(tok), {"id": 1})
        t[0] = 180; self.assertEqual(s.obtener(tok), {"id": 1})          # el uso renueva
        t[0] = 400; self.assertIsNone(s.obtener(tok))
        self.assertIsNone(s.obtener("inventado"))

    def test_errores_http(self):
        tok = self.login()
        self.assertEqual(self.req("GET", "/api/nada", token=tok)[0], 404)
        self.assertEqual(self.req("POST", "/api/venta", token=tok, crudo="{no es json")[0], 400)
        self.assertEqual(self.req("POST", "/api/venta", token=tok, crudo="[1,2]")[0], 400)
        _, v = self.req("POST", "/api/venta", token=tok)
        st, r = self.req("POST", f"/api/venta/{v['venta_id']}/escanear", {"codigo": "NOEXISTE"}, tok)
        self.assertEqual(st, 400); self.assertIn("no encontrado", r["error"])
        st, r = self.req("POST", f"/api/venta/{v['venta_id']}/escanear", {"codigo": NEG, "cantidad": "abc"}, tok)
        self.assertEqual(st, 400)
        self.assertEqual(self.req("POST", f"/api/venta/{v['venta_id']}/pago", {"metodo": "zelle", "monto": "abc"}, tok)[0], 400)

    def test_cada_sucursal_ve_solo_lo_suyo(self):
        _, v = self.req("POST", "/api/venta", token=self.login())
        ana = self.login("Ana", "5192", 2)
        self.assertEqual(self.req("GET", f"/api/venta/{v['venta_id']}", token=ana)[0], 404)
        self.assertEqual(self.req("POST", f"/api/venta/{v['venta_id']}/escanear", {"codigo": NEG}, ana)[0], 404)

    def test_pantalla_inicial(self):
        st, cuerpo = self.req("GET", "/")
        self.assertEqual(st, 200); self.assertIn(b"ZapaPOS", cuerpo)

    # ---------- flujo de caja ----------
    def test_venta_completa_por_http_con_ticket_y_cajon(self):
        tok = self.login()
        st, e = self.req("GET", "/api/estado", token=tok)
        self.assertEqual((e["tasa"]["tipo"], e["tasa"]["tasa_micro"], e["impresora"]), ("bcv", 40_500_000, True))
        self.assertEqual(e["metodos"]["pago_movil"], {"moneda": "BS", "referencia": True, "digital": True, "efectivo": False})
        _, v = self.req("POST", "/api/venta", token=tok); vid = v["venta_id"]
        self.assertEqual(len(self.req("GET", "/api/buscar?q=flor", token=tok)[1]), 2)
        _, r = self.req("POST", f"/api/venta/{vid}/escanear", {"codigo": "gamo-flor-38-neg"}, tok)
        self.assertEqual(r["venta"]["venta"]["total_usd_cents"], 1739)
        item = r["venta"]["lineas"][0]["item_id"]
        _, r = self.req("POST", f"/api/venta/{vid}/item/{item}", {"cantidad": 1}, tok)
        _, z = self.req("POST", f"/api/venta/{vid}/pago", {"metodo": "zelle", "monto": "5,00", "referencia": "ZEL12345"}, tok)
        self.assertTrue(z["requiere_verificacion"])
        _, p = self.req("POST", f"/api/venta/{vid}/pago", {"metodo": "efectivo_usd", "monto": "12,39"}, tok)
        self.assertEqual(p["venta"]["resumen_pagos"]["pendiente_usd_cents"], 0)
        st, r = self.req("POST", f"/api/venta/{vid}/cerrar", token=tok)
        self.assertEqual(st, 400); self.assertIn("confirmar", r["error"])
        self.assertEqual(self.req("POST", f"/api/pago/{z['pago_id']}/confirmar", token=tok)[0], 200)
        st, r = self.req("POST", f"/api/venta/{vid}/cerrar", token=tok)
        self.assertEqual((st, r["venta"]["venta"]["estado"], r["ticket"], r["ticket_error"]), (200, "cerrada", {"cajon": True}, None))
        datos = leer(self.impresora)
        self.assertTrue(datos.endswith(T.CAJON))
        self.assertIn("TOTAL USD", datos.decode("cp858")); self.assertIn("$17,39", datos.decode("cp858"))
        os.remove(self.impresora)
        self.assertEqual(self.req("POST", f"/api/venta/{vid}/ticket", token=tok)[0], 200)
        self.assertIn(b"REIMPRESION", leer(self.impresora))

    def test_si_falla_la_impresora_la_venta_igual_se_cierra(self):
        self.con.execute("UPDATE config SET valor='red:127.0.0.1:1' WHERE clave='impresora_ticket'")
        vid, r = self.venta_cerrada(self.login())
        self.assertEqual(r["venta"]["venta"]["estado"], "cerrada")
        self.assertIn("No se pudo imprimir", r["ticket_error"])
        self.con.execute("DELETE FROM config WHERE clave='impresora_ticket'")
        self.assertEqual(self.req("POST", f"/api/venta/{vid}/ticket", token=self.login())[0], 400)

    def test_vuelto_y_anulaciones_por_http(self):
        tok = self.login()
        _, v = self.req("POST", "/api/venta", token=tok); vid = v["venta_id"]
        self.req("POST", f"/api/venta/{vid}/escanear", {"codigo": NEG}, tok)
        _, p = self.req("POST", f"/api/venta/{vid}/pago", {"metodo": "efectivo_usd", "monto": "20"}, tok)
        self.assertEqual(p["vuelto_sugerido"]["usd_cents"], 261)
        self.assertEqual(self.req("POST", f"/api/venta/{vid}/cerrar", token=tok)[0], 400)           # falta el vuelto
        st, r = self.req("POST", f"/api/venta/{vid}/vuelto", {"metodo": "efectivo_bs", "monto": "105,71"}, tok)
        self.assertEqual((st, r["resumen_pagos"]["exceso_usd_cents"]), (200, 0))
        pid = p["pago_id"]                                                                           # efectivo: solo con PIN del dueño
        self.assertEqual(self.req("POST", f"/api/pago/{pid}/anular", {"motivo": "error"}, tok)[0], 403)
        self.assertEqual(self.req("POST", f"/api/pago/{pid}/anular", {"motivo": "error", "autoriza": {"nombre": "Marta", "pin": "0000"}}, tok)[0], 403)
        self.assertEqual(self.req("POST", f"/api/pago/{pid}/anular", {"motivo": "error", "autoriza": {"nombre": "Marta", "pin": "4821"}}, tok)[0], 200)
        _, otro = self.req("POST", "/api/venta", token=tok)
        self.assertEqual(self.req("POST", f"/api/venta/{otro['venta_id']}/anular", {"motivo": "cliente se fue"}, tok)[0], 200)

    # ---------- permisos del dueño ----------
    def test_tasa_cajon_y_devolucion_exigen_dueno(self):
        luis, marta = self.login(), self.login("Marta", "4821")
        self.assertEqual(self.req("POST", "/api/tasa", {"tipo": "bcv", "valor": "41,25"}, luis)[0], 403)
        st, t = self.req("POST", "/api/tasa", {"tipo": "bcv", "valor": "41,25"}, marta)
        self.assertEqual((st, t["tasa_micro"]), (200, 41_250_000))
        self.assertEqual(self.req("POST", "/api/tasa", {"tipo": "bcv", "valor": "0"}, marta)[0], 400)
        self.assertEqual(self.req("POST", "/api/cajon", {}, luis)[0], 403)
        self.assertEqual(self.req("POST", "/api/cajon", {"autoriza": {"nombre": "Marta", "pin": "4821"}}, luis)[0], 200)
        self.assertTrue(leer(self.impresora).endswith(T.CAJON))

    def test_devolucion_con_pin_de_dueno_es_atomica(self):
        luis = self.login()
        vid, _ = self.venta_cerrada(luis)
        cuerpo = {"venta_id": vid, "items": [[NEG, 1]], "motivo": "talla incorrecta", "reembolso": {"metodo": "efectivo_usd", "monto": "17,39"}}
        self.assertEqual(self.req("POST", "/api/devolucion", cuerpo, luis)[0], 403)
        self.assertEqual(self.req("POST", "/api/devolucion", {**cuerpo, "autoriza": {"nombre": "Marta", "pin": "1111"}}, luis)[0], 403)
        exceso = {**cuerpo, "reembolso": {"metodo": "efectivo_usd", "monto": "99"}, "autoriza": {"nombre": "Marta", "pin": "4821"}}
        self.assertEqual(self.req("POST", "/api/devolucion", exceso, luis)[0], 400)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM devoluciones").fetchone()[0], 0)       # nada a medias
        st, r = self.req("POST", "/api/devolucion", {**cuerpo, "autoriza": {"nombre": "Marta", "pin": "4821"}}, luis)
        self.assertEqual((st, r["completa"], r["total_usd_cents"], r["pendiente_reembolso_usd_cents"]), (200, True, 1739, 0))

    def test_cierre_de_caja_por_http(self):
        luis = self.login(); self.venta_cerrada(luis)
        st, r = self.req("POST", "/api/caja/cerrar", {"contado_usd": "12,39", "contado_bs": "0", "retirado_usd": "10"}, luis)
        self.assertEqual((st, r["esperado_usd_cents"], r["diferencia_usd_cents"]), (200, 1239, 0))
        self.assertEqual(self.req("POST", "/api/caja/cerrar", {"contado_usd": "abc"}, luis)[0], 400)


if __name__ == "__main__":
    unittest.main()
