import base64, http.client, json, os, shutil, subprocess, sys, tempfile, threading, unittest
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None
from app.server import crear_servidor
from core import auth, licencia as LIC, negocio as N
from core.licencia import LicenciaError
from tools import actualizar as ACT, respaldo as RS
from tools.db import connect, migrate
import tests.test_actualizar as TA

ALTA = dict(negocio="Zapatería Sol", rif="J-12345678-9", direccion="Av 1", telefono="0414-1", dueno_nombre="Marta", dueno_pin="4821",
            cajero_nombre="Luis", cajero_pin="7305")
Z = lambda *a: datetime(*a, 12, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); (self.d / "datos").mkdir()
        self.priv, self.pub = ACT.generar_claves(self.d / "claves")
        shutil.copy(self.pub, self.d / "clave_publica.pem")
        self.db = str(self.d / "datos" / "zapateria.db")
        self.con = connect(self.db); migrate(self.con)
        self.iid = N.configurar_negocio(self.con, **ALTA)["instalacion_id"]
        self.addCleanup(setattr, LIC, "CLAVE_PUBLICA", None)

    def tearDown(self):
        self.con.close(); shutil.rmtree(self.d, ignore_errors=True)

    def lic(self, plan="basico", vence="2027-01-31", **k):
        return LIC.emitir(self.priv, self.iid, "Calzados Sol", plan, vence, **k)

    def poner(self, ahora=Z(2026, 10, 10), **k):
        return LIC.instalar(self.con, json.dumps(self.lic(**k)), self.pub, ahora)

    def est(self, ahora):
        return LIC.estado(self.con, self.pub, ahora)


class Unidad(Base):
    def test_emitir_y_verificar(self):
        p = LIC.verificar(self.lic("pro", sucursales_max=5, funciones=["x"]), self.pub, self.iid)
        self.assertEqual((p["plan"], p["sucursales_max"], p["funciones"], p["cliente"], p["instalacion_id"]), ("pro", 5, ["x"], "Calzados Sol", self.iid))

    def test_rechaza_licencias_no_validas(self):
        otra_priv, _ = ACT.generar_claves(self.d / "otra")
        ok = self.lic()
        alterada = json.loads(json.dumps(ok)); alterada["payload"]["vence"] = "2099-12-31"
        sin_prefijo = json.loads(json.dumps(ok))                                   # firma válida pero SIN el prefijo propio de licencias
        from cryptography.hazmat.primitives import serialization
        k = serialization.load_pem_private_key(Path(self.priv).read_bytes(), None)
        sin_prefijo["firma"] = base64.b64encode(k.sign(json.dumps(ok["payload"], sort_keys=True, ensure_ascii=False).encode())).decode()
        incompleta = {"payload": {"plan": "pro"}, "firma": ok["firma"]}
        casos = {"otra clave": (LIC.emitir(otra_priv, self.iid, "X", "pro", "2027-01-01"), "firma no corresponde"),
                 "alterada": (alterada, "firma no corresponde"), "sin prefijo": (sin_prefijo, "firma no corresponde"),
                 "otra instalación": (LIC.emitir(self.priv, "OTRO-ID", "X", "pro", "2027-01-01"), "otra instalación"),
                 "incompleta": (incompleta, "incompleta|dañada|firma"), "sin firma": ({"payload": ok["payload"]}, "incompleta")}
        for nombre, (lic, msg) in casos.items():
            with self.assertRaisesRegex(LicenciaError, msg, msg=nombre):
                LIC.verificar(lic, self.pub, self.iid)
        for malo in ("", "no es json", "[1]", "{}"):
            with self.assertRaises(LicenciaError, msg=malo):
                LIC.instalar(self.con, malo, self.pub)
        for args in (("x", "J", "enorme", "2027-01-01"), (self.iid, "J", "pro", "31/12/2027"), ("", "J", "pro", "2027-01-01"), (self.iid, " ", "pro", "2027-01-01")):
            with self.assertRaises(LicenciaError):
                LIC.emitir(self.priv, *args)
        with self.assertRaisesRegex(LicenciaError, "clave pública"):
            LIC.verificar(ok, None, self.iid)

    def test_periodo_de_prueba_de_30_dias(self):
        e = self.est(Z(2026, 10, 10))
        self.assertEqual((e["estado"], e["dias_restantes"], e["puede_actualizar"], e["alerta"]), ("prueba", 30, True, False))
        self.assertEqual(self.est(Z(2026, 10, 25))["dias_restantes"], 15)
        e = self.est(Z(2026, 11, 5)); self.assertEqual((e["estado"], e["dias_restantes"], e["alerta"]), ("prueba", 4, True))
        e = self.est(Z(2026, 11, 20))
        self.assertEqual((e["estado"], e["puede_actualizar"]), ("vencida", False)); self.assertIn("seguir vendiendo", e["mensaje"])

    def test_ciclo_de_vida_de_la_licencia(self):
        self.est(Z(2026, 10, 10)); self.poner(vence="2027-01-31")
        e = self.est(Z(2026, 10, 10)); self.assertEqual((e["estado"], e["dias_restantes"], e["alerta"], e["plan"], e["cliente"]), ("activa", 113, False, "basico", "Calzados Sol"))
        e = self.est(Z(2027, 1, 20)); self.assertEqual((e["estado"], e["alerta"]), ("activa", True)); self.assertIn("vence en 11", e["mensaje"])
        e = self.est(Z(2027, 2, 15)); self.assertEqual((e["estado"], e["puede_actualizar"]), ("gracia", True)); self.assertIn("renueve", e["mensaje"])
        e = self.est(Z(2027, 3, 5)); self.assertEqual((e["estado"], e["puede_actualizar"]), ("vencida", False)); self.assertIn("seguir vendiendo", e["mensaje"])
        self.poner(vence="2028-01-31", ahora=Z(2027, 3, 5))                               # renovar = instalar la nueva
        self.assertEqual(self.est(Z(2027, 3, 5))["estado"], "activa")

    def test_lo_esencial_nunca_se_bloquea(self):
        casos = []
        self.est(Z(2026, 10, 10)); casos.append(Z(2026, 12, 25))                           # prueba terminada
        for ahora in casos:
            self.assertEqual(self.est(ahora)["estado"], "vencida")
            for f in LIC.SIEMPRE:
                self.assertTrue(LIC.permite(self.con, f, self.pub, ahora), f)
            self.assertFalse(LIC.permite(self.con, "nube", self.pub, ahora))
        self.poner(vence="2027-01-31")
        self.assertEqual(self.est(Z(2027, 6, 1))["estado"], "vencida")                     # licencia vencida
        self.assertTrue(all(LIC.permite(self.con, f, self.pub, Z(2027, 6, 1)) for f in LIC.SIEMPRE))
        self.con.execute("UPDATE config SET valor=? WHERE clave='licencia'", ('{"payload": {}, "firma": "x"}',))   # licencia dañada
        self.assertEqual(self.est(Z(2027, 6, 2))["estado"], "invalida")
        self.assertTrue(all(LIC.permite(self.con, f, self.pub, Z(2027, 6, 2)) for f in LIC.SIEMPRE))
        self.poner(vence="2028-01-01", ahora=Z(2027, 6, 3))
        self.assertEqual(LIC.estado(self.con, None, Z(2027, 6, 3))["estado"], "sin_clave")      # sin clave pública: no se puede verificar, no se castiga
        self.assertTrue(LIC.permite(self.con, "ventas", None))

    def test_funciones_segun_el_plan(self):
        self.poner(plan="basico"); a = Z(2026, 10, 10)
        self.assertFalse(LIC.permite(self.con, "nube", self.pub, a))
        self.poner(plan="basico", funciones=["whatsapp"]); self.assertTrue(LIC.permite(self.con, "whatsapp", self.pub, a)); self.assertFalse(LIC.permite(self.con, "nube", self.pub, a))
        self.poner(plan="pro")
        self.assertTrue(all(LIC.permite(self.con, f, self.pub, a) for f in ("nube", "app_dueno", "whatsapp", "asistente_ia", "conciliacion")))
        self.assertFalse(LIC.permite(self.con, "inventada", self.pub, a))
        self.assertTrue(LIC.permite(self.con, "nube", self.pub, Z(2027, 2, 10)))           # en gracia aún funciona
        self.assertFalse(LIC.permite(self.con, "nube", self.pub, Z(2027, 4, 10)))          # vencida: las Pro se detienen

    def test_prueba_permite_funciones_pro_para_que_las_conozca(self):
        self.assertTrue(LIC.permite(self.con, "nube", self.pub, Z(2026, 10, 10)))

    def test_la_licencia_viaja_con_el_respaldo(self):
        self.poner(plan="pro")
        z = RS.respaldar(self.db, str(self.d / "resp"))["archivo"]
        nuevo = self.d / "pc_nueva.db"; RS.restaurar(z, str(nuevo))
        c = connect(nuevo); e = LIC.estado(c, self.pub, Z(2026, 10, 10)); c.close()
        self.assertEqual((e["estado"], e["plan"]), ("activa", "pro"))

    def test_reloj_atrasado_no_regala_tiempo(self):
        self.poner(vence="2027-01-31")
        self.assertEqual(self.est(Z(2027, 3, 5))["estado"], "vencida")
        e = self.est(Z(2026, 10, 1))                                                       # atrasan el reloj de la PC
        self.assertEqual((e["estado"], e["reloj_atrasado"]), ("vencida", True))
        self.assertEqual(self.est(Z(2027, 3, 4))["reloj_atrasado"], False)                  # 1 día de diferencia se tolera

    def test_limite_de_sucursales_por_plan(self):
        LIC.CLAVE_PUBLICA = self.pub
        dueno = auth.login(self.con, 1, "Marta", "4821")
        with self.assertRaisesRegex(N.NegocioError, "permite 1 sucursal"):                  # prueba
            N.crear_sucursal(self.con, dueno, "S2", "Segunda")
        self.poner(plan="basico")
        with self.assertRaisesRegex(N.NegocioError, "permite 1 sucursal"):
            N.crear_sucursal(self.con, dueno, "S2", "Segunda")
        self.poner(plan="pro")
        N.crear_sucursal(self.con, dueno, "S2", "Segunda"); N.crear_sucursal(self.con, dueno, "S3", "Tercera")
        with self.assertRaisesRegex(N.NegocioError, "permite 3 sucursal"):
            N.crear_sucursal(self.con, dueno, "S4", "Cuarta")
        self.poner(plan="pro", sucursales_max=5, vence="2099-01-01"); N.crear_sucursal(self.con, dueno, "S4", "Cuarta")

    def test_actualizar_exige_mantenimiento_vigente_pero_soporte_puede_forzar(self):
        inst = self.d / "inst"; TA.copiar_codigo(inst / "versiones" / "1.0.0"); (inst / "datos").mkdir(parents=True)
        (inst / "version_actual.txt").write_text("1.0.0"); shutil.copy(self.pub, inst / "clave_publica.pem")
        db = inst / "datos" / "zapateria.db"
        import sqlite3
        o, d_ = sqlite3.connect(self.db), sqlite3.connect(db); o.backup(d_); o.close(); d_.close()     # copia consistente (la base está en modo WAL)
        c = connect(db); iid = c.execute("SELECT valor FROM config WHERE clave='instalacion_id'").fetchone()[0]
        LIC.instalar(c, json.dumps(LIC.emitir(self.priv, iid, "Sol", "basico", "2026-01-01")), self.pub, Z(2025, 12, 1)); c.close()
        def paquete(v):
            t = self.d / f"o{v}"; TA.copiar_codigo(t); (t / "app" / "version.py").write_text(f'VERSION = "{v}"\n')
            return ACT.empaquetar(t, self.d / f"p{v}.zip", v, self.priv)
        ok = lambda *a: True
        ACT.aplicar(paquete("1.1.0"), inst, prueba=ok, ahora=Z(2025, 12, 1))                       # mantenimiento vigente: se actualiza sin más
        self.assertEqual(ACT.version_actual(inst), "1.1.0")
        p12 = paquete("1.2.0")
        with self.assertRaisesRegex(ACT.ActualizacionError, "No se aplicó.*vencido"):             # vencido: no hay actualizaciones
            ACT.aplicar(p12, inst, prueba=ok, ahora=Z(2026, 10, 10))
        self.assertEqual(ACT.version_actual(inst), "1.1.0")
        ACT.aplicar(p12, inst, prueba=ok, ahora=Z(2026, 10, 10), forzar=True)        # soporte puede forzarlo
        self.assertEqual(ACT.version_actual(inst), "1.2.0")

    def test_linea_de_comandos(self):
        py = lambda *a: subprocess.run([sys.executable, "-m", "core.licencia", *a], cwd=ROOT, capture_output=True, text=True)
        out = str(self.d / "lic.json")
        r = py("emitir", self.priv, out, "--instalacion", self.iid, "--cliente", "Calzados Sol", "--plan", "pro", "--vence", "2027-12-31", "--sucursales", "4")
        self.assertEqual(r.returncode, 0, r.stderr)
        LIC.instalar(self.con, Path(out).read_text(), self.pub)
        self.assertEqual((self.est(Z(2026, 10, 10))["plan"], self.est(Z(2026, 10, 10))["sucursales_max"]), ("pro", 4))
        self.assertIn("2027-12-31", py("ver", out).stdout)
        self.assertEqual(py("emitir", self.priv, out, "--instalacion", self.iid, "--cliente", "X", "--plan", "raro", "--vence", "2027-12-31").returncode, 1)
        self.assertEqual(py().returncode, 2)


class Servidor(Base):
    def setUp(self):
        super().setUp()
        self.con.close()
        self.srv = crear_servidor(self.db, 0); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.con = connect(self.db)

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def req(self, m, ruta, cuerpo=None, tok=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
        c.request(m, ruta, json.dumps(cuerpo) if cuerpo is not None else None, {"X-Token": tok or "", "Content-Type": "application/json"})
        r = c.getresponse(); d = r.read(); c.close(); return r.status, json.loads(d)

    def entrar(self, n, p):
        return self.req("POST", "/api/login", {"nombre": n, "pin": p})[1]["token"]

    def test_endpoints_de_licencia(self):
        dueno, luis = self.entrar("Marta", "4821"), self.entrar("Luis", "7305")
        self.assertEqual((self.req("GET", "/api/licencia", tok=luis)[0], self.req("POST", "/api/licencia", {"texto": "x"}, luis)[0]), (403, 403))
        self.assertIsNone(self.req("GET", "/api/estado", tok=luis)[1]["licencia"])
        st, e = self.req("GET", "/api/licencia", tok=dueno)
        self.assertEqual((st, e["estado"], e["instalacion_id"]), (200, "prueba", self.iid))
        self.assertEqual(self.req("POST", "/api/licencia", {"texto": "no es licencia"}, dueno)[0], 400)
        otra = LIC.emitir(self.priv, "OTRO", "X", "pro", "2027-01-01")
        st, r = self.req("POST", "/api/licencia", {"texto": json.dumps(otra)}, dueno)
        self.assertEqual((st, "otra instalación" in r["error"]), (400, True))
        st, e = self.req("POST", "/api/licencia", {"texto": json.dumps(self.lic("pro", "2099-01-01"))}, dueno)
        self.assertEqual((st, e["estado"], e["plan"]), (200, "activa", "pro"))
        self.assertEqual(self.req("GET", "/api/estado", tok=dueno)[1]["licencia"]["estado"], "activa")


@unittest.skipUnless(sync_playwright, "playwright no instalado")
class Pantalla(Servidor):
    def test_activar_licencia_en_pantalla(self):
        with sync_playwright() as p:
            b = p.chromium.launch(); pg = b.new_context(viewport={"width": 1100, "height": 900}, bypass_csp=True).new_page()
            pg.goto(f"http://127.0.0.1:{self.srv.server_port}/"); pg.fill("#nom", "Marta")
            for d in "4821":
                pg.click(f'.pin button[data-d="{d}"]')
            pg.click('.pin button[data-d="ok"]'); pg.wait_for_selector("#scan")
            pg.click("#mas"); pg.click('button[data-o="lic"]'); pg.wait_for_selector("#lic")
            self.assertIn("Período de prueba", pg.inner_text("#modal")); self.assertIn(self.iid, pg.inner_text("#modal"))
            pg.fill("#lic", "esto no es una licencia"); pg.click("#act"); pg.wait_for_selector("#fe .err")
            pg.fill("#lic", json.dumps(self.lic("pro", "2099-01-01"))); pg.click("#act")
            pg.wait_for_function("document.querySelector('#modal').textContent.includes('Mantenimiento al día')")
            self.assertIn("Plan: pro", pg.inner_text("#modal")); pg.screenshot(path="/tmp/ui_licencia.png")
            b.close()


if __name__ == "__main__":
    unittest.main()
