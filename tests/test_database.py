import os, subprocess, sys, tempfile, textwrap, unittest, sqlite3
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tools.db import connect, migrate


def base(con):
    con.execute("INSERT INTO sucursales(codigo,nombre) VALUES('S1','Principal')")
    con.execute("INSERT INTO categorias(nombre,margen_bp) VALUES('Dama',4000)")
    con.execute("INSERT INTO marcas(nombre) VALUES('Gamo')")
    con.execute("INSERT INTO productos(categoria_id,marca_id,modelo,genero) VALUES(1,1,'Sandalia X','dama')")
    con.execute("INSERT INTO variantes(producto_id,sku,talla,color) VALUES(1,'GAMO-SANX-38-NEG','38','negro')")
    con.execute("INSERT INTO variantes(producto_id,sku,talla,color) VALUES(1,'GAMO-SANX-38-ROJ','38','rojo')")
    con.execute("INSERT INTO tasas_cambio(tipo,tasa_micro) VALUES('bcv',40500000)")


class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.d.name, "t.db")
        self.con = connect(self.path)
        migrate(self.con)
        base(self.con)

    def tearDown(self):
        self.con.close(); self.d.cleanup()

    def test_migracion_idempotente(self):
        self.assertEqual(migrate(self.con), [])

    def test_pragmas_resiliencia(self):
        self.assertEqual(self.con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.con.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_sku_unico_y_variantes_sin_colision(self):
        n = self.con.execute("SELECT COUNT(*) FROM variantes WHERE talla='38'").fetchone()[0]
        self.assertEqual(n, 2)  # 38 negro y 38 rojo conviven
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO variantes(producto_id,sku,talla,color) VALUES(1,'GAMO-SANX-38-NEG','39','azul')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO variantes(producto_id,sku,talla,color) VALUES(1,'OTRO','38','negro')")

    def test_kardex_inmutable_y_reversa(self):
        c = self.con
        c.execute("INSERT INTO inventario_movimientos(uuid,sucursal_id,variante_id,tipo,cantidad,motivo) VALUES('a',1,1,'compra',10,'compra inicial')")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("UPDATE inventario_movimientos SET cantidad=99")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("DELETE FROM inventario_movimientos")
        c.execute("INSERT INTO inventario_movimientos(uuid,sucursal_id,variante_id,tipo,cantidad,motivo,reversa_de) VALUES('b',1,1,'reversa',-10,'error de carga',1)")
        self.assertEqual(c.execute("SELECT stock FROM stock_actual WHERE variante_id=1").fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):  # motivo obligatorio
            c.execute("INSERT INTO inventario_movimientos(uuid,sucursal_id,variante_id,tipo,cantidad,motivo) VALUES('c',1,1,'ajuste_pos',1,'  ')")

    def test_snapshot_tasa_y_pago_dividido(self):
        c = self.con
        c.execute("INSERT INTO ventas(uuid,sucursal_id,tasa_id,tasa_micro,subtotal_usd_cents,iva_usd_cents,total_usd_cents,total_bs_centimos) VALUES('v1',1,1,40500000,10000,1600,11600,469800)")
        c.execute("INSERT INTO tasas_cambio(tipo,tasa_micro) VALUES('bcv',45000000)")  # cambia la tasa luego
        self.assertEqual(c.execute("SELECT tasa_micro FROM ventas WHERE uuid='v1'").fetchone()[0], 40500000)
        c.execute("INSERT INTO pagos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents,referencia) VALUES(1,'pago_movil','BS',187920,4640,'REF1')")
        c.execute("INSERT INTO pagos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents) VALUES(1,'efectivo_usd','USD',6960,6960)")
        self.assertEqual(c.execute("SELECT SUM(equiv_usd_cents) FROM pagos").fetchone()[0], 11600)  # 40% + 60%
        with self.assertRaises(sqlite3.IntegrityError):  # referencia duplicada
            c.execute("INSERT INTO pagos(venta_id,metodo,moneda,monto_cents,equiv_usd_cents,referencia) VALUES(1,'pago_movil','BS',100,1,'REF1')")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("DELETE FROM pagos")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("DELETE FROM ventas")

    def test_tasa_invalida_y_fiscal_vacio(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("INSERT INTO tasas_cambio(tipo,tasa_micro) VALUES('bcv',0)")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM fiscal_correlativos").fetchone()[0], 0)

    # ---- Escenario VE: corte_luz_mid_venta ----
    def _crash(self, commit: bool):
        code = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {ROOT!r})
            from tools.db import connect
            c = connect({self.path!r})
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO ventas(uuid,sucursal_id,tasa_id,tasa_micro,total_usd_cents) VALUES('corte',1,1,40500000,5000)")
            c.execute("INSERT INTO venta_items(venta_id,variante_id,cantidad,precio_unit_usd_cents) VALUES(last_insert_rowid(),1,1,5000)")
            c.execute("INSERT INTO inventario_movimientos(uuid,sucursal_id,variante_id,tipo,cantidad,motivo) VALUES('m-corte',1,1,'venta',-1,'venta corte')")
            {"c.execute('COMMIT')" if commit else "pass"}
            os._exit(1)   # simula corte de luz: sin cierre limpio
        """)
        subprocess.run([sys.executable, "-c", code], check=False)

    def test_corte_luz_antes_de_commit_no_deja_datos_parciales(self):
        self.con.execute("INSERT INTO ventas(uuid,sucursal_id,tasa_id,tasa_micro,total_usd_cents) VALUES('previa',1,1,40500000,100)")
        self._crash(commit=False)
        c2 = connect(self.path)
        self.assertEqual(c2.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM ventas WHERE uuid='corte'").fetchone()[0], 0)
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM inventario_movimientos WHERE uuid='m-corte'").fetchone()[0], 0)
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM ventas WHERE uuid='previa'").fetchone()[0], 1)
        c2.close()

    def test_corte_luz_despues_de_commit_conserva_venta(self):
        self._crash(commit=True)
        c2 = connect(self.path)
        self.assertEqual(c2.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM ventas WHERE uuid='corte'").fetchone()[0], 1)
        self.assertEqual(c2.execute("SELECT COUNT(*) FROM venta_items").fetchone()[0], 1)
        self.assertEqual(c2.execute("SELECT stock FROM stock_actual WHERE variante_id=1").fetchone()[0], -1)
        c2.close()


class Separador(unittest.TestCase):
    def test_varias_sentencias_en_una_linea_y_triggers(self):
        from tools.db import _split
        sql = ("CREATE TABLE a(x); INSERT INTO a VALUES(1); -- nota; con punto y coma\n"
               "CREATE TRIGGER t BEFORE DELETE ON a BEGIN SELECT RAISE(ABORT,'no'); END;\nINSERT INTO a VALUES(2);\n-- fin\n")
        partes = [p.strip() for p in _split(sql)]
        self.assertEqual(len(partes), 4)
        self.assertTrue(partes[2].startswith("-- nota") or partes[2].startswith("CREATE TRIGGER"))
        self.assertTrue(any("RAISE(ABORT,'no'); END;" in p for p in partes))
        con = sqlite3.connect(":memory:"); [con.execute(p) for p in _split(sql)]
        self.assertEqual(con.execute("SELECT COUNT(*) FROM a").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            list(_split("CREATE TABLE b(x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
