import os, sys, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from playwright.sync_api import sync_playwright
except ImportError:                                  # sin navegador de pruebas: se omite
    sync_playwright = None
from app.server import crear_servidor
from core import ticket as T
from tests.test_reportes import Base


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class UI(Base):
    def setUp(self):
        super().setUp()
        self.ruta = os.path.join(self.d.name, "t.bin")
        self.con.execute("INSERT INTO config(clave,valor) VALUES('impresora_ticket',?)", (f"archivo:{self.ruta}",))
        self.srv = crear_servidor(os.path.join(self.d.name, "r.db"), 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/"

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        super().tearDown()

    def entrar(self, pg, nombre, pin):
        pg.goto(self.url)
        pg.fill("#nom", nombre)
        for d in pin:
            pg.click(f'.pin button[data-d="{d}"]')
        pg.click('.pin button[data-d="ok"]')
        pg.wait_for_selector("#scan")

    def test_venta_completa_en_pantalla(self):
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_context(viewport={"width": 1280, "height": 760}, bypass_csp=True).new_page()
            errores = []; pg.on("pageerror", lambda e: errores.append(str(e)))
            pg.goto(self.url); pg.fill("#nom", "Luis")
            for d in "0000":                                             # PIN malo: mensaje claro, sin entrar
                pg.click(f'.pin button[data-d="{d}"]')
            pg.click('.pin button[data-d="ok"]')
            pg.wait_for_selector("#le .err"); self.assertIn("incorrecto", pg.inner_text("#le"))
            for d in "7305":
                pg.click(f'.pin button[data-d="{d}"]')
            pg.click('.pin button[data-d="ok"]'); pg.wait_for_selector("#scan")
            self.assertIn("BCV 40,50", pg.inner_text("header"))
            self.assertTrue(pg.evaluate("document.activeElement.id") == "scan")           # el lector escribe directo
            pg.fill("#scan", "gamo-flor-38-neg"); pg.press("#scan", "Enter")
            pg.wait_for_selector(".linea")
            self.assertIn("Gamo Flor", pg.inner_text("#cart")); self.assertIn("$17,39", pg.inner_text(".gran"))
            pg.click('button[data-q="2"]'); pg.wait_for_function("document.querySelector('.qty b').textContent=='2'")
            pg.click('button[data-q="1"]'); pg.wait_for_function("document.querySelector('.qty b').textContent=='1'")
            pg.screenshot(path="/tmp/ui_caja.png")
            pg.click("#cobrar"); pg.wait_for_selector("#modal.on")
            pg.click('.mets button[data-m="zelle"]'); pg.fill("#mon", "5,00"); pg.fill("#ref", "ZEL12345"); pg.click("#reg")
            pg.wait_for_selector('button[data-c]')
            self.assertTrue(pg.is_disabled("#fin"))                                         # falta cobrar y confirmar
            pg.click('.mets button[data-m="efectivo_usd"]')
            self.assertEqual(pg.input_value("#mon"), "12,39")                               # sugiere lo que falta
            pg.click("#reg"); pg.wait_for_selector('.pago:nth-of-type(2)')
            self.assertTrue(pg.is_disabled("#fin"))                                         # aún sin confirmar el Zelle
            pg.screenshot(path="/tmp/ui_cobro.png")
            pg.click('button[data-c]'); pg.wait_for_function("!document.querySelector('#fin').disabled")
            pg.click("#fin"); pg.wait_for_selector("text=Venta cerrada")
            self.assertIn("Se abrió el cajón", pg.inner_text("#modal"))
            pg.click("#nv"); pg.wait_for_selector(".vacio")
            self.assertEqual(errores, [])
            b.close()
        with open(self.ruta, "rb") as f:
            datos = f.read()
        self.assertTrue(datos.endswith(T.CAJON)); self.assertIn("$17,39", datos.decode("cp858"))

    def test_vuelto_y_tasa_faltante(self):
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_context(viewport={"width": 1280, "height": 760}, bypass_csp=True).new_page()
            self.entrar(pg, "Luis", "7305")
            pg.fill("#scan", "GAMO-FLOR-38-NEG"); pg.press("#scan", "Enter"); pg.wait_for_selector(".linea")
            pg.click("#cobrar"); pg.click('.mets button[data-m="efectivo_usd"]'); pg.fill("#mon", "20"); pg.click("#reg")
            pg.wait_for_selector(".aviso b"); self.assertIn("Entregue vuelto: $2,61", pg.inner_text(".aviso"))
            self.assertTrue(pg.is_disabled("#fin"))
            pg.select_option("#vm", "efectivo_bs"); pg.fill("#vv", "105,71"); pg.click("#vr")
            pg.wait_for_function("!document.querySelector('#fin').disabled")
            b.close()

    def test_cajero_no_ve_errores_tecnicos_y_pide_pin_de_dueno(self):
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_context(viewport={"width": 1280, "height": 760}, bypass_csp=True).new_page()
            self.entrar(pg, "Luis", "7305")
            pg.fill("#scan", "NOEXISTE"); pg.press("#scan", "Enter")
            pg.wait_for_selector("#toast .err"); self.assertIn("no encontrado", pg.inner_text("#toast"))
            pg.click("#mas"); pg.click('button[data-o="cajon"]'); pg.wait_for_selector("#an")   # el cajón pide PIN del dueño
            pg.fill("#an", "Marta"); pg.fill("#ap", "4821"); pg.click("#aok")
            pg.wait_for_selector("#toast .aviso"); self.assertIn("Cajón abierto", pg.inner_text("#toast"))
            b.close()

    def test_sin_tasa_avisa(self):
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_context(viewport={"width": 390, "height": 800}, bypass_csp=True).new_page()     # celular/tablet pequeña
            self.entrar(pg, "Marta", "4821"); pg.screenshot(path="/tmp/ui_movil.png")
            pg.click("#mas"); pg.click('button[data-o="tas"]'); pg.fill("#f_valor", "41,25"); pg.click("#go")
            pg.wait_for_function("document.querySelector('header').textContent.includes('41,25')")
            self.assertEqual(pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), True)  # sin scroll lateral
            b.close()


if __name__ == "__main__":
    unittest.main()
