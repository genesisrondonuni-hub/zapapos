import http.server, json, os, sys, threading, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.server import crear_servidor
from core import inventario as I, negocio as N, pos, pagos as PG, licencia as LIC
from tools import sync as SY
from tools.sync import SyncError, TransporteHTTP
from tests.test_reportes import Base, NEG

T0 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


class Nube:
    """Nube de mentira: idempotente por uuid, se puede 'desconectar' o hacer que rechace eventos."""
    def __init__(self):
        self.recibidos, self.lotes, self.offline, self.rechazar = {}, [], False, set()

    def push(self, eventos):
        self.lotes.append(len(eventos))
        if self.offline:
            raise ConnectionError("sin internet")
        ack = []
        for e in eventos:
            if e["uuid"] in self.rechazar:
                continue
            self.recibidos.setdefault(e["uuid"], e); ack.append(e["uuid"])
        return ack


class S(Base):
    def eventos(self, entidad=None, pendientes=False):
        q = "SELECT uuid,entidad,payload,intentos,proximo_intento,enviado_en FROM outbox WHERE 1=1"
        q += f" AND entidad='{entidad}'" if entidad else ""
        q += " AND enviado_en IS NULL" if pendientes else ""
        return self.con.execute(q + " ORDER BY id").fetchall()

    # ---------- eventos que genera la caja ----------
    def test_cada_movimiento_de_inventario_genera_un_evento(self):
        ev = self.eventos("movimiento")
        self.assertEqual(len(ev), 3)                                                    # las 3 compras iniciales de Base
        p = json.loads(ev[0][2]); self.assertEqual((p["sku"], p["tipo"], p["cantidad"], p["sucursal_id"]), (NEG, "compra", 10, 1))
        vid = self.carrito(self.luis, [(NEG, 1)]); self.cobrar_usd_y_cerrar(vid, self.luis)
        self.assertEqual(len(self.eventos("movimiento")), 4); self.assertEqual(len(self.eventos("venta")), 1)
        n = self.con.execute("SELECT id FROM inventario_movimientos WHERE tipo='venta'").fetchone()[0]
        I.reversar_movimiento(self.con, self.dueno, n, "venta anulada")
        self.assertEqual([json.loads(e[2])["tipo"] for e in self.eventos("movimiento")][-1], "reversa")

    def test_un_movimiento_rechazado_no_deja_evento(self):
        antes = len(self.eventos())
        with self.assertRaises(I.InventarioError):
            I.registrar_movimiento(self.con, 1, 1, "ajuste_neg", 999, "no hay tanto")
        self.assertEqual(len(self.eventos()), antes)

    # ---------- envío ----------
    def test_sincronizar_envia_todo_una_sola_vez(self):
        vid = self.carrito(self.luis, [(NEG, 1)]); self.cobrar_usd_y_cerrar(vid, self.luis)
        nube = Nube()
        r = SY.sincronizar(self.con, nube, T0)
        total = len(nube.recibidos)
        self.assertEqual((r["enviados"], r["fallidos"], r["error"]), (total, 0, None)); self.assertGreaterEqual(total, 5)
        self.assertEqual({e["entidad"] for e in nube.recibidos.values()}, {"movimiento", "venta", "catalogo"})
        self.assertEqual(SY.estado(self.con)["pendientes"], 0)
        self.assertEqual(SY.sincronizar(self.con, nube, T0)["enviados"], 0)             # nada nuevo, nada que enviar
        self.assertEqual(len(nube.recibidos), total)

    def test_sin_internet_no_se_pierde_nada_y_reintenta_con_espera_creciente(self):
        """Sin internet no se pierde nada y el backoff crece entre intentos.

        `_espera` se mockea para eliminar el jitter (verificado por separado en
        test_jitter_en_backoff_dentro_de_rango_y_varia). Este test verifica la
        lógica de reintento: no pérdida, backoff creciente, recuperación.
        """
        from unittest.mock import patch
        nube = Nube(); nube.offline = True
        with patch("tools.sync._espera", side_effect=lambda n: 60 * 2 ** min(n, 20)):
            r = SY.sincronizar(self.con, nube, T0)
            self.assertEqual((r["enviados"], "sin internet" in r["error"]), (0, True))
            pend = self.eventos(pendientes=True); self.assertTrue(pend)
            self.assertTrue(all(e[3] == 1 for e in pend))                                   # un intento anotado
            llamadas = len(nube.lotes)
            SY.sincronizar(self.con, nube, T0 + timedelta(seconds=30))                      # todavía dentro de la espera (60 s)
            self.assertEqual(len(nube.lotes), llamadas)
            SY.sincronizar(self.con, nube, T0 + timedelta(seconds=61))                      # ya toca: falla otra vez, espera 120 s
            self.assertEqual(self.eventos(pendientes=True)[0][3], 2)
        nube.offline = False                                                            # vuelve el internet
        r = SY.sincronizar(self.con, nube, T0 + timedelta(minutes=10))
        self.assertEqual((r["fallidos"], r["error"], SY.estado(self.con)["pendientes"]), (0, None, 0))
        self.assertEqual(len(nube.recibidos), len(self.eventos()))                      # llegó todo, sin duplicados

    def test_confirmacion_parcial_reintenta_solo_lo_pendiente(self):
        SY.encolar_catalogo(self.con)
        rechazado = self.eventos("movimiento")[1][0]
        nube = Nube(); nube.rechazar = {rechazado}
        r = SY.sincronizar(self.con, nube, T0)
        self.assertEqual(r["fallidos"], 1); self.assertEqual([e[0] for e in self.eventos(pendientes=True)], [rechazado])
        nube.rechazar = set()
        self.assertEqual(SY.sincronizar(self.con, nube, T0 + timedelta(minutes=5))["enviados"], 1)
        self.assertEqual(len(nube.recibidos), len(self.eventos()))

    def test_enviar_lote_reporta_error_cuando_push_devuelve_menos_uuids(self):
        """Si push() no lanza excepción pero devuelve menos uuids, error != None."""
        SY.encolar_catalogo(self.con)
        rechazado = self.eventos("movimiento")[0][0]
        nube = Nube(); nube.rechazar = {rechazado}
        r = SY.enviar_lote(self.con, nube, T0)
        self.assertEqual(r["fallidos"], 1)
        self.assertIsNotNone(r["error"])
        self.assertIn("sin confirmación", r["error"])

    def test_sincronizar_propaga_error_de_fallidos_parciales(self):
        """sincronizar no debe devolver error=None cuando hay fallidos."""
        SY.encolar_catalogo(self.con)
        rechazado = self.eventos("movimiento")[0][0]
        nube = Nube(); nube.rechazar = {rechazado}
        r = SY.sincronizar(self.con, nube, T0)
        self.assertEqual(r["fallidos"], 1)
        self.assertIsNotNone(r["error"])
        self.assertIn("sin confirmación", r["error"])

    def test_jitter_en_backoff_dentro_de_rango_y_varia(self):
        """_espera aplica jitter 50-100% y varía entre llamadas."""
        import random
        # Rangos: base * [0.5, 1.0]
        for intentos, (lo, hi) in [(0, (30, 60)), (1, (60, 120)), (5, (960, 1920)), (9, (10800, 21600))]:
            for _ in range(10):
                v = SY._espera(intentos)
                self.assertGreaterEqual(v, lo, f"intentos={intentos} v={v} < {lo}")
                self.assertLessEqual(v, hi, f"intentos={intentos} v={v} > {hi}")
        # Varia entre llamadas (con seed fijo, 20 llamadas no deben dar lo mismo)
        random.seed(42)
        vals = [SY._espera(5) for _ in range(20)]
        self.assertGreater(len(set(vals)), 1, "jitter debe variar entre llamadas")

    def test_lotes(self):
        for _ in range(250):
            I.registrar_movimiento(self.con, 1, 1, "compra", 1, "carga")
        nube = Nube(); SY.sincronizar(self.con, nube, T0)
        self.assertEqual(nube.lotes, [100, 100, 54]); self.assertEqual(sum(nube.lotes), len(self.eventos()))      # 250 + 3 compras + 1 catálogo
        self.assertEqual(SY.estado(self.con)["pendientes"], 0)

    def test_catalogo_solo_se_reenvia_si_cambia_y_el_ultimo_manda(self):
        self.assertTrue(SY.encolar_catalogo(self.con)); self.assertFalse(SY.encolar_catalogo(self.con))
        self.con.execute("UPDATE variantes SET precio_usd_cents=precio_usd_cents+100 WHERE sku=?", (NEG,))
        self.assertTrue(SY.encolar_catalogo(self.con))
        self.con.execute("UPDATE variantes SET precio_usd_cents=precio_usd_cents-100 WHERE sku=?", (NEG,))
        self.assertTrue(SY.encolar_catalogo(self.con))                                  # volver al precio anterior también es un cambio
        cat = self.eventos("catalogo"); self.assertEqual(len(cat), 3)
        v = {x["sku"]: x for x in json.loads(cat[0][2])["variantes"]}[NEG]
        self.assertEqual((v["marca"], v["modelo"], v["talla"], v["color"]), ("Gamo", "Flor", "38", "negro"))

    def test_catalogo_reenviar_a_b_a_genera_uuids_distintos(self):
        """A→B→A genera tres eventos con UUIDs distintos.

        La idempotencia real vive en la huella, que evita reencolar una foto
        cuando el catálogo no cambió. El UUID es único por envío y no deriva de
        la huella: si A1 y A3 compartieran UUID, la nube podría colapsarlos con
        ON CONFLICT DO NOTHING y dejar B como catálogo final.
        """
        # Estado A
        self.assertTrue(SY.encolar_catalogo(self.con))

        # Cambio a B
        self.con.execute(
            "UPDATE variantes SET precio_usd_cents=precio_usd_cents+100 WHERE sku=?",
            (NEG,),
        )
        self.assertTrue(SY.encolar_catalogo(self.con))

        # Vuelta a A
        self.con.execute(
            "UPDATE variantes SET precio_usd_cents=precio_usd_cents-100 WHERE sku=?",
            (NEG,),
        )
        self.assertTrue(SY.encolar_catalogo(self.con))

        cat = self.eventos("catalogo")
        self.assertEqual(len(cat), 3, "A→B→A debe generar 3 eventos, no colapsarlos")

        uuids = [e[0] for e in cat]
        self.assertEqual(len(set(uuids)), 3, "los 3 eventos deben tener UUIDs distintos")

        payload_a1 = json.loads(cat[0][2])
        payload_a3 = json.loads(cat[2][2])
        self.assertEqual(payload_a1["variantes"], payload_a3["variantes"])

        payload_b = json.loads(cat[1][2])
        self.assertNotEqual(
            payload_a1["variantes"],
            payload_b["variantes"],
            "el evento del medio debe ser B (catálogo cambiado)",
        )

    def test_estado(self):
        e = SY.estado(self.con); self.assertEqual((e["pendientes"] > 0, e["enviados"], e["ultimo_envio"]), (True, 0, None))
        SY.sincronizar(self.con, Nube(), T0)
        e = SY.estado(self.con); self.assertEqual((e["pendientes"], e["enviados"] > 0, e["ultimo_envio"][:10]), (0, True, "2026-10-10"))

    # ---------- configuración y licencia ----------
    def test_configuracion_de_la_nube_valida_y_enmascara_secretos(self):
        self.assertIsNone(SY.transporte_desde_config(self.con))
        self.con.execute("INSERT INTO config(clave,valor) VALUES('instalacion_id','11111111-1111-1111-1111-111111111111')")
        for mal in ({"nube_url": "http://inseguro.com"}, {"nube_url": "ftp://x"}, {"nube_token": "x" * 601}):
            with self.assertRaises(N.NegocioError):
                N.actualizar_datos(self.con, self.dueno, mal)
        N.actualizar_datos(self.con, self.dueno, {"nube_url": "https://abc.supabase.co", "nube_apikey": "clave-publica", "nube_token": "secreto"})
        d = N.leer_datos(self.con); self.assertEqual((d["nube_url"], d["nube_token"], d["nube_apikey"]), ("https://abc.supabase.co", "********", "********"))
        N.actualizar_datos(self.con, self.dueno, {"nube_token": "********", "negocio_telefono": "0414-9"})              # el formulario no pisa el secreto
        t = SY.transporte_desde_config(self.con)
        self.assertEqual((t.token, t.url, t.iid[:2]), ("secreto", "https://abc.supabase.co", "11"))

    def test_el_ciclo_respeta_la_licencia_pero_nunca_afecta_lo_esencial(self):
        db = os.path.join(self.d.name, "r.db"); nube = Nube()
        self.assertEqual(SY.ciclo(db, nube)["fallidos"], 0)                             # en prueba: las funciones Pro están activas
        self.assertGreater(len(nube.recibidos), 0)
        self.con.execute("UPDATE config SET valor='2020-01-01' WHERE clave='licencia_primer_uso'")      # prueba terminada hace años
        I.registrar_movimiento(self.con, 1, 1, "compra", 1, "nuevo")
        nube2 = Nube(); self.assertIsNone(SY.ciclo(db, nube2)); self.assertEqual(nube2.lotes, [])                  # la nube se detiene...
        vid = self.carrito(self.luis, [(NEG, 1)]); self.cobrar_usd_y_cerrar(vid, self.luis)                       # ...pero se sigue vendiendo
        self.assertEqual(pos.ver_venta(self.con, vid)["venta"]["estado"], "cerrada")
        self.assertGreater(SY.estado(self.con)["pendientes"], 0)                                                  # y queda en cola para cuando se renueve

    # ---------- transporte HTTP contra un servidor de prueba ----------
    def servidor(self, estado=200, respuesta=None, crudo=None):
        capt = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(h):
                capt.update(ruta=h.path, apikey=h.headers.get("apikey"), auth=h.headers.get("Authorization"),
                            cuerpo=json.loads(h.rfile.read(int(h.headers["Content-Length"]))))
                datos = crudo if crudo is not None else json.dumps(respuesta if respuesta is not None else {"aceptados": [e["uuid"] for e in capt["cuerpo"]["p_eventos"]]}).encode()
                h.send_response(estado); h.send_header("Content-Length", str(len(datos))); h.end_headers(); h.wfile.write(datos)
            def log_message(h, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return f"http://127.0.0.1:{srv.server_port}", capt

    def test_transporte_http_formato_y_confirmacion(self):
        url, capt = self.servidor()
        t = TransporteHTTP(url, "APIKEY", "iid-1", "tok-1")
        r = SY.sincronizar(self.con, t, T0)
        self.assertEqual((r["fallidos"], r["error"]), (0, None)); self.assertEqual(SY.estado(self.con)["pendientes"], 0)
        self.assertEqual((capt["ruta"], capt["apikey"], capt["auth"]), ("/rest/v1/rpc/sync_push", "APIKEY", "Bearer APIKEY"))
        self.assertEqual((capt["cuerpo"]["p_instalacion"], capt["cuerpo"]["p_token"]), ("iid-1", "tok-1"))
        self.assertEqual(set(capt["cuerpo"]["p_eventos"][0]), {"uuid", "entidad", "operacion", "payload", "creado_en"})

    def test_transporte_http_errores_se_convierten_en_reintentos(self):
        casos = [dict(estado=401, crudo=b'{"message":"token invalido"}'), dict(estado=500, crudo=b"boom"), dict(crudo=b"no es json"),
                 dict(respuesta={"otra": 1}), dict(respuesta=[1, 2])]
        for c in casos:
            url, _ = self.servidor(**c)
            with self.assertRaises(SyncError, msg=c):
                TransporteHTTP(url, "k", "i", "t").push([{"uuid": "x", "entidad": "e", "operacion": "insert", "payload": {}, "creado_en": "z"}])
        with self.assertRaisesRegex(SyncError, "Sin conexión"):
            TransporteHTTP("http://127.0.0.1:1", "k", "i", "t", timeout=2).push([])
        url, _ = self.servidor(estado=401, crudo=b"{}")
        r = SY.sincronizar(self.con, TransporteHTTP(url, "k", "i", "t"), T0)
        self.assertIn("401", r["error"]); self.assertGreater(SY.estado(self.con)["con_reintentos"], 0)

    def test_endpoint_nube_solo_dueno(self):
        import http.client
        self.srv = crear_servidor(os.path.join(self.d.name, "r.db"), 0); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        def req(m, ruta, cuerpo=None, tok=""):
            c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=10)
            c.request(m, ruta, json.dumps(cuerpo) if cuerpo is not None else None, {"X-Token": tok, "Content-Type": "application/json"})
            r = c.getresponse(); d = r.read(); c.close(); return r.status, json.loads(d)
        luis = req("POST", "/api/login", {"nombre": "Luis", "pin": "7305"})[1]["token"]
        dueno = req("POST", "/api/login", {"nombre": "Marta", "pin": "4821"})[1]["token"]
        self.assertEqual(req("GET", "/api/nube", tok=luis)[0], 403)
        st, e = req("GET", "/api/nube", tok=dueno)
        self.assertEqual((st, e["configurada"], e["permitida"], e["pendientes"] > 0), (200, False, True, True))


if __name__ == "__main__":
    unittest.main()
