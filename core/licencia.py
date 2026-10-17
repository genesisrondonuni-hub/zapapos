"""Licencia de mantenimiento. Firmada (Ed25519), atada a la instalación y válida sin internet.
REGLA DE ORO: una licencia vencida NUNCA bloquea ventas, cobros, facturación, respaldos ni restauración. Solo afecta
lo que el mantenimiento incluye: actualizaciones, soporte y funciones Pro. Emisión: python -m core.licencia emitir ..."""
import base64, json, os, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from core.auth import auditar

PRUEBA_DIAS, GRACIA_DIAS, AVISO_DIAS = 30, 30, 15
SIEMPRE = frozenset({"ventas", "cobros", "devoluciones", "fiscal", "caja", "reportes", "etiquetas", "respaldos", "restauracion"})
PLANES = {"basico": {"sucursales": 1, "funciones": []},
          "pro": {"sucursales": 3, "funciones": ["nube", "app_dueno", "whatsapp", "asistente_ia", "conciliacion"]}}
CLAVE_PUBLICA = None                      # la fija el servidor al arrancar (ruta al PEM de la instalación)


class LicenciaError(Exception):
    pass


def clave_publica(db_path=None):
    """Orden: variable ZAPAPOS_CLAVE_PUBLICA, la fijada por el servidor, y <instalación>/clave_publica.pem junto a datos/."""
    cand = [os.environ.get("ZAPAPOS_CLAVE_PUBLICA"), CLAVE_PUBLICA]
    if db_path:
        cand.append(str(Path(db_path).resolve().parent.parent / "clave_publica.pem"))
    return next((c for c in cand if c and os.path.exists(c)), None)


def _firmable(payload):
    return b"ZAPAPOS-LICENCIA-v1\n" + json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")   # prefijo: no se confunde con otras firmas


def _fecha(t):
    try:
        return date.fromisoformat(str(t))
    except ValueError:
        raise LicenciaError(f"Fecha inválida: {t!r} (use AAAA-MM-DD)")


def emitir(clave_privada, instalacion_id, cliente, plan, vence, sucursales_max=None, funciones=None) -> dict:
    if plan not in PLANES:
        raise LicenciaError(f"Plan desconocido: {plan!r} (opciones: {', '.join(PLANES)})")
    _fecha(vence)
    if not instalacion_id or not (cliente or "").strip():
        raise LicenciaError("Se necesita el código de instalación y el nombre del cliente")
    payload = {"version_licencia": 1, "instalacion_id": instalacion_id, "cliente": cliente.strip(), "plan": plan, "vence": str(vence),
               "sucursales_max": int(sucursales_max or PLANES[plan]["sucursales"]), "funciones": sorted(set(funciones or [])),
               "emitida": datetime.now(timezone.utc).date().isoformat()}
    k = serialization.load_pem_private_key(Path(clave_privada).read_bytes(), None)
    return {"payload": payload, "firma": base64.b64encode(k.sign(_firmable(payload))).decode()}


def verificar(lic, clave_pem, instalacion_id) -> dict:
    if not clave_pem:
        raise LicenciaError("No se encontró la clave pública de su proveedor en esta instalación")
    try:
        payload, firma = lic["payload"], base64.b64decode(lic["firma"])
        try:
            serialization.load_pem_public_key(Path(clave_pem).read_bytes()).verify(firma, _firmable(payload))
        except InvalidSignature:
            raise LicenciaError("Licencia inválida: la firma no corresponde a su proveedor")
        if payload["version_licencia"] != 1 or payload["plan"] not in PLANES:
            raise LicenciaError("Licencia de una versión no soportada")
        _fecha(payload["vence"]); int(payload["sucursales_max"]); list(payload["funciones"])
    except (KeyError, TypeError, ValueError, OSError) as e:
        raise LicenciaError(f"La licencia está incompleta o dañada: {e}") from e
    if payload["instalacion_id"] != instalacion_id:
        raise LicenciaError("Esta licencia es de otra instalación")
    return payload


def _get(con, k):
    r = con.execute("SELECT valor FROM config WHERE clave=?", (k,)).fetchone()
    return r[0] if r else None


def _set(con, k, v):
    con.execute("INSERT INTO config(clave,valor) VALUES(?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (k, v))


def _hoy(con, ahora):
    """Fecha local efectiva. El 'último día visto' impide ganar tiempo atrasando el reloj de la PC."""
    ahora = ahora or datetime.now(timezone.utc)
    off = _get(con, "utc_offset_min")
    hoy = (ahora + timedelta(minutes=int(off) if off else -240)).date()
    u = _get(con, "licencia_ultimo_visto")
    ult = date.fromisoformat(u) if u else None
    efectivo = max(hoy, ult) if ult else hoy
    if not ult or efectivo > ult:
        _set(con, "licencia_ultimo_visto", efectivo.isoformat())
    return efectivo, bool(ult and ult > hoy + timedelta(days=1))


def instalar(con, texto, clave_pem, ahora=None) -> dict:
    try:
        lic = json.loads(texto)
    except (TypeError, ValueError):
        raise LicenciaError("El texto no es una licencia válida: péguela completa")
    iid = _get(con, "instalacion_id")
    if not iid:
        raise LicenciaError("Esta instalación todavía no está configurada")
    p = verificar(lic, clave_pem, iid)
    _set(con, "licencia", json.dumps(lic, ensure_ascii=False, sort_keys=True))
    auditar(con, None, "licencia_instalada", f"{p['plan']} hasta {p['vence']}")
    return estado(con, clave_pem, ahora)


def estado(con, clave_pem=None, ahora=None) -> dict:
    hoy, atrasado = _hoy(con, ahora)
    base = {"reloj_atrasado": atrasado, "instalacion_id": _get(con, "instalacion_id"), "cliente": None, "vence": None, "funciones": [],
            "plan": "basico", "sucursales_max": 1}
    txt = _get(con, "licencia")
    if not txt:
        pu = _get(con, "licencia_primer_uso")
        if not pu:
            pu = hoy.isoformat(); _set(con, "licencia_primer_uso", pu)
        rest = PRUEBA_DIAS - (hoy - date.fromisoformat(pu)).days
        if rest >= 0:
            return {**base, "estado": "prueba", "dias_restantes": rest, "puede_actualizar": True, "alerta": rest <= 7, "plan": "prueba",
                    "mensaje": f"Período de prueba: quedan {rest} día(s). Active su licencia para seguir con actualizaciones y soporte"}
        return {**base, "estado": "vencida", "dias_restantes": rest, "puede_actualizar": False, "alerta": True,
                "mensaje": "El período de prueba terminó: active su licencia. Puede seguir vendiendo con normalidad"}
    try:
        p = verificar(json.loads(txt), clave_pem, base["instalacion_id"])
    except LicenciaError as e:
        sin = "clave pública" in str(e)
        return {**base, "estado": "sin_clave" if sin else "invalida", "dias_restantes": None, "puede_actualizar": sin, "alerta": not sin,
                "mensaje": ("No se puede verificar la licencia en esta instalación" if sin else f"{e}. Puede seguir vendiendo con normalidad")}
    dias = (_fecha(p["vence"]) - hoy).days
    est = "activa" if dias >= 0 else "gracia" if dias >= -GRACIA_DIAS else "vencida"
    msg = {"activa": f"Mantenimiento al día hasta {p['vence']}" + (f" (vence en {dias} día(s): renueve)" if dias <= AVISO_DIAS else ""),
           "gracia": f"Su mantenimiento venció el {p['vence']}: renueve para no perder actualizaciones y soporte",
           "vencida": f"Mantenimiento vencido desde el {p['vence']}: sin actualizaciones ni soporte. Puede seguir vendiendo con normalidad"}[est]
    return {**base, "estado": est, "plan": p["plan"], "cliente": p["cliente"], "vence": p["vence"], "dias_restantes": dias,
            "sucursales_max": p["sucursales_max"], "funciones": sorted(set(PLANES[p["plan"]]["funciones"]) | set(p["funciones"])),
            "puede_actualizar": est in ("activa", "gracia"), "alerta": est != "activa" or dias <= AVISO_DIAS, "mensaje": msg}


def permite(con, funcion, clave_pem=None, ahora=None) -> bool:
    """Las funciones esenciales SIEMPRE se permiten. Las demás (Pro) requieren licencia vigente que las incluya, o prueba."""
    if funcion in SIEMPRE:
        return True
    e = estado(con, clave_pem, ahora)
    return e["estado"] == "prueba" or (e["estado"] in ("activa", "gracia") and funcion in e["funciones"])


if __name__ == "__main__":
    a = sys.argv[1:]
    try:
        if a[:1] == ["emitir"] and len(a) >= 4:
            op = lambda k, d=None: a[a.index(k) + 1] if k in a else d
            lic = emitir(a[1], op("--instalacion"), op("--cliente"), op("--plan", "basico"), op("--vence"), op("--sucursales"))
            Path(a[2]).write_text(json.dumps(lic, indent=1, ensure_ascii=False)); print("Licencia creada:", a[2], lic["payload"]["vence"])
        elif a[:1] == ["ver"] and len(a) == 2:
            print(json.dumps(json.loads(Path(a[1]).read_text())["payload"], indent=1, ensure_ascii=False))
        else:
            print("Uso: emitir CLAVE_PRIVADA SALIDA.json --instalacion ID --cliente NOMBRE --plan basico|pro --vence AAAA-MM-DD [--sucursales N] | ver LICENCIA.json")
            sys.exit(2)
    except (LicenciaError, OSError, ValueError) as e:
        print("ERROR:", e); sys.exit(1)
