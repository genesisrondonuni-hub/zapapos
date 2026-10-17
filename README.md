# ZapaPOS — POS + inventario para zapaterías en Venezuela

Sistema **offline-first** (SQLite local, la caja nunca depende de internet) con nube opcional (plan Pro).
Doble moneda USD/Bs con la tasa guardada en cada venta, pagos divididos (Pago Móvil, Zelle, USDT, Binance Pay, efectivo, punto),
IVA, devoluciones, etiquetas Code128, ticket térmico 58 mm, reportes, respaldos, licencias de mantenimiento y actualizaciones firmadas.

> Repositorio **privado**: es un producto comercial. No publique claves: las de firma (`clave_privada.pem`), la `service_role` de Supabase
> y las claves de IA **nunca** van al código (ver `.gitignore`).

## Estructura
| Carpeta | Contenido |
|---|---|
| `core/` | Lógica de negocio: `auth`, `productos`, `pricing`, `inventario`, `pos`, `pagos`, `reportes`, `etiquetas`, `ticket`, `fiscal`, `negocio`, `licencia` |
| `app/` | Servidor local de la caja (`server.py`, sin dependencias), pantalla de caja (`static/index.html`) y panel del dueño (`dueno.html`) |
| `database/migrations/` | Migraciones SQL numeradas (001–007) |
| `tools/` | `db`, `respaldo`, `actualizar`, `sync`, `demo` |
| `nube/` | Esquema Supabase (`001`–`003`), pruebas de seguridad SQL y la función Edge `asistente` |
| `instalador/` | `instalar.bat` y `iniciar.py` para Windows (**sin probar en Windows todavía**) |
| `tests/` | 247 pruebas (incluye pantallas en navegador real y la función Edge con Node) |

## Cómo correrlo
```bash
pip install -r requirements.txt
python -m tools.demo                     # demo con datos ficticios en http://127.0.0.1:8787  (Luis/7305 cajero · Marta/4821 dueño)
python -m app.server --db zapateria.db   # caja real (primer arranque: asistente de configuración)
python -m unittest discover -s tests -v  # pruebas
node --test nube/funciones/asistente/test.mjs
```

## Configuración del servidor del proveedor

El servidor del proveedor configura la URL centralizada de Supabase y su clave
pública mediante variables de entorno. El cliente final no debe configurar
estos valores:

```bash
export ZAPAPOS_URL_NUBE="https://xxxx.supabase.co"
export ZAPAPOS_APIKEY="sb_publishable_xxxx"
python -m app.server --db zapateria.db
```

El panel del dueño debe abrirse mediante `server.py` en `/dueno`. No se debe
abrir `app/dueno.html` directamente con `file://`, porque los valores del
proveedor se inyectan al servir el template. Si las variables no están
configuradas, el panel muestra un aviso de soporte y conserva las tiendas
locales en modo de solo lectura.

La clave pública no es una `service_role`: cada instalación continúa aislada
por `instalacion_id` y token. `.env.example` contiene únicamente nombres de
variables vacíos para desarrollo; nunca se deben guardar tokens o claves
secretas en el repositorio.

La prueba de integración real con Supabase se omite si no están definidas
`ZAPAPOS_URL_NUBE`, `ZAPAPOS_APIKEY` y `ZAPAPOS_NUBE_TOKEN`.

## Estado (ver `docs/ESTADO.md`)
Completo y probado: motor de ventas, pagos, inventario, precios, etiquetas, reportes, respaldos/restauración, actualizaciones firmadas,
licencias, base fiscal, sincronización a la nube, panel del dueño y asistente de IA (función Edge, proveedor configurable).
Pendiente: adaptador fiscal real, conciliación bancaria, WhatsApp, pruebas en hardware/Windows reales.
