-- Asistente de IA del dueño. Las funciones SOLO las puede llamar la función Edge (service_role): anon NO tiene acceso.
-- La clave del proveedor de IA vive como secreto de la función Edge, nunca en la base ni en las cajas.

create table if not exists public.consultas_ia (
  id bigint generated always as identity primary key,
  instalacion_id uuid not null references public.instalaciones(id) on delete cascade,
  creada_en timestamptz not null default now(),
  tokens_entrada int, tokens_salida int, ok boolean not null default true, modelo text
);
create index if not exists consultas_ia_idx on public.consultas_ia (instalacion_id, creada_en);
alter table public.consultas_ia enable row level security;
revoke all on public.consultas_ia from anon, authenticated;

-- Valida token y plan, cuenta el uso de las últimas 24 h y arma un resumen compacto de los datos (lo único que ve la IA).
create or replace function public.ia_preparar(p_instalacion uuid, p_token text, p_dias int default 30, p_offset_min int default -240, p_max_diarias int default 30)
returns jsonb language plpgsql security definer set search_path = public, extensions as $$
declare inst record; usadas int; ctx jsonb; desde timestamptz := now() - make_interval(days => greatest(1, least(coalesce(p_dias, 30), 365)));
begin
  select * into inst from public.instalaciones where id = p_instalacion and activa and token_hash = encode(digest(coalesce(p_token,''),'sha256'),'hex');
  if not found then raise exception 'instalación o token inválidos' using errcode = '28000'; end if;
  select count(*) into usadas from public.consultas_ia where instalacion_id = p_instalacion and creada_en >= now() - interval '24 hours';

  with ult as (select payload from public.eventos where instalacion_id = p_instalacion and entidad = 'catalogo' order by creado_local desc nulls last, recibido_en desc limit 1),
       cat as (select v->>'sku' sku, concat_ws(' ', v->>'marca', v->>'modelo') producto, v->>'talla' talla, v->>'color' color, coalesce((v->>'stock_minimo')::int, 0) minimo
                 from ult, jsonb_array_elements(case when jsonb_typeof(ult.payload->'variantes') = 'array' then ult.payload->'variantes' else '[]'::jsonb end) v),
       ventas as (select * from public.eventos where instalacion_id = p_instalacion and entidad = 'venta' and creado_local >= desde)
  select jsonb_build_object(
    'dias_analizados', greatest(1, least(coalesce(p_dias, 30), 365)),
    'ventas_por_dia', coalesce((select jsonb_agg(x order by x->>'fecha') from (
        select jsonb_build_object('fecha', to_char((creado_local + make_interval(mins => p_offset_min))::date, 'YYYY-MM-DD'), 'ventas', count(*),
                                  'total_usd_cents', sum((payload->'venta'->>'total_usd_cents')::bigint)) x
          from ventas group by (creado_local + make_interval(mins => p_offset_min))::date) t), '[]'::jsonb),
    'mas_vendidos', coalesce((select jsonb_agg(x) from (
        select jsonb_build_object('sku', i->>'sku', 'producto', concat_ws(' ', max(i->>'marca'), max(i->>'modelo')), 'talla', max(i->>'talla'), 'color', max(i->>'color'),
                                  'unidades', sum((i->>'cantidad')::int), 'ingreso_usd_cents', sum((i->>'cantidad')::int * (i->>'precio_unit_usd_cents')::bigint)) x
          from ventas e, jsonb_array_elements(case when jsonb_typeof(e.payload->'items') = 'array' then e.payload->'items' else '[]'::jsonb end) i
         where i->>'sku' is not null
         group by i->>'sku'
         order by sum((i->>'cantidad')::int) desc limit 15) t), '[]'::jsonb),
    'stock_bajo', coalesce((select jsonb_agg(x) from (
        select jsonb_build_object('sku', m.sku, 'stock', m.stock, 'minimo', coalesce(c.minimo, 0), 'producto', c.producto, 'talla', c.talla, 'color', c.color) x
          from (select payload->>'sku' sku, sum((payload->>'cantidad')::int) stock from public.eventos
                 where instalacion_id = p_instalacion and entidad = 'movimiento' group by 1) m
          left join cat c on c.sku = m.sku
         where m.stock <= greatest(coalesce(c.minimo, 0), 2) order by m.stock limit 30) t), '[]'::jsonb),
    'pagos_por_metodo', coalesce((select jsonb_agg(x) from (
        select jsonb_build_object('metodo', p->>'metodo', 'pagos', count(*), 'total_usd_cents', sum((p->>'equiv_usd_cents')::bigint)) x
          from ventas e, jsonb_array_elements(case when jsonb_typeof(e.payload->'pagos') = 'array' then e.payload->'pagos' else '[]'::jsonb end) p
         group by p->>'metodo') t), '[]'::jsonb),
    'ultima_tasa_bs_por_usd_micro', (select (payload->'venta'->>'tasa_micro')::bigint from ventas where payload->'venta'->>'tasa_micro' is not null order by creado_local desc limit 1)
  ) into ctx;

  return jsonb_build_object('plan', inst.plan, 'usadas_24h', usadas, 'maximo_24h', p_max_diarias,
                            'permitido', inst.plan = 'pro' and usadas < p_max_diarias, 'contexto', ctx);
end $$;

create or replace function public.ia_registrar(p_instalacion uuid, p_tokens_entrada int, p_tokens_salida int, p_ok boolean, p_modelo text)
returns void language sql security definer set search_path = public as $$
  insert into public.consultas_ia(instalacion_id, tokens_entrada, tokens_salida, ok, modelo) values (p_instalacion, p_tokens_entrada, p_tokens_salida, p_ok, left(p_modelo, 80));
$$;

revoke all on function public.ia_preparar(uuid, text, int, int, int), public.ia_registrar(uuid, int, int, boolean, text) from public, anon, authenticated;
grant execute on function public.ia_preparar(uuid, text, int, int, int), public.ia_registrar(uuid, int, int, boolean, text) to service_role;
