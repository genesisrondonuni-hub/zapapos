# Nube ZapaPOS (Supabase)

Proyecto: `<nombre-proyecto>` · ref `<ref-proyecto>` · región `<region>` · URL: `https://<ref-proyecto>.supabase.co`

## Dar de alta una zapatería (solo el proveedor)
1. En el editor SQL del proyecto: `select public.registrar_instalacion('<instalacion_id de la caja>', 'Nombre del cliente', 'pro');`
   Devuelve un **token** (se muestra una sola vez; en la base queda solo su huella). Guárdalo en tu gestor de claves.
2. En la caja de ese cliente (dueño → Datos del negocio, o `actualizar_datos`): `nube_url`, `nube_apikey` (clave pública "anon/publishable" del proyecto: Project Settings → API) y `nube_token`.
3. La caja envía sola cuando hay internet y la licencia incluye la función `nube` (plan Pro o prueba).

## Seguridad (probada en vivo: `prueba_seguridad.sql`)
- Las tablas tienen RLS sin políticas: nadie las lee ni escribe directamente con la clave pública.
- Solo `sync_push` y `sync_estado` pueden ejecutarse con la clave pública, y exigen el token de esa instalación.
- La clave `service_role` NUNCA va en una caja ni en el código.

## URL y clave pública (para configurar cada caja)
- URL: `https://<ref-proyecto>.supabase.co`
- Clave pública (apikey / anon, para copiar en Datos del negocio → nube_apikey):
  `sb_publishable_<tu-clave-publica>`
  Esta clave es pública. Nunca ponga `service_role` en ninguna caja.

## Registrar una nueva instalación
```sql
-- En el editor SQL del proyecto (con la clave service_role; solo el proveedor):
select public.registrar_instalacion(
  '<pegar el instalacion_id de la caja>'::uuid,
  'Nombre de la zapatería',
  'pro'         -- o 'basico'
);
-- Devuelve el token: cópialo una sola vez y dáselo al cliente para que lo pegue en la caja.
```

## Prueba de integración real
```bash
ZAPAPOS_NUBE_TOKEN=<token_de_esa_instalacion> \
ZAPAPOS_NUBE_INSTALACION_ID=<instalacion_id> \
python -m unittest tests.test_sync_real -v
```
