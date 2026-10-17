import os, sys, tempfile, unittest, sqlite3
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.db import connect, migrate
from core import auth
from core.auth import AuthError


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.con = connect(os.path.join(self.d.name, "a.db"))
        migrate(self.con)
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')")
        self.dueno_id = auth.crear_usuario(self.con, 1, "Marta", "dueno", "4821")
        self.dueno = {"id": self.dueno_id, "rol": "dueno"}
        self.caj_id = auth.crear_usuario(self.con, 1, "Luis", "cajero", "7305", actor=self.dueno)

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def test_decisiones_dueno_en_config(self):
        g = lambda k: self.con.execute("SELECT valor FROM config WHERE clave=?", (k,)).fetchone()[0]
        self.assertEqual((g("tasa_default"), g("iva_modo_default"), g("fiscal_via")), ("bcv", "sumado", "imprenta_digital"))

    def test_login_ok_y_pin_no_guardado_en_claro(self):
        u = auth.login(self.con, 1, "luis", "7305")  # nombre sin distinguir mayúsculas
        self.assertEqual((u["rol"], u["id"]), ("cajero", self.caj_id))
        h = self.con.execute("SELECT pin_hash FROM usuarios WHERE id=?", (self.caj_id,)).fetchone()[0]
        self.assertNotIn("7305", h)

    def test_login_fallido_mensaje_generico(self):
        for nom, pin in (("Luis", "0001"), ("Nadie", "7305")):
            with self.assertRaisesRegex(AuthError, "Usuario o PIN incorrecto"):
                auth.login(self.con, 1, nom, pin)

    def test_bloqueo_tras_5_intentos_y_expira(self):
        t0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        for _ in range(5):
            with self.assertRaises(AuthError):
                auth.login(self.con, 1, "Luis", "9999", ahora=t0)
        with self.assertRaisesRegex(AuthError, "bloqueado"):   # ni con el PIN correcto
            auth.login(self.con, 1, "Luis", "7305", ahora=t0 + timedelta(minutes=1))
        u = auth.login(self.con, 1, "Luis", "7305", ahora=t0 + timedelta(minutes=6))
        self.assertEqual(u["nombre"], "Luis")

    def test_intentos_fallidos_se_persisten(self):
        with self.assertRaises(AuthError):
            auth.login(self.con, 1, "Luis", "9999")
        self.assertEqual(self.con.execute("SELECT intentos_fallidos FROM usuarios WHERE id=?", (self.caj_id,)).fetchone()[0], 1)

    def test_pin_invalido_o_debil(self):
        for p in ("12", "abcd", "1111", "1234", "1234567"):
            with self.assertRaises(AuthError):
                auth.hash_pin(p)

    def test_permisos_creacion_usuarios(self):
        with self.assertRaises(AuthError):
            auth.crear_usuario(self.con, 1, "Ana", "cajero", "5192", actor={"id": self.caj_id, "rol": "cajero"})
        with self.assertRaises(AuthError):
            auth.crear_usuario(self.con, 1, "luis", "cajero", "5192", actor=self.dueno)  # nombre repetido

    def test_primer_usuario_debe_ser_dueno(self):
        c = connect(os.path.join(self.d.name, "b.db")); migrate(c)
        c.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')")
        with self.assertRaises(AuthError):
            auth.crear_usuario(c, 1, "Pepe", "cajero", "4821")
        c.close()

    def test_cambiar_pin(self):
        caj = {"id": self.caj_id, "rol": "cajero"}
        with self.assertRaises(AuthError):
            auth.cambiar_pin(self.con, caj, self.caj_id, "8264", pin_actual="0000")
        auth.cambiar_pin(self.con, caj, self.caj_id, "8264", pin_actual="7305")
        auth.login(self.con, 1, "Luis", "8264")
        with self.assertRaises(AuthError):   # cajero no toca a otro
            auth.cambiar_pin(self.con, caj, self.dueno_id, "8264", pin_actual="8264")
        auth.cambiar_pin(self.con, self.dueno, self.caj_id, "6038")  # dueño restablece sin PIN actual
        auth.login(self.con, 1, "Luis", "6038")

    def test_auditoria_inmutable_y_registra(self):
        auth.login(self.con, 1, "Luis", "7305")
        acc = [r[0] for r in self.con.execute("SELECT accion FROM auditoria")]
        self.assertIn("login_ok", acc); self.assertIn("usuario_creado", acc)
        with self.assertRaises(sqlite3.DatabaseError):
            self.con.execute("DELETE FROM auditoria")

    def test_require_rol(self):
        auth.require_rol({"rol": "dueno"}, "dueno")
        with self.assertRaises(AuthError):
            auth.require_rol({"rol": "cajero"}, "dueno")


if __name__ == "__main__":
    unittest.main()
