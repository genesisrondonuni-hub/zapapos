import http.client, json, os, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
from app.server import crear_servidor
from tools.db import connect, migrate
from core import auth, negocio as N, ticket as T
from core.negocio import NegocioError

ALTA = dict(negocio="Zapatería Los Andes", rif="j-12345678-9", direccion="Av. Principal, local 4", telefono="0414-0000000",
            dueno_nombre="Marta", dueno_pin="4821", cajero_nombre="Luis", cajero_pin="7305")


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.d.name, "n.db")
        self.con = connect(self.db); migrate(self.con)

    def tearDown(self):
        self.con.close(); self.d.cleanup()


class Unidad(Base):
    def test_primer_arranque_crea_todo_y_solo_una_vez(self):
        self.assertTrue(N.necesita_configuracion(self.con))
        r = N.configurar_negocio(self.con, **ALTA)
        self.assertFalse(N.necesita_configuracion(self.con))
        d = N.leer_datos(self.con)
        self.assertEqual((d["negocio_nombre"], d["negocio_rif"], d["instalacion_id"]), ("Zapatería Los Andes", "J-12345678-9", r["instalacion_id"]))
        self.assertEqual(auth.login(self.con, r["sucursal_id"], "marta", "4821")["rol"], "dueno")
        self.assertEqual(auth.login(self.con, r["sucursal_id"], "Luis", "7305")["rol"], "cajero")
        with self.assertRaisesRegex(NegocioError, "ya está configurado"):
            N.configurar_negocio(self.con, **ALTA)

    def test_validaciones_del_alta_y_nada_queda_a_medias(self):
        casos = [dict(negocio=""), dict(rif="12345"), dict(dueno_pin="12"), dict(dueno_pin="1111"), dict(cajero_pin="4821"),
                 dict(cajero_nombre="marta"), dict(negocio="x" * 121)]
        for c in casos:
            with self.assertRaises(NegocioError, msg=c):
                N.configurar_negocio(self.con, **{**ALTA, **c})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM sucursales").fetchone()[0], 0)
        self.assertIsNone(self.con.execute("SELECT valor FROM config WHERE clave='instalacion_id'").fetchone())

    def test_cajero_es_opcional_y_rif_es_opcional(self):
        N.configurar_negocio(self.con, **{**ALTA, "cajero_nombre": None, "cajero_pin": None, "rif": ""})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 1)
        self.assertEqual(N.leer_datos(self.con)["negocio_rif"], "")

    def test_formato_de_rif(self):
        for ok, esperado in (("J-12345678-9", "J-12345678-9"), ("j123456789", "J-12345678-9"), (" V 12345678 5 ", "V-12345678-5")):
            self.assertEqual(N.normalizar_rif(ok), esperado)
        for mal in ("", "A-12345678-9", "J-1234567-9", "J-123456789-9", "J-12345678"):
            with self.assertRaises(NegocioError):
                N.normalizar_rif(mal)

    def test_actualizar_datos_solo_dueno_y_con_validacion(self):
        r = N.configurar_negocio(self.con, **ALTA)
        dueno, cajero = auth.login(self.con, 1, "Marta", "4821"), auth.login(self.con, 1, "Luis", "7305")
        with self.assertRaises(auth.AuthError):
            N.actualizar_datos(self.con, cajero, {"negocio_nombre": "X"})
        N.actualizar_datos(self.con, dueno, {"negocio_telefono": "0212-5551234", "impresora_ticket": "red:192.168.1.50:9100", "ticket_cajon": "0",
                                             "ticket_codepage": "cp850", "ticket_pie": ""})
        d = N.leer_datos(self.con)
        self.assertEqual((d["negocio_telefono"], d["impresora_ticket"], d["ticket_cajon"], d["ticket_codepage"], d["ticket_pie"]),
                         ("0212-5551234", "red:192.168.1.50:9100", "0", "cp850", ""))
        for mal in ({"negocio_nombre": " "}, {"negocio_rif": "mal"}, {"impresora_ticket": "wifi:1"}, {"impresora_ticket": "red:"}, {"ticket_codepage": "x"},
                    {"ticket_cajon": "2"}, {"instalacion_id": "otro"}, {"tasa_default": "paralelo"}):
            with self.assertRaises(NegocioError, msg=mal):
                N.actualizar_datos(self.con, dueno, mal)
        self.assertEqual(N.leer_datos(self.con)["instalacion_id"], r["instalacion_id"])             # no se puede cambiar

    def test_el_ticket_usa_los_datos_configurados(self):
        N.configurar_negocio(self.con, **ALTA)
        self.assertEqual(T._cfg(self.con, "negocio_rif"), "J-12345678-9")

    def test_cajeros_y_sucursales(self):
        r = N.configurar_negocio(self.con, **{**ALTA, "cajero_nombre": None, "cajero_pin": None})
        dueno = auth.login(self.con, 1, "Marta", "4821")
        from core import licencia as LIC, fiscal    # el plan Pro permite varias sucursales
        priv, pub = __import__("tools.actualizar", fromlist=["x"]).generar_claves(os.path.join(self.d.name, "k"))
        LIC.CLAVE_PUBLICA = pub
        self.addCleanup(setattr, LIC, "CLAVE_PUBLICA", None)
        LIC.instalar(self.con, __import__("json").dumps(LIC.emitir(priv, r["instalacion_id"], "Demo", "pro", "2099-01-01")), pub)
        N.crear_cajero(self.con, dueno, "Ana", "5192")
        with self.assertRaises(NegocioError):
            N.crear_cajero(self.con, dueno, "ana", "6038")
        with self.assertRaises(auth.AuthError):
            N.crear_cajero(self.con, auth.login(self.con, 1, "Ana", "5192"), "Pepe", "6038")
        self.assertEqual(N.crear_sucursal(self.con, dueno, " s2 ", "Segunda  tienda"), 2)
        for mal in (("S2", "Otra"), ("X", "Otra"), ("S3", " ")):
            with self.assertRaises(NegocioError):
                N.crear_sucursal(self.con, dueno, *mal)


class Servidor(Base):
    def setUp(self):
        super().setUp()
        self.con.close()
        self.srv = crear_servidor(self.db, 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.con = connect(self.db)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        super().tearDown()

    def req(self, m, ruta, cuerpo=None, tok=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        c.request(m, ruta, json.dumps(cuerpo) if cuerpo is not None else None, {"X-Token": tok or "", "Content-Type": "application/json"})
        r = c.getresponse(); datos = r.read(); c.close()
        return r.status, json.loads(datos)

    def test_alta_por_http_entra_solo_y_se_cierra(self):
        self.assertEqual(self.req("GET", "/api/instalacion"), (200, {"configurado": False}))
        self.assertEqual(self.req("GET", "/api/negocio")[0], 401)
        st, r = self.req("POST", "/api/instalacion", {"negocio": "X", "rif": "mal", "dueno_nombre": "M", "dueno_pin": "4821"})
        self.assertEqual((st, "RIF" in r["error"]), (400, True))
        body = {"negocio": "Calzados Sol", "rif": "J-12345678-9", "dueno_nombre": "Marta", "dueno_pin": "4821", "cajero_nombre": "Luis", "cajero_pin": "7305"}
        st, r = self.req("POST", "/api/instalacion", body)
        self.assertEqual((st, r["usuario"]["rol"]), (200, "dueno"))
        self.assertEqual(self.req("GET", "/api/instalacion")[1], {"configurado": True})
        self.assertEqual(self.req("POST", "/api/instalacion", body)[0], 400)                         # no se puede reconfigurar
        self.assertEqual(self.req("GET", "/api/negocio", tok=r["token"])[1]["negocio_nombre"], "Calzados Sol")

    def test_dueno_edita_y_crea_cajeros_cajero_no(self):
        _, r = self.req("POST", "/api/instalacion", dict(ALTA, negocio=ALTA["negocio"]) | {})
        dueno = r["token"]
        luis = self.req("POST", "/api/login", {"nombre": "Luis", "pin": "7305"})[1]["token"]
        self.assertEqual(self.req("POST", "/api/negocio", {"negocio_telefono": "0212-1"}, luis)[0], 403)
        self.assertEqual(self.req("POST", "/api/usuarios", {"nombre": "Ana", "pin": "5192"}, luis)[0], 403)
        self.assertEqual(self.req("POST", "/api/negocio", {"negocio_telefono": "0212-1"}, dueno)[1]["negocio_telefono"], "0212-1")
        self.assertEqual(self.req("POST", "/api/negocio", {"negocio_rif": "mal"}, dueno)[0], 400)
        self.assertEqual(self.req("POST", "/api/usuarios", {"nombre": "Ana", "pin": "5192"}, dueno)[0], 200)
        st, e = self.req("POST", "/api/usuarios", {"nombre": "Ana", "pin": "6038"}, dueno)
        self.assertEqual((st, "Ya existe" in e["error"]), (400, True))


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class Pantalla(Servidor):
    def test_asistente_de_primer_arranque_en_pantalla(self):
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_context(viewport={"width": 1000, "height": 900}, bypass_csp=True).new_page()
            url = f"http://127.0.0.1:{self.srv.server_port}/"
            pg.goto(url); pg.wait_for_selector("#sgo")
            for k, v in (("nn", "Zapatería Los Andes"), ("rif", "J-12345678-9"), ("dir", "Av. Principal"), ("tel", "0414-0000000"),
                         ("dn", "Marta"), ("dp", "4821"), ("dp2", "4822")):
                pg.fill(f"#s_{k}", v)
            pg.click("#sgo"); pg.wait_for_selector("#se .err"); self.assertIn("no coinciden", pg.inner_text("#se"))
            pg.fill("#s_dp2", "4821"); pg.screenshot(path="/tmp/ui_alta.png"); pg.click("#sgo")
            pg.wait_for_selector("#scan")                                                           # entra directo a la caja
            pg.click("#mas"); pg.click('button[data-o="neg"]'); pg.wait_for_selector("#f_negocio_rif")
            self.assertEqual(pg.input_value("#f_negocio_rif"), "J-12345678-9")
            b.close()


if __name__ == "__main__":
    unittest.main()
