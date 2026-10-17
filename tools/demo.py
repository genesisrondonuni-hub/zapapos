"""Demostración: crea una zapatería de ejemplo con datos ficticios y abre la caja.
   python -m tools.demo      ->  http://127.0.0.1:8787   (dueño: Marta / 4821 · cajero: Luis / 7305)"""
import os, sys
from tools.db import connect, migrate
from core import auth, inventario as I, negocio as N, pagos as PG, pos, pricing as PR, productos as P

CATALOGO = [("Sandalias", "Gamo", "Flor", "dama", ["36", "37", "38", "39"], ["negro", "rojo"], 1000),
            ("Sandalias", "Gamo", "Brisa", "dama", ["37", "38", "39"], ["beige"], 900),
            ("Tenis", "Rs21", "Veloz", "caballero", ["40", "41", "42", "43"], ["negro", "blanco"], 2200),
            ("Zapatos", "Gamo", "Oxford", "caballero", ["41", "42", "43"], ["marrón", "negro"], 2800),
            ("Botas", "Rs21", "Trek", "niño", ["28", "30", "32"], ["marrón"], 1800),
            ("Tenis", "Rs21", "Kids", "niña", ["28", "30", "32"], ["rosado", "blanco"], 1500)]
MARGENES = {"Sandalias": 4500, "Tenis": 4000, "Zapatos": 5000, "Botas": 5000}


def crear_demo(ruta, impresora="archivo:demo_ticket.bin"):
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(ruta + ext):
            os.remove(ruta + ext)
    con = connect(ruta); migrate(con)
    N.configurar_negocio(con, "Zapatería Demo", "J-12345678-9", "Av. Principal, local 4", "0414-0000000", "Marta", "4821", "Luis", "7305")
    N.actualizar_datos(con, auth.login(con, 1, "Marta", "4821"), {"impresora_ticket": impresora})
    dueno, cajero = auth.login(con, 1, "Marta", "4821"), auth.login(con, 1, "Luis", "7305")
    for cat, marca, modelo, genero, tallas, colores, costo in CATALOGO:
        P.crear_variantes(con, P.crear_producto(con, cat, marca, modelo, genero), tallas, colores, costo)
    for cid, nombre in con.execute("SELECT id, nombre FROM categorias").fetchall():
        PR.actualizar_margen_categoria(con, dueno, cid, MARGENES[nombre])
    PR.registrar_tasa(con, "bcv", "40,50", dueno["id"])                       # tasa de EJEMPLO
    PR.aplicar_recalculo(con, dueno, PR.previsualizar_recalculo(con, modo=".99"), "precios de demostración")
    for (vid,) in con.execute("SELECT id FROM variantes").fetchall():
        I.registrar_compra(con, dueno, 1, vid, 6)
    for sku, n, metodo in (("GAMO-BRISA-38-BEI", 1, "efectivo_usd"), ("RS21-KIDS-30-ROS", 2, "efectivo_usd"), ("GAMO-OXFORD-42-MAR", 1, "efectivo_usd")):
        vid = pos.abrir_venta(con, cajero)["venta_id"]; pos.escanear(con, vid, sku, n)
        PG.registrar_pago(con, cajero, vid, metodo, pos.ver_venta(con, vid)["pendiente_usd_cents"]); pos.cerrar_venta(con, vid, cajero["id"])
    con.close()
    return ruta


if __name__ == "__main__":
    from app.server import crear_servidor
    crear_demo("demo.db")
    s = crear_servidor("demo.db", 8787)
    print("Demo lista en http://127.0.0.1:8787  (Luis/7305 cajero · Marta/4821 dueño). Ctrl+C para salir.")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
