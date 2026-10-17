"""Prueba de integración REAL contra la nube ZapaPOS en Supabase. Se salta automáticamente si la caja no tiene
la nube configurada (en desarrollo local no está). Solo se corre con la caja de demo para verificar el circuito completo."""
import json, os, sqlite3, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import inventario as I, negocio as N
from tools import sync as SY
from tools.db import connect, migrate

NUBE_URL = os.environ.get("ZAPAPOS_URL_NUBE", "")
NUBE_APIKEY = os.environ.get("ZAPAPOS_APIKEY", "")


@unittest.skipUnless(all(os.environ.get(k) for k in ("ZAPAPOS_URL_NUBE", "ZAPAPOS_APIKEY", "ZAPAPOS_NUBE_TOKEN")),
                     "Configuración de nube no está fijada: prueba omitida (solo para el proveedor)")
class NubeReal(unittest.TestCase):
    """Para correr esta prueba: ZAPAPOS_NUBE_TOKEN=<token> python -m unittest tests.test_sync_real -v"""
    def setUp(self):
        import tempfile; self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "z.db")
        self.con = connect(self.db); migrate(self.con)
        iid = os.environ.get("ZAPAPOS_NUBE_INSTALACION_ID", "11111111-1111-1111-1111-111111111111")
        self.con.execute("INSERT INTO config(clave,valor) VALUES('instalacion_id',?)", (iid,))
        self.con.execute("INSERT INTO config(clave,valor) VALUES('nube_url',?)", (NUBE_URL,))
        self.con.execute("INSERT INTO config(clave,valor) VALUES('nube_apikey',?)", (NUBE_APIKEY,))
        self.con.execute("INSERT INTO config(clave,valor) VALUES('nube_token',?)", (os.environ["ZAPAPOS_NUBE_TOKEN"],))

    def tearDown(self):
        self.con.close(); __import__("shutil").rmtree(self.d, ignore_errors=True)

    def test_enviar_y_verificar_con_estado(self):
        self.con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','P')")
        self.con.execute("INSERT INTO categorias(nombre) VALUES('C')")
        self.con.execute("INSERT INTO marcas(nombre) VALUES('M')")
        self.con.execute("INSERT INTO productos(categoria_id,marca_id,modelo,genero) VALUES(1,1,'X','dama')")
        self.con.execute("INSERT INTO variantes(producto_id,sku,talla,color) VALUES(1,'TEST-REAL-01','38','negro')")
        self.con.execute("INSERT INTO tasas_cambio(tipo,tasa_micro) VALUES('bcv',40500000)")
        I.registrar_movimiento(self.con, 1, 1, "compra", 5, "prueba de integracion real")
        t = SY.transporte_desde_config(self.con)
        self.assertIsNotNone(t, "La nube no está configurada en la base de prueba")
        r = SY.sincronizar(self.con, t)
        self.assertEqual((r["fallidos"], r["error"]), (0, None))
        self.assertGreater(r["enviados"], 0)
        self.assertEqual(SY.estado(self.con)["pendientes"], 0)
        print(f"\n[nube real] enviados={r['enviados']} · url={NUBE_URL}")


if __name__ == "__main__":
    unittest.main()
