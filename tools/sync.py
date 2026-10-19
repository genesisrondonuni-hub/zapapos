"""Sincronización hacia la nube (solo envío por ahora). La caja NUNCA espera a la nube: todo se anota primero en la cola local (outbox)
y se envía cuando hay conexión, con reintentos espaciados. Cada evento lleva un uuid: reenviarlo no duplica nada.
Contrato del transporte: push(eventos: list[dict]) -> list[str] con los uuid que la nube confirmó (aceptados o ya recibidos)."""
import hashlib, json, urllib.error, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from core import licencia as LIC
from core.reportes import _iso
from tools.db import connect, transaccion

BACKOFF_BASE, BACKOFF_MAX = 60, 6 * 3600           # segundos: 1 min, 2, 4 ... hasta 6 h


class SyncError(Exception):
    pass


def encolar_catalogo(con) -> bool:
    """Encola una foto completa del catálogo (nombres, precios, costos).

    Idempotencia:
      - La huella SHA-256 del JSON canónico del catálogo evita reenvíos cuando
        el catálogo no cambió. Si coincide con la última foto, no se encola nada
        y la función devuelve False.
      - Si el catálogo cambió, se encola un evento con UUID único generado con
        uuid4. El UUID no deriva de la huella: esto conserva correctamente el
        escenario A → B → A, en el que deben existir tres eventos con tres UUIDs
        distintos y el catálogo A más reciente debe prevalecer en la nube según
        creado_local descendente.

    Nota de diseño:
      Cada cambio de catálogo inserta una fila nueva en `public.eventos` de la
      nube. Con aproximadamente 100 cambios mensuales son unas 1200 filas al
      año por cliente, lo cual es aceptable. Si en el futuro la tabla crece
      demasiado, se puede migrar a un UPSERT en `sync_push` con UUID basado en
      la huella y timestamp, actualizando `creado_local` en el conflicto.
      Ese cambio requeriría modificar el SQL de la nube y validarlo manualmente.
    """
    filas = con.execute("SELECT v.sku,m.nombre,p.modelo,c.nombre,p.genero,v.talla,v.color,v.costo_usd_cents,v.precio_usd_cents,v.stock_minimo,v.activo "
                        "FROM variantes v JOIN productos p ON p.id=v.producto_id JOIN marcas m ON m.id=p.marca_id JOIN categorias c ON c.id=p.categoria_id "
                        "ORDER BY v.sku").fetchall()
    if not filas:
        return False
    cols = ("sku", "marca", "modelo", "categoria", "genero", "talla", "color", "costo_usd_cents", "precio_usd_cents", "stock_minimo", "activo")
    cuerpo = json.dumps([dict(zip(cols, f)) for f in filas], ensure_ascii=False, sort_keys=True)
    huella = hashlib.sha256(cuerpo.encode()).hexdigest()
    with transaccion(con):
        ultima = con.execute("SELECT valor FROM config WHERE clave='sync_catalogo_huella'").fetchone()
        if ultima and ultima[0] == huella:
            return False                                              # nada cambió desde la última foto
        con.execute("INSERT INTO outbox(uuid,entidad,operacion,payload) VALUES(?,?,?,?)",
                    (f"catalogo:{uuid.uuid4().hex}", "catalogo", "insert", json.dumps({"variantes": json.loads(cuerpo)}, ensure_ascii=False)))
        con.execute("INSERT INTO config(clave,valor) VALUES('sync_catalogo_huella',?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (huella,))
        return True


def _espera(intentos):
    return min(BACKOFF_BASE * 2 ** min(intentos, 20), BACKOFF_MAX)


def estado(con) -> dict:
    g = lambda w: con.execute(f"SELECT COUNT(*) FROM outbox WHERE {w}").fetchone()[0]
    u = con.execute("SELECT MAX(enviado_en) FROM outbox").fetchone()[0]
    return {"pendientes": g("enviado_en IS NULL"), "enviados": g("enviado_en IS NOT NULL"), "con_reintentos": g("enviado_en IS NULL AND intentos>0"), "ultimo_envio": u}


def enviar_lote(con, transporte, ahora=None, lote=100) -> dict:
    ahora = ahora or datetime.now(timezone.utc)
    filas = con.execute("SELECT id,uuid,entidad,operacion,payload,creado_en,intentos FROM outbox WHERE enviado_en IS NULL AND "
                        "(proximo_intento IS NULL OR proximo_intento<=?) ORDER BY id LIMIT ?", (_iso(ahora), lote)).fetchall()
    if not filas:
        return {"enviados": 0, "fallidos": 0, "error": None}
    eventos = [{"uuid": u, "entidad": e, "operacion": o, "payload": json.loads(p), "creado_en": c} for _, u, e, o, p, c, _ in filas]
    try:
        acks = set(transporte.push(eventos))
        error = None
    except Exception as e:                                           # sin internet, nube caída, token inválido...
        acks, error = set(), str(e)[:300]
    ok = [f for f in filas if f[1] in acks]
    mal = [f for f in filas if f[1] not in acks]
    with transaccion(con):
        for f in ok:
            con.execute("UPDATE outbox SET enviado_en=? WHERE id=?", (_iso(ahora), f[0]))
        for f in mal:
            con.execute("UPDATE outbox SET intentos=intentos+1, proximo_intento=? WHERE id=?", (_iso(ahora + timedelta(seconds=_espera(f[6]))), f[0]))
    return {"enviados": len(ok), "fallidos": len(mal), "error": error}


def sincronizar(con, transporte, ahora=None, max_lotes=20) -> dict:
    """Envía todo lo pendiente por lotes; se detiene en el primer fallo (probablemente no hay conexión) para reintentar más tarde."""
    encolar_catalogo(con)
    tot = {"enviados": 0, "fallidos": 0, "error": None}
    for _ in range(max_lotes):
        r = enviar_lote(con, transporte, ahora)
        tot["enviados"] += r["enviados"]; tot["fallidos"] += r["fallidos"]
        if r["error"] or r["fallidos"]:
            tot["error"] = r["error"]; break
        if r["enviados"] == 0:
            break
    return tot


class TransporteHTTP:
    """Llama a la función sync_push de la nube (PostgREST). La clave 'apikey' es pública; lo que autoriza es el token de la instalación."""
    def __init__(self, url, apikey, instalacion_id, token, timeout=20):
        self.url, self.apikey, self.iid, self.token, self.timeout = url.rstrip("/"), apikey, instalacion_id, token, timeout

    def push(self, eventos):
        cuerpo = json.dumps({"p_instalacion": self.iid, "p_token": self.token, "p_eventos": eventos}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.url}/rest/v1/rpc/sync_push", data=cuerpo, method="POST",
                                     headers={"apikey": self.apikey, "Authorization": f"Bearer {self.apikey}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                datos = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise SyncError(f"La nube respondió {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SyncError(f"Sin conexión con la nube: {e}") from e
        if not isinstance(datos, dict) or not isinstance(datos.get("aceptados"), list):
            raise SyncError("Respuesta inesperada de la nube")
        return datos["aceptados"]


def transporte_desde_config(con):
    g = lambda k: (con.execute("SELECT valor FROM config WHERE clave=?", (k,)).fetchone() or [""])[0]
    url, key, tok, iid = g("nube_url"), g("nube_apikey"), g("nube_token"), g("instalacion_id")
    return TransporteHTTP(url, key, iid, tok) if url and key and tok and iid else None


def ciclo(db_path, transporte=None):
    """Una vuelta de sincronización. Devuelve None si no corresponde (sin configurar o sin la función 'nube' en la licencia)."""
    con = connect(db_path)
    try:
        t = transporte or transporte_desde_config(con)
        if not t or not LIC.permite(con, "nube", LIC.clave_publica(db_path)):
            return None
        return sincronizar(con, t)
    finally:
        con.close()


def iniciar_sync(db_path, intervalo=30):
    """Hilo de fondo: reintenta cada tanto; los errores se anotan y nunca afectan a la caja."""
    import sys, threading
    parar = threading.Event()

    def bucle():
        while not parar.is_set():
            try:
                ciclo(db_path)
            except Exception as e:
                print(f"[sync] error: {e}", file=sys.stderr)
            parar.wait(intervalo)
    h = threading.Thread(target=bucle, daemon=True); h.start()
    return h, parar
