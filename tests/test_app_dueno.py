import http.client, json, os, shutil, sys, tempfile, threading, unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
from app.server import crear_servidor
from core import negocio as N
from tests.test_reportes import Base

# Valores fake de test. NO son credenciales reales.
# Se usan "" como "ausente" para simular panel sin configurar.
if not os.environ.get("ZAPAPOS_URL_NUBE"):
    os.environ["ZAPAPOS_URL_NUBE"] = "https://test.example.invalid"
if not os.environ.get("ZAPAPOS_APIKEY"):
    os.environ["ZAPAPOS_APIKEY"] = "test-apikey-NOT-REAL"

URL_NUBE = os.environ["ZAPAPOS_URL_NUBE"]
APIKEY = os.environ["ZAPAPOS_APIKEY"]


class ServidorBase(Base):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.d.name, "r.db")
        self.srv = crear_servidor(self.db, 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def get(self, ruta):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)
        c.request("GET", ruta, headers={"Host": f"127.0.0.1:{self.srv.server_port}"})
        r = c.getresponse(); d = r.read(); c.close(); return r.status, r.headers["Content-Type"], d


class HTTP(ServidorBase):
    def test_rutas_dueno_sirven_html(self):
        for ruta in ("/dueno", "/dueno.html"):
            st, ct, d = self.get(ruta)
            self.assertEqual(st, 200, ruta)
            self.assertIn("text/html", ct, ruta)
            self.assertIn(b"ZapaPOS", d, ruta)
            self.assertIn(b"panel del due", d, ruta)

    def test_html_es_valido_y_tiene_lo_esencial(self):
        _, _, d = self.get("/dueno")
        html = d.decode("utf-8")
        self.assertIn("<meta name=\"viewport\"", html)
        self.assertIn("viewport-fit=cover", html)
        self.assertIn(URL_NUBE, html)
        self.assertIn(APIKEY, html)
        self.assertIn("localStorage", html)          # persiste las tiendas entre recargas
        for sec in ("Hoy", "Tiendas", "Configurar", "Agregar tienda", "instalacion_id", "token"):
            self.assertIn(sec, html, sec)

    def test_mismo_control_de_acceso_que_la_caja(self):
        st, _, _ = self.get.__func__(self, "/dueno") if False else self.get("/dueno")
        self.assertEqual(st, 200)
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)
        c.request("GET", "/dueno", headers={"Host": "evil.com"})
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 403)

    def test_la_caja_y_el_panel_coexisten(self):
        for ruta, texto in (("/", b"Escanee"), ("/dueno", b"panel del due")):
            _, _, d = self.get(ruta); self.assertIn(texto, d)

    def test_nube_url_y_apikey_precargados_en_el_formulario(self):
        _, _, d = self.get("/dueno")
        self.assertIn(URL_NUBE.encode(), d)
        self.assertIn(APIKEY.encode(), d)

    def test_panel_configurado_inyecta_valores_y_elimina_placeholders(self):
        with patch.dict(os.environ, {
            "ZAPAPOS_URL_NUBE": URL_NUBE,
            "ZAPAPOS_APIKEY": APIKEY,
        }, clear=False):
            st, _, d = self.get("/dueno")
        self.assertEqual(st, 200)
        html = d.decode("utf-8")
        self.assertIn('data-configurado="1"', html)
        self.assertIn(URL_NUBE, html)
        self.assertIn(APIKEY, html)
        self.assertNotIn("{{URL_NUBE}}", html)
        self.assertNotIn("{{APIKEY}}", html)
        self.assertNotIn("{{CONFIGURADO}}", html)

    def test_panel_sin_configurar_devuelve_200_y_no_expone_formulario_activo(self):
        with patch.dict(os.environ, {
            "ZAPAPOS_URL_NUBE": "",
            "ZAPAPOS_APIKEY": "",
        }, clear=False):
            st, _, d = self.get("/dueno")
        self.assertEqual(st, 200)
        html = d.decode("utf-8")
        self.assertIn('data-configurado="0"', html)
        self.assertNotIn("sb_publishable_", html)
        self.assertNotIn("optnvredjvjibkkdhmxz", html)


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class Pantalla(ServidorBase):
    def pg(self, b, ls=None):
        ctx = b.new_context(viewport={"width": 390, "height": 844}, bypass_csp=True)
        if ls:
            ctx.add_init_script(f"localStorage.setItem('zapapos_cfg', JSON.stringify({json.dumps(ls)}))")
        pg = ctx.new_page(); pg.goto(f"{self.url}/dueno"); return pg

    def test_bienvenida_sin_tiendas_y_navega_a_configurar(self):
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = self.pg(b)
            pg.wait_for_selector("button.p")
            self.assertIn("Bienvenido", pg.inner_text("#main"))
            self.assertIn("primera tienda", pg.inner_text("button.p"))
            pg.click("button.p")
            pg.wait_for_selector("#cnombre")
            self.assertIn(URL_NUBE, pg.input_value("#curl"))
            self.assertIn(APIKEY[:20], pg.input_value("#capikey"))
            pg.screenshot(path="/tmp/dueno_test_config.png")
            b.close()

    def test_playwright_sin_configuracion_muestra_banner_y_tiendas_readonly(self):
        tienda = {"nombre": "Tienda guardada", "iid": "11111111-1111-1111-1111-111111111111",
                  "url": URL_NUBE, "apikey": APIKEY, "tok": "TOKEN-LOCAL"}
        with patch.dict(os.environ, {
            "ZAPAPOS_URL_NUBE": "",
            "ZAPAPOS_APIKEY": "",
        }, clear=False):
            with sync_playwright() as p:
                b = p.chromium.launch()
                pg = self.pg(b, [tienda])
                pg.click('[data-t="config"]')
                pg.wait_for_selector(".alerta")
                self.assertIn("Panel no configurado", pg.inner_text("#main"))
                self.assertFalse(pg.is_visible("#cnombre"))
                self.assertIn("Tienda guardada", pg.inner_text("#main"))
                self.assertFalse(pg.locator("button:has-text('✕')").count())
                b.close()

    def test_playwright_configurado_muestra_formulario_readonly(self):
        with patch.dict(os.environ, {
            "ZAPAPOS_URL_NUBE": URL_NUBE,
            "ZAPAPOS_APIKEY": APIKEY,
        }, clear=False):
            with sync_playwright() as p:
                b = p.chromium.launch()
                pg = self.pg(b)
                # button.p es el botón de bienvenida ("Agregar primera tienda").
                # Si el HTML de bienvenida cambia ese selector, actualizar acá.
                # Depende del HTML de bienvenida, no del panel configurado.
                pg.click("button.p")
                pg.wait_for_selector("#cnombre")
                self.assertNotIn("Panel no configurado", pg.inner_text("#main"))
                self.assertTrue(pg.is_visible("#cnombre"))
                self.assertEqual(pg.input_value("#curl"), URL_NUBE)
                self.assertEqual(pg.input_value("#capikey"), APIKEY)
                self.assertFalse(pg.locator("#curl").is_editable())
                self.assertFalse(pg.locator("#capikey").is_editable())
                pg.click("button:has-text('Agregar tienda')")
                self.assertIn("Complete todos los campos", pg.inner_text("#cerr"))
                b.close()

    def test_con_tienda_muestra_nav_y_carga_la_pestaña(self):
        tienda = {"nombre": "Mi Tienda", "iid": "11111111-1111-1111-1111-111111111111",
                  "url": URL_NUBE, "apikey": APIKEY, "tok": "TOKEN-SIN-ACCESO"}
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = self.pg(b, [tienda])
            pg.wait_for_selector("nav")
            self.assertTrue(pg.is_visible("nav"))
            self.assertIn("Mi Tienda", pg.inner_text("header"))
            # la pestaña Tiendas muestra la tienda registrada
            pg.click('[data-t="tiendas"]')
            pg.wait_for_timeout(200)
            self.assertIn("Mi Tienda", pg.inner_text("#main"))
            # la pestaña Configurar tiene el formulario de alta con los datos precompletados
            pg.click('[data-t="config"]'); pg.wait_for_selector("#cnombre")
            self.assertIn(URL_NUBE, pg.input_value("#curl"))
            # botón de borrar tienda existe
            self.assertTrue(pg.is_visible("button:has-text('✕')"))
            pg.screenshot(path="/tmp/dueno_test_tiendas.png")
            b.close()

    def test_error_de_nube_se_muestra_con_claridad_y_tiene_reintentar(self):
        tienda = {"nombre": "T1", "iid": "00000000-0000-0000-0000-000000000000",
                  "url": URL_NUBE, "apikey": APIKEY, "tok": "TOKEN-INVALIDO"}
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = self.pg(b, [tienda])
            pg.wait_for_selector(".alerta, .ok-msg", timeout=12000)
            self.assertFalse(pg.is_visible(".ok-msg"))           # con token inválido no debe mostrar OK
            self.assertTrue(pg.is_visible("button:has-text('Reintentar')"))
            pg.screenshot(path="/tmp/dueno_test_error.png")
            b.close()


    def test_asistente_chat_con_historial_errores_y_sin_inyeccion_html(self):
        tienda = {"nombre": "T1", "iid": "11111111-1111-1111-1111-111111111111", "url": URL_NUBE, "apikey": APIKEY, "tok": "TOK"}
        pedidos = []
        respuestas = [
            (200, {"respuesta": "Reponga **talla 38 negro**.\n<img src=x onerror=alert(1)>", "restantes": 3}),
            (429, {"error": "Alcanzó el límite de 30 consultas en 24 horas"}),
        ]
        def falso(route):
            pedidos.append(json.loads(route.request.post_data))
            estado, cuerpo = respuestas.pop(0)
            route.fulfill(status=estado, content_type="application/json", headers={"Access-Control-Allow-Origin": "*"}, body=json.dumps(cuerpo))
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": 390, "height": 844}, bypass_csp=True)
            ctx.add_init_script(f"localStorage.setItem('zapapos_cfg', JSON.stringify([{json.dumps(tienda)}]))")
            pg = ctx.new_page(); pg.route("**/functions/v1/asistente", falso); pg.goto(f"{self.url}/dueno")
            pg.wait_for_selector("nav"); pg.click('[data-t="ia"]'); pg.wait_for_selector("#pq")
            self.assertEqual(pg.locator(".chips button").count(), 4)                          # sugerencias iniciales
            pg.click(".chips button >> nth=0")                                                # pregunta rápida
            pg.wait_for_selector(".burb.a")
            self.assertIn("talla 38 negro", pg.inner_text(".burb.a"))
            self.assertEqual(pg.locator(".burb.a b").count(), 1)                              # **negrita** se renderiza
            self.assertEqual(pg.locator(".burb.a img").count(), 0)                            # el HTML del modelo NO se ejecuta
            self.assertIn("<img", pg.inner_text(".burb.a"))
            self.assertEqual(pg.locator(".chips button").count(), 0)                          # tras empezar, ya no se muestran
            self.assertEqual((pedidos[0]["instalacion"], pedidos[0]["token"], pedidos[0]["pregunta"], pedidos[0]["historial"]), (tienda["iid"], "TOK", "¿Qué tallas debo reponer?", []))
            pg.fill("#pq", "¿Y de las botas?"); pg.press("#pq", "Enter")
            pg.wait_for_selector(".burb.e")
            self.assertIn("límite de 30", pg.inner_text(".burb.e"))
            self.assertEqual([h["rol"] for h in pedidos[1]["historial"]], ["usuario", "asistente"])  # la 2.ª pregunta lleva la conversación previa
            self.assertEqual(pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), True)
            pg.screenshot(path="/tmp/dueno_asistente.png")
            b.close()

    def test_asistente_sin_red_muestra_error_claro(self):
        tienda = {"nombre": "T1", "iid": "11111111-1111-1111-1111-111111111111", "url": URL_NUBE, "apikey": APIKEY, "tok": "TOK"}
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": 390, "height": 844}, bypass_csp=True)
            ctx.add_init_script(f"localStorage.setItem('zapapos_cfg', JSON.stringify([{json.dumps(tienda)}]))")
            pg = ctx.new_page(); pg.route("**/functions/v1/asistente", lambda r: r.abort()); pg.goto(f"{self.url}/dueno")
            pg.wait_for_selector("nav"); pg.click('[data-t="ia"]'); pg.fill("#pq", "hola"); pg.click("#pbtn")
            pg.wait_for_selector(".burb.e"); self.assertTrue(pg.inner_text(".burb.e").strip())
            self.assertFalse(pg.is_disabled("#pbtn"))                                         # se puede reintentar
            b.close()

    def test_sin_scroll_horizontal_en_celular(self):
        tienda = {"nombre": "T", "iid": "x", "url": URL_NUBE, "apikey": APIKEY, "tok": "T"}
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = self.pg(b, [tienda]); pg.wait_for_timeout(500)
            self.assertEqual(pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), True)
            b.close()


if __name__ == "__main__":
    unittest.main()
