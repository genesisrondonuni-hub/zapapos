-- Consultas para la app del dueño. Solo lectura, mediante funciones con security definer (mismo patrón que sync_push).

-- Resumen del día: ventas, cobros por método, top productos.
create or replace function public.resumen_dia(p_instalacion uuid, p_token text, p_fecha date default current_date, p_offset_min int default -240)
returns jsonb language plpgsql security definer set search_path = public, extensions as $$
declare ini timestamptz; fin timestamptz;
begin
  perform 1 from public.instalaciones where id = p_instalacion and activa and token_hash = encode(digest(coalesce(p_token,''),'sha256'),'hex');
  if not found then raise exception 'instalación o token inválidos' using errcode = '28000'; end if;
  ini := p_fecha::timestamptz - make_interval(mins => -p_offset_min);
  fin := ini + interval '1 day';
  return (
    with v as (select payload from public.eventos where instalacion_id=p_instalacion and entidad='venta' and creado_local >= ini and creado_local < fin),
         p as (select payload from public.eventos where instalacion_id=p_instalacion and entidad='movimiento' and payload->>'tipo'='compra' and creado_local >= ini and creado_local < fin)
    select jsonb_build_object(
      'ventas_n', (select count(*) from v),
      'total_usd_cents', (select coalesce(sum((payload->>'total_usd_cents')::int),0) from v),
      'total_bs_centimos', (select coalesce(sum((payload->>'total_bs_centimos')::int),0) from v),
      'movimientos_n', (select count(*) from p),
      'generado_en', now()
    )
  );
end $$;

-- Estado de sincronización y alertas de stock crítico.
create or replace function public.estado_instalacion(p_instalacion uuid, p_token text)
returns jsonb language plpgsql security definer set search_path = public, extensions as $$
begin
  perform 1 from public.instalaciones where id = p_instalacion and activa and token_hash = encode(digest(coalesce(p_token,''),'sha256'),'hex');
  if not found then raise exception 'instalación o token inválidos' using errcode = '28000'; end if;
  return (select jsonb_build_object(
    'ultimo_sync', i.ultimo_sync,
    'horas_sin_sync', round(extract(epoch from (now() - i.ultimo_sync))/3600, 1),
    'eventos_total', (select count(*) from public.eventos where instalacion_id = i.id),
    'eventos_rechazados', (select count(*) from public.eventos_rechazados where instalacion_id = i.id),
    'plan', i.plan
  ) from public.instalaciones i where i.id = p_instalacion);
end $$;

revoke all on function public.resumen_dia(uuid,text,date,int), public.estado_instalacion(uuid,text) from public, anon, authenticated;
grant execute on function public.resumen_dia(uuid,text,date,int), public.estado_instalacion(uuid,text) to anon;
