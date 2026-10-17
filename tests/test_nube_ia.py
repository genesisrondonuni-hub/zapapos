import os, shutil, subprocess, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "node no instalado")
class FuncionEdgeAsistente(unittest.TestCase):
    def test_pruebas_de_la_funcion_del_asistente(self):
        r = subprocess.run(["node", "--test", str(ROOT / "nube" / "funciones" / "asistente" / "test.mjs")], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout[-2500:] + r.stderr[-800:])
        self.assertIn("# fail 0", r.stdout)
        self.assertRegex(r.stdout, r"# pass (1[4-9]|[2-9]\d)")

    def test_sql_de_la_nube_solo_para_service_role(self):
        sql = (ROOT / "nube" / "003_asistente_ia.sql").read_text()
        self.assertIn("to service_role", sql)
        self.assertNotIn("to anon", sql)
        self.assertIn("from public, anon, authenticated", sql)

    def test_la_clave_de_ia_nunca_esta_en_el_codigo(self):
        for archivo in [*ROOT.joinpath("nube").rglob("*"), ROOT / "app" / "dueno.html"]:
            if archivo.is_file() and archivo.suffix in (".ts", ".mjs", ".sql", ".html", ".md"):
                t = archivo.read_text(errors="ignore")
                for patron in ("sk-", "AIza", "Bearer sk"):
                    self.assertNotIn(patron, t, f"{archivo.name} parece contener una clave ({patron})")
