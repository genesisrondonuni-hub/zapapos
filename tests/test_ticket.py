import os, socket, sys, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import pos, pagos as PG, ticket as T
from core.ticket import TicketError
from tests.test_reportes import Base

NEG = "GAMO-FLOR-38-NEG"


def leer(ruta):
    with open(ruta, "rb") as f:
        return f.read()


class TicketTests(Base):
    def venta_cerrada(self, efectivo=True):
        vid = self.carrito(self.luis, [(NEG, 1)])                                      # total $17,39 con IVA sumado
        if efectivo:
            z = PG.registrar_pago(self.con, self.luis, vid, "zelle", 500, "ZEL12345")["pago_id"]
            PG.confirmar_pago(self.con, self.luis, z)
            PG.registrar_pago(self.con, self.luis, vid, "efectivo_usd", 1239)
        else:
            z = PG.registrar_pago(self.con, self.luis, vid, "zelle", 1739, "ZEL99999")["pago_id"]
            PG.confirmar_pago(self.con, self.luis, z)
        pos.cerrar_venta(self.con, vid, self.luis["id"])
        return vid

    def test_contenido_y_ancho_de_58mm(self):
        vid = self.venta_cerrada()
        L = T.lineas_ticket(self.con, vid)
        self.assertTrue(all(len(x) <= T.COLS for x in L), [x for x in L if len(x) > T.COLS])
        txt = "\n".join(L)
        for esperado in ("TOTAL USD", "$17,39", "Tasa", "40,50", "Zelle ..2345", "Efectivo USD", "$12,39", "DOCUMENTO SIN VALOR FISCAL",
                         "Subtotal", "$14,99", "IVA 16%", "$2,40", f"V-{vid:06d}", "Cajero: Luis"):
            self.assertIn(esperado, txt)
        self.assertNotIn("RIF", txt)                                                  # no se inventa el RIF
        self.assertIn("Bs 704,30", txt)                                               # 17,39 x 40,50 = 704,295 -> 704,30

    def test_datos_del_negocio_solo_si_estan_configurados(self):
        vid = self.venta_cerrada()
        for k, v in (("negocio_nombre", "Zapatería Los Andes C.A. sucursal principal"), ("negocio_rif", "TEST-0000"), ("ticket_pie", "Cambios en 8 días con ticket")):
            self.con.execute("INSERT INTO config(clave,valor) VALUES(?,?)", (k, v))
        L = T.lineas_ticket(self.con, vid)
        self.assertTrue(all(len(x) <= T.COLS for x in L))
        self.assertIn("RIF TEST-0000", "\n".join(L))
        self.assertIn("Zapatería Los Andes C.A.", "\n".join(L).replace("\n", " ")[:80])
        self.assertIn("Cambios en 8 días", "\n".join(L))

    def test_hora_local_y_reimpresion(self):
        vid = self.venta_cerrada()
        self.retro(vid, "2026-10-09T15:00:00.000Z")
        L = T.lineas_ticket(self.con, vid, reimpresion=True)
        self.assertTrue(any("09/10/2026 11:00" in x for x in L))                        # UTC-4
        self.assertIn("*** REIMPRESION ***", L)

    def test_descripcion_larga_no_desborda_y_vuelto_en_ticket(self):
        self.con.execute("UPDATE productos SET modelo='Sandalia plataforma tejida edicion especial'")
        vid = self.carrito(self.luis, [(NEG, 1)])
        PG.registrar_pago(self.con, self.luis, vid, "efectivo_usd", 2000)
        PG.registrar_vuelto(self.con, self.luis, vid, "efectivo_bs", 10571)
        pos.cerrar_venta(self.con, vid, self.luis["id"])
        L = T.lineas_ticket(self.con, vid)
        self.assertTrue(all(len(x) <= T.COLS for x in L))
        self.assertTrue(any(x.strip().startswith("Vuelto Efectivo Bs") and x.rstrip().endswith("Bs 105,71") for x in L))

    def test_iva_incluido_en_etiqueta_del_ticket(self):
        self.con.execute("UPDATE config SET valor='incluido' WHERE clave='iva_modo_default'")
        vid = self.carrito(self.luis, [(NEG, 1)])
        PG.registrar_pago(self.con, self.luis, vid, "efectivo_usd", 1499); pos.cerrar_venta(self.con, vid, self.luis["id"])
        self.assertTrue(any(x.startswith("IVA incluido 16%") for x in T.lineas_ticket(self.con, vid)))

    def test_formato_de_tasa(self):
        self.assertEqual((T._tasa(40_500_000), T._tasa(42_123_456), T._tasa(40_000_000)), ("40,50", "42,123456", "40,00"))

    # ---------- bytes ESC/POS ----------
    def test_escpos_acentos_cajon_y_corte(self):
        b = T.a_escpos(["Niña á é"], "cp858")
        self.assertTrue(b.startswith(b"\x1b@\x1bt\x13"))
        self.assertIn("Niña á é".encode("cp858"), b)
        self.assertNotIn(T.CAJON, b)
        self.assertTrue(T.a_escpos(["x"], abrir_cajon=True).endswith(T.CAJON))
        self.assertIn(b"\x1dV", T.a_escpos(["x"], cortar=True))
        self.assertIn(b"?", T.a_escpos(["日"]))                                          # carácter no soportado: no falla
        with self.assertRaises(TicketError):
            T.a_escpos(["x"], "latin9000")

    # ---------- envío ----------
    def test_enviar_a_archivo_y_red(self):
        ruta = os.path.join(self.d.name, "p.bin")
        T.enviar(b"hola", f"archivo:{ruta}")
        T.enviar(b"mundo", f"archivo:{ruta}")
        self.assertEqual(leer(ruta), b"holamundo")
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        recibido = []

        def servir():
            c, _ = srv.accept(); recibido.append(c.recv(100)); c.close()
        th = threading.Thread(target=servir); th.start()
        T.enviar(b"ticket", f"red:127.0.0.1:{srv.getsockname()[1]}")
        th.join(3); srv.close()
        self.assertEqual(recibido, [b"ticket"])

    def test_errores_de_envio_son_claros(self):
        for destino, patron in (("", "No hay impresora"), ("ninguna", "No hay impresora"), ("wifi:1", "desconocido"),
                                ("red:127.0.0.1:1", "No se pudo imprimir"), ("windows:Termica", "pywin32"), ("serial:COM9", "pyserial")):
            with self.assertRaisesRegex(TicketError, patron):
                T.enviar(b"x", destino)

    def test_imprimir_ticket_abre_cajon_solo_con_efectivo(self):
        ruta = os.path.join(self.d.name, "t.bin")
        self.con.execute("INSERT INTO config(clave,valor) VALUES('impresora_ticket',?)", (f"archivo:{ruta}",))
        con_ef, sin_ef = self.venta_cerrada(True), self.venta_cerrada(False)
        self.assertTrue(T.imprimir_ticket(self.con, con_ef)["cajon"])
        self.assertTrue(leer(ruta).endswith(T.CAJON))
        os.remove(ruta)
        self.assertFalse(T.imprimir_ticket(self.con, sin_ef)["cajon"])
        self.assertFalse(T.imprimir_ticket(self.con, con_ef, reimpresion=True)["cajon"])
        self.con.execute("INSERT INTO config(clave,valor) VALUES('ticket_cajon','0')")
        self.assertFalse(T.imprimir_ticket(self.con, con_ef)["cajon"])
        os.remove(ruta)
        T.abrir_cajon(self.con)
        self.assertEqual(leer(ruta), b"\x1b@" + T.CAJON)


if __name__ == "__main__":
    unittest.main()
