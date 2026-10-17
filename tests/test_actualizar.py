import importlib.util, json, os, shutil, sqlite3, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import actualizar as ACT, respaldo as RS
from tools.actualizar import ActualizacionError
from tools.db import connect, migrate
from core import negocio as N


def tablas(db):
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def copiar_codigo(destino):
    for d in ("app", "core", "tools", "database"):
        shutil.copytree(ROOT / d, Path(destino) / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


class A(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.n = 0
        self.priv, self.pub = ACT.generar_claves(self.d / "claves")
        self.inst = self.d / "ZapaPOS"
        copiar_codigo(self.inst / "versiones" / "1.0.0")
        (self.inst / "datos").mkdir(); (self.inst / "version_actual.txt").write_text("1.0.0\n"); shutil.copy(self.pub, self.inst / "clave_publica.pem")
        self.db = self.inst / "datos" / "zapateria.db"
        c = connect(self.db); migrate(c)
        N.configurar_negocio(c, "Zapatería Demo", "J-12345678-9", "Av 1", "0414-1", "Marta", "4821", "Luis", "7305"); c.close()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def paquete(self, version, migraciones=None, cambios=None, min_version=None, clave=None):
        self.n += 1
        t = self.d / f"origen_{self.n}"; copiar_codigo(t)
        (t / "app" / "version.py").write_text(f'VERSION = "{version}"\n')
        for nombre, sql in (migraciones or {}).items():
            (t / "database" / "migrations" / nombre).write_text(sql)
        for ruta, txt in (cambios or {}).items():
            (t / ruta).write_text(txt)
        return ACT.empaquetar(t, self.d / f"p{self.n}.zip", version, clave or self.priv, min_version, "notas de prueba")

    DEMO = {"008_demo.sql": "CREATE TABLE nota_demo(id INTEGER PRIMARY KEY, texto TEXT); INSERT INTO nota_demo(texto) VALUES('hola');"}

    def forjar(self, archivos, clave=None, extra_zip=None):
        man = {"version": "9.9.9", "min_version": None, "fecha": "x", "notas": "", "archivos": {n: ACT._sha(b) for n, b in archivos.items()}}
        mb = json.dumps(man, sort_keys=True).encode()
        import base64
        from cryptography.hazmat.primitives import serialization
        k = serialization.load_pem_private_key(Path(clave or self.priv).read_bytes(), None)
        self.n += 1; ruta = self.d / f"forjado_{self.n}.zip"
        with zipfile.ZipFile(ruta, "w") as z:
            for n, b in {**archivos, **(extra_zip or {})}.items():
                z.writestr(n, b)
            z.writestr("manifest.json", mb); z.writestr("manifest.sig", base64.b64encode(k.sign(mb)))
        return ruta

    # ---------- paquetes ----------
    def test_paquete_firmado_se_verifica(self):
        m = ACT.verificar_paquete(self.paquete("1.1.0"), self.pub)
        self.assertEqual((m["version"], "app/server.py" in m["archivos"], m["notas"]), ("1.1.0", True, "notas de prueba"))
        self.assertFalse(any("__pycache__" in n for n in m["archivos"]))
        for mala in ("1.0", "uno", "1.0.0.0"):
            with self.assertRaises(ActualizacionError):
                ACT.empaquetar(self.d / "origen_1", self.d / "x.zip", mala, self.priv)

    def test_rechaza_paquetes_no_confiables(self):
        ok = self.paquete("1.1.0")
        _, otra_pub = ACT.generar_claves(self.d / "otras")
        casos = {"firma de otra clave": (ok, otra_pub, "Firma inválida")}
        with zipfile.ZipFile(ok) as z:
            datos = {n: z.read(n) for n in z.namelist()}
        man_alterado = self.d / "man.zip"
        with zipfile.ZipFile(man_alterado, "w") as z:
            for n, b in datos.items():
                z.writestr(n, b.replace(b'"1.1.0"', b'"1.1.1"') if n == "manifest.json" else b)
        casos["manifiesto alterado"] = (man_alterado, self.pub, "Firma inválida")
        arch_alt = self.d / "alt.zip"
        with zipfile.ZipFile(arch_alt, "w") as z:
            for n, b in datos.items():
                z.writestr(n, b + b"# virus" if n == "core/pos.py" else b)
        casos["archivo alterado"] = (arch_alt, self.pub, "alterado")
        casos["archivo extra"] = (self.forjar({"app/a.py": b"x = 1"}, extra_zip={"core/extra.py": b"y"}), self.pub, "no están en su manifiesto")
        casos["ruta con .."] = (self.forjar({"app/../x.py": b"x"}), self.pub, "Ruta no permitida")
        casos["carpeta no permitida"] = (self.forjar({"evil/x.py": b"x"}), self.pub, "Ruta no permitida")
        casos["ruta absoluta"] = (self.forjar({"/etc/x.py": b"x"}), self.pub, "Ruta no permitida")
        for nombre, (zipf, clave, msg) in casos.items():
            with self.assertRaisesRegex(ActualizacionError, msg, msg=nombre):
                ACT.verificar_paquete(zipf, clave)
        roto, basura = self.d / "roto.zip", self.d / "basura.zip"
        roto.write_bytes(Path(ok).read_bytes()[:500]); basura.write_bytes(b"nada")
        for malo in (roto, basura, self.d / "no_existe.zip"):
            with self.assertRaises(ActualizacionError):
                ACT.verificar_paquete(malo, self.pub)
        self.assertEqual(ACT.version_actual(self.inst), "1.0.0")                      # nada se tocó

    # ---------- aplicar ----------
    def test_actualizacion_completa_conserva_los_datos_y_cambia_de_version(self):
        r = ACT.aplicar(self.paquete("1.1.0", self.DEMO), self.inst)
        self.assertEqual((r["desde"], r["hasta"], ACT.version_actual(self.inst)), ("1.0.0", "1.1.0", "1.1.0"))
        self.assertIn("nota_demo", tablas(self.db))
        c = sqlite3.connect(self.db)
        self.assertEqual((c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], c.execute("PRAGMA integrity_check").fetchone()[0]), (2, "ok"))
        self.assertEqual(c.execute("SELECT texto FROM nota_demo").fetchone()[0], "hola"); c.close()
        self.assertEqual(RS.verificar_respaldo(r["respaldo"])["filas"]["usuarios"], 2)               # respaldo previo válido
        self.assertEqual(sorted(p.name for p in (self.inst / "versiones").iterdir()), ["1.0.0", "1.1.0"])
        h = ACT.historial(self.inst); self.assertEqual((len(h), h[0]["desde"], h[0]["hasta"]), (1, "1.0.0", "1.1.0"))
        spec = importlib.util.spec_from_file_location("iniciar", ROOT / "instalador" / "iniciar.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        cmd, cwd = m.comando_arranque(self.inst)
        self.assertEqual((Path(cwd).name, cmd[-1], cmd[cmd.index("--db") + 1]), ("1.1.0", "8787", str(self.db)))     # el lanzador usa la nueva
        self.assertTrue(ACT.prueba_de_arranque(cwd, self.db.parent / "copia_prueba.db"))

    def test_no_aplica_misma_menor_ni_incompatible(self):
        with self.assertRaisesRegex(ActualizacionError, "Ya tiene"):
            ACT.aplicar(self.paquete("1.0.0"), self.inst)
        with self.assertRaisesRegex(ActualizacionError, "requiere"):
            ACT.aplicar(self.paquete("1.1.0", min_version="1.0.5"), self.inst)
        ACT.aplicar(self.paquete("1.1.0", self.DEMO), self.inst)
        with self.assertRaisesRegex(ActualizacionError, "Ya tiene"):
            ACT.aplicar(self.paquete("1.0.5"), self.inst)
        with self.assertRaisesRegex(ActualizacionError, "firma|Firma"):
            ACT.aplicar(self.paquete("1.2.0", clave=ACT.generar_claves(self.d / "k2")[0]), self.inst)

    def assertIntacto(self):
        self.assertEqual(ACT.version_actual(self.inst), "1.0.0")
        self.assertNotIn("nota_demo", tablas(self.db))
        self.assertEqual([p.name for p in (self.inst / "versiones").iterdir()], ["1.0.0"])          # sin carpetas a medias
        self.assertEqual(ACT.historial(self.inst), [])

    def test_migracion_mala_se_detecta_en_el_ensayo_sin_tocar_la_base_real(self):
        with self.assertRaisesRegex(ActualizacionError, "migración de datos falló"):
            ACT.aplicar(self.paquete("1.1.0", {"008_roto.sql": "CREATE TABLE x(;"}), self.inst)
        self.assertIntacto()
        self.assertFalse((self.inst / "datos" / "respaldos" / "antes_de_actualizar").exists())      # ni siquiera llegó a respaldar

    def test_codigo_con_error_o_que_no_arranca_se_rechaza(self):
        with self.assertRaisesRegex(ActualizacionError, "sintaxis"):
            ACT.aplicar(self.paquete("1.1.0", self.DEMO, {"core/pos.py": "def roto(:\n"}), self.inst)
        self.assertIntacto()
        with self.assertRaisesRegex(ActualizacionError, "no arrancó"):
            ACT.aplicar(self.paquete("1.1.1", self.DEMO, {"app/server.py": "raise SystemExit('roto a propósito')\n"}), self.inst)
        self.assertIntacto()

    def test_si_falla_el_cambio_de_version_la_base_vuelve_a_como_estaba(self):
        original = ACT._puntero
        def falla(inst, v): raise OSError("disco lleno")
        ACT._puntero = falla
        try:
            with self.assertRaisesRegex(ActualizacionError, "todo quedó como estaba"):
                ACT.aplicar(self.paquete("1.1.0", self.DEMO), self.inst)
        finally:
            ACT._puntero = original
        self.assertEqual(ACT.version_actual(self.inst), "1.0.0")
        self.assertNotIn("nota_demo", tablas(self.db))                                               # la migración real se deshizo
        self.assertFalse((self.inst / "versiones" / "1.1.0").exists())
        self.assertEqual(ACT.historial(self.inst), [])
        c = sqlite3.connect(self.db); self.assertEqual(c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 2); c.close()

    # ---------- revertir ----------
    def test_revertir_vuelve_a_la_version_y_base_anteriores_y_guarda_lo_posterior(self):
        ACT.aplicar(self.paquete("1.1.0", self.DEMO), self.inst)
        c = sqlite3.connect(self.db); c.execute("INSERT INTO config(clave,valor) VALUES('venta_posterior','1')"); c.commit(); c.close()
        with self.assertRaisesRegex(ActualizacionError, "perder_datos_posteriores"):
            ACT.revertir(self.inst)
        r = ACT.revertir(self.inst, perder_datos_posteriores=True)
        self.assertEqual((ACT.version_actual(self.inst), r["version"]), ("1.0.0", "1.0.0"))
        self.assertNotIn("nota_demo", tablas(self.db))
        c = sqlite3.connect(self.db); self.assertEqual(c.execute("SELECT COUNT(*) FROM config WHERE clave='venta_posterior'").fetchone()[0], 0); c.close()
        c = sqlite3.connect(r["copia_datos_posteriores"]); self.assertEqual(c.execute("SELECT COUNT(*) FROM config WHERE clave='venta_posterior'").fetchone()[0], 1); c.close()
        self.assertEqual([h["tipo"] for h in ACT.historial(self.inst)], ["actualizacion", "reversion"])
        with self.assertRaisesRegex(ActualizacionError, "No hay ninguna"):
            ACT.revertir(self.inst, True)
        ACT.aplicar(self.paquete("1.1.0", self.DEMO), self.inst)                                      # se puede volver a actualizar

    def test_se_conservan_solo_las_versiones_recientes(self):
        m8, m9 = self.DEMO, {**self.DEMO, "009_demo.sql": "CREATE TABLE otra_demo(id INTEGER);"}
        ACT.aplicar(self.paquete("1.1.0", m8), self.inst, conservar=2)
        ACT.aplicar(self.paquete("1.2.0", m9), self.inst, conservar=2)
        ACT.aplicar(self.paquete("1.3.0", m9), self.inst, conservar=2)
        self.assertEqual(sorted(p.name for p in (self.inst / "versiones").iterdir()), ["1.2.0", "1.3.0"])
        self.assertEqual(ACT.version_actual(self.inst), "1.3.0")

    # ---------- línea de comandos e instalador ----------
    def test_linea_de_comandos(self):
        py = lambda *a: subprocess.run([sys.executable, "-m", "tools.actualizar", *a], cwd=ROOT, capture_output=True, text=True)
        r = py("generar-claves", str(self.d / "cli")); self.assertEqual(r.returncode, 0, r.stderr)
        origen = self.d / "origen_cli"; copiar_codigo(origen); (origen / "app" / "version.py").write_text('VERSION = "1.1.0"\n')
        z = str(self.d / "cli.zip")
        self.assertEqual(py("empaquetar", str(origen), z, "1.1.0", str(self.d / "cli" / "clave_privada.pem")).returncode, 0)
        self.assertEqual(py("verificar", z, str(self.d / "cli" / "clave_publica.pem")).returncode, 0)
        malo = py("verificar", z, self.pub); self.assertEqual(malo.returncode, 1); self.assertIn("Firma inválida", malo.stdout)
        self.assertEqual(py("version", str(self.inst)).stdout.strip(), "1.0.0")
        self.assertEqual(py().returncode, 2)

    def test_scripts_del_instalador_no_tienen_la_trampa_del_digito_antes_del_redireccionamiento(self):
        bat = (ROOT / "instalador" / "instalar.bat").read_text()
        self.assertIn('>"%DEST%\\version_actual.txt" echo %VERSION%', bat)
        self.assertNotIn("echo %VERSION%>", bat)
        for d in ("app", "core", "tools", "database", "fonts"):
            self.assertIn(d, bat)


if __name__ == "__main__":
    unittest.main()
