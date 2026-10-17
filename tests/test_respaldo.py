import http.client, json, os, shutil, sqlite3, subprocess, sys, tempfile, threading, time, unittest, zipfile
from datetime import datetime, timedelta, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tools.db as TDB
from tools import respaldo as RS
from tools.db import connect, migrate
from tools.respaldo import RespaldoError
from app.server import crear_servidor
from core import auth, inventario as I
from tests.test_reportes import Base, NEG

UTC = timezone.utc


def escribir(ruta, datos):
    with open(ruta, "wb" if isinstance(datos, bytes) else "w") as f:
        f.write(datos)


def leer(ruta):
    with open(ruta, "rb") as f:
        return f.read()


def contar(db, tabla="ventas"):
    c = sqlite3.connect(db)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
    finally:
        c.close()


class R(Base):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.d.name, "r.db"); self.carpeta = os.path.join(self.d.name, "resp")

    def vender(self, n=1):
        for _ in range(n):
            self.cobrar_usd_y_cerrar(self.carrito(self.luis, [(NEG, 1)]), self.luis)

    def test_respaldo_verificado_con_manifiesto(self):
        self.vender(2)
        r = RS.respaldar(self.db, self.carpeta)
        self.assertTrue(os.path.exists(r["archivo"]))
        self.assertEqual((r["filas"]["ventas"], r["filas"]["usuarios"]), (2, 2))
        self.assertIn("001_init.sql", r["esquema"])
        self.assertEqual(RS.verificar_respaldo(r["archivo"])["sha256"], r["sha256"])
        self.assertEqual([f for f in os.listdir(self.carpeta) if f.startswith(".tmp")], [])        # no deja basura

    def test_respaldo_con_la_caja_escribiendo(self):
        vid = self.con.execute("SELECT id FROM variantes LIMIT 1").fetchone()[0]
        antes = contar(self.db, "inventario_movimientos")
        def escribir():
            c2 = connect(self.db)                                    # la conexión se crea en el propio hilo
            for _ in range(300):
                I.registrar_movimiento(c2, 1, vid, "compra", 1, "carga continua")
            c2.close()
        hilo = threading.Thread(target=escribir); hilo.start()
        r = RS.respaldar(self.db, self.carpeta)
        hilo.join()
        man = RS.verificar_respaldo(r["archivo"])                                                   # íntegro y consistente
        despues = contar(self.db, "inventario_movimientos")
        self.assertEqual(despues, antes + 300)                                                      # el hilo sí escribió
        self.assertLessEqual(man["filas"]["inventario_movimientos"], despues)

    def test_verificar_detecta_danos_y_alteraciones(self):
        self.vender(); ok = RS.respaldar(self.db, self.carpeta)["archivo"]
        datos = leer(ok)
        trunc, basura = os.path.join(self.d.name, "t.zip"), os.path.join(self.d.name, "b.zip")
        escribir(trunc, datos[:len(datos) // 2]); escribir(basura, b"no soy un zip")
        sin_man, alterado = os.path.join(self.d.name, "s.zip"), os.path.join(self.d.name, "a.zip")
        with zipfile.ZipFile(ok) as z, zipfile.ZipFile(sin_man, "w") as w, zipfile.ZipFile(alterado, "w") as a:
            w.writestr("zapateria.db", z.read("zapateria.db"))
            a.writestr("manifest.json", z.read("manifest.json")); a.writestr("zapateria.db", z.read("zapateria.db")[:-10] + b"X" * 10)
        for malo in (trunc, basura, sin_man, alterado, os.path.join(self.d.name, "nada.zip")):
            with self.assertRaises(RespaldoError, msg=malo):
                RS.verificar_respaldo(malo)

    def test_restaurar_sobre_la_base_actual_con_copia_de_seguridad(self):
        self.vender(2); zipf = RS.respaldar(self.db, self.carpeta)["archivo"]
        self.vender(3)                                                                                # lo posterior al respaldo "se pierde"
        self.con.close()
        r = RS.restaurar(zipf, self.db)
        self.assertEqual(contar(self.db), 2)
        self.assertEqual(contar(r["seguridad"]), 5)                                                   # pero queda guardado aparte
        self.assertEqual([e for e in os.listdir(self.d.name) if e.endswith(("-wal", "-shm")) and e.startswith("r.db-")] , [])
        self.con = connect(self.db)
        self.assertEqual(auth.login(self.con, 1, "Luis", "7305")["rol"], "cajero")

    def test_restaurar_en_una_pc_nueva(self):
        self.vender(); zipf = RS.respaldar(self.db, self.carpeta)["archivo"]
        nuevo = os.path.join(self.d.name, "pc_nueva", "zapateria.db"); os.makedirs(os.path.dirname(nuevo))
        r = RS.restaurar(zipf, nuevo)
        self.assertEqual((r["seguridad"], contar(nuevo), r["migraciones"]), (None, 1, []))

    def test_restaurar_un_respaldo_de_version_anterior_lo_actualiza(self):
        real = TDB.MIGRATIONS; viejo = os.path.join(self.d.name, "mig"); os.makedirs(viejo)
        for f in sorted(real.glob("00[1-3]_*.sql")):
            shutil.copy(f, viejo)
        TDB.MIGRATIONS = type(real)(viejo)
        try:
            db_v = os.path.join(self.d.name, "viejo.db"); c = connect(db_v); migrate(c); c.close()
            zipf = RS.respaldar(db_v, self.carpeta)["archivo"]
        finally:
            TDB.MIGRATIONS = real
        destino = os.path.join(self.d.name, "nuevo.db")
        r = RS.restaurar(zipf, destino)
        self.assertEqual(r["migraciones"][:2], ["004_pos.sql", "005_pagos.sql"])
        self.assertEqual(contar(destino, "devoluciones"), 0)                                          # existe la tabla nueva

    def test_un_respaldo_invalido_no_toca_la_base_y_un_fallo_posterior_revierte(self):
        self.vender(2); self.con.close()
        malo = os.path.join(self.d.name, "malo.zip"); escribir(malo, b"x")
        with self.assertRaises(RespaldoError):
            RS.restaurar(malo, self.db)
        self.assertEqual((contar(self.db), [e for e in os.listdir(self.d.name) if "antes_de" in e]), (2, []))
        buena = RS.respaldar(self.db, self.carpeta)["archivo"]
        c = connect(self.db); c.execute("INSERT INTO config(clave,valor) VALUES('marca_posterior','1')"); c.close()
        original = RS.migrate; RS.migrate = lambda con: (_ for _ in ()).throw(RuntimeError("falla simulada"))
        try:
            with self.assertRaises(RuntimeError):
                RS.restaurar(buena, self.db)
        finally:
            RS.migrate = original
        c = sqlite3.connect(self.db)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM config WHERE clave='marca_posterior'").fetchone()[0], 1)   # volvió a como estaba
        c.close()

    def test_nombres_no_se_pisan(self):
        ahora = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
        a, b = RS.respaldar(self.db, self.carpeta, ahora)["archivo"], RS.respaldar(self.db, self.carpeta, ahora)["archivo"]
        self.assertNotEqual(a, b); RS.verificar_respaldo(a); RS.verificar_respaldo(b)

    def test_rotacion_conserva_dias_semanas_y_meses(self):
        os.makedirs(self.carpeta)
        nombres = ["20261010_080000", "20261010_200000", "20261009_100000", "20261008_100000", "20261007_100000",
                   "20261001_100000", "20260920_100000", "20260815_100000", "20260701_100000"]
        for n in nombres:
            escribir(os.path.join(self.carpeta, f"respaldo_{n}.zip"), "x")
        escribir(os.path.join(self.carpeta, "notas.txt"), "no tocar")
        borrados = RS.rotar(self.carpeta, diarios=3, semanales=2, mensuales=2)
        self.assertEqual(sorted(os.path.basename(b)[9:24] for b in borrados), ["20260701_100000", "20260815_100000", "20261007_100000", "20261010_080000"])
        self.assertEqual(len(RS.listar(self.carpeta)), 5); self.assertTrue(os.path.exists(os.path.join(self.carpeta, "notas.txt")))

    def test_respaldo_diario_uno_por_dia_local_y_segunda_copia(self):
        copia = os.path.join(self.d.name, "usb")
        f = lambda *a: datetime(*a, tzinfo=UTC)
        r = RS.respaldo_diario(self.db, self.carpeta, copia, f(2026, 10, 10, 12))
        self.assertTrue(os.path.exists(r["archivo"]) and os.path.exists(r["copia"]) and r["copia_error"] is None)
        for tarde in (f(2026, 10, 10, 20), f(2026, 10, 11, 3)):                                      # sigue siendo el 10 en Venezuela
            self.assertIsNone(RS.respaldo_diario(self.db, self.carpeta, copia, tarde))
        self.assertIsNotNone(RS.respaldo_diario(self.db, self.carpeta, copia, f(2026, 10, 11, 4, 30)))   # ya es el 11 local
        self.assertEqual((len(RS.listar(self.carpeta)), len(RS.listar(copia))), (2, 2))
        archivo = os.path.join(self.d.name, "es_un_archivo"); escribir(archivo, "x")
        r = RS.respaldo_diario(self.db, os.path.join(self.d.name, "otra"), os.path.join(archivo, "x"), f(2026, 10, 12, 12))
        self.assertTrue(os.path.exists(r["archivo"]) and r["copia_error"])                            # el USB falló: el principal quedó

    def test_estado_y_alerta(self):
        ahora = datetime(2026, 10, 10, 12, tzinfo=UTC)
        self.assertTrue(RS.estado(self.carpeta, ahora)["alerta"])
        RS.respaldar(self.db, self.carpeta, ahora)
        self.assertFalse(RS.estado(self.carpeta, ahora + timedelta(hours=5))["alerta"])
        e = RS.estado(self.carpeta, ahora + timedelta(hours=60))
        self.assertTrue(e["alerta"]); self.assertIn("2 día(s)", e["mensaje"])

    def test_base_grande_respalda_y_restaura_muy_por_debajo_de_10_minutos(self):
        grande = os.path.join(self.d.name, "grande.db"); c = connect(grande); migrate(c)
        c.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')"); c.execute("INSERT INTO tasas_cambio(tipo,tasa_micro) VALUES('bcv',40500000)")
        c.execute("INSERT INTO categorias(nombre) VALUES('C')"); c.execute("INSERT INTO marcas(nombre) VALUES('M')")
        c.execute("INSERT INTO productos(categoria_id,marca_id,modelo,genero) VALUES(1,1,'X','dama')")
        c.execute("INSERT INTO variantes(producto_id,sku,talla,color) VALUES(1,'S','38','n')")
        N = 20000
        c.execute("BEGIN")
        c.executemany("INSERT INTO ventas(uuid,sucursal_id,tasa_id,tasa_micro,total_usd_cents) VALUES(?,1,1,40500000,1739)", [(f"v{i}",) for i in range(N)])
        c.executemany("INSERT INTO venta_items(venta_id,variante_id,cantidad,precio_unit_usd_cents) VALUES(?,1,2,1499)", [(i + 1,) for i in range(N)])
        c.executemany("INSERT INTO pagos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents) VALUES(?,'efectivo_usd','USD',1739,1739)", [(i + 1,) for i in range(N)])
        c.executemany("INSERT INTO inventario_movimientos(uuid,sucursal_id,variante_id,tipo,cantidad,motivo) VALUES(?,1,1,'venta',-2,'v')", [(f"m{i}",) for i in range(N)])
        c.execute("COMMIT"); c.close()
        t0 = time.monotonic()
        zipf = RS.respaldar(grande, self.carpeta)["archivo"]; RS.verificar_respaldo(zipf)
        t1 = time.monotonic(); r = RS.restaurar(zipf, os.path.join(self.d.name, "restaurada.db")); t2 = time.monotonic()
        self.assertEqual(contar(os.path.join(self.d.name, "restaurada.db")), N)
        self.assertLess(t2 - t0, 600)                                                                  # criterio: < 10 minutos
        print(f"\n[rendimiento] {N} ventas: respaldo+verificación {t1 - t0:.1f}s · restauración {t2 - t1:.1f}s · zip {os.path.getsize(zipf) / 1e6:.1f} MB")

    def test_linea_de_comandos(self):
        self.vender()
        py = lambda *a: subprocess.run([sys.executable, "-m", "tools.respaldo", *a], cwd=ROOT, capture_output=True, text=True)
        r = py("respaldar", self.db, self.carpeta); self.assertEqual(r.returncode, 0, r.stderr)
        z = RS.listar(self.carpeta)[0]["archivo"]
        self.assertEqual(py("verificar", z).returncode, 0)
        nuevo = os.path.join(self.d.name, "cli.db"); self.assertEqual(py("restaurar", z, nuevo).returncode, 0); self.assertEqual(contar(nuevo), 1)
        malo = os.path.join(self.d.name, "m.zip"); escribir(malo, b"x")
        r = py("verificar", malo); self.assertEqual(r.returncode, 1); self.assertIn("ERROR", r.stdout)
        self.assertEqual(py().returncode, 2)


class Servidor(Base):
    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.d.name, "r.db")
        self.srv = crear_servidor(self.db, 0); threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); super().tearDown()

    def req(self, m, ruta, cuerpo=None, tok=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=20)
        c.request(m, ruta, json.dumps(cuerpo) if cuerpo is not None else None, {"X-Token": tok or "", "Content-Type": "application/json"})
        r = c.getresponse(); d = r.read(); c.close(); return r.status, json.loads(d)

    def test_respaldos_por_http_solo_dueno_con_alerta_y_copia(self):
        dueno = self.req("POST", "/api/login", {"nombre": "Marta", "pin": "4821"})[1]["token"]
        luis = self.req("POST", "/api/login", {"nombre": "Luis", "pin": "7305"})[1]["token"]
        self.assertEqual((self.req("GET", "/api/respaldos", tok=luis)[0], self.req("POST", "/api/respaldos", tok=luis)[0]), (403, 403))
        self.assertIsNone(self.req("GET", "/api/estado", tok=luis)[1]["respaldo"])                    # el cajero no ve esto
        self.assertTrue(self.req("GET", "/api/estado", tok=dueno)[1]["respaldo"]["alerta"])           # aún no hay respaldo
        usb = os.path.join(self.d.name, "usb"); self.con.execute("INSERT INTO config(clave,valor) VALUES('respaldo_copia',?)", (usb,))
        st, r = self.req("POST", "/api/respaldos", tok=dueno)
        self.assertEqual((st, r["copia_error"]), (200, None))
        st, e = self.req("GET", "/api/respaldos", tok=dueno)
        self.assertEqual((e["alerta"], len(e["lista"]), len(os.listdir(usb))), (False, 1, 1))
        self.assertFalse(self.req("GET", "/api/estado", tok=dueno)[1]["respaldo"]["alerta"])


if __name__ == "__main__":
    unittest.main()
