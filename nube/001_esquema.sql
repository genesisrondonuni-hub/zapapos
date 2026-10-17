-- ZapaPOS · plataforma en la nube (Supabase / Postgres).
-- ESTADO: APLICADO el 2026-10-13 al proyecto 'zapapos-nube' (ref optnvredjvjibkkdhmxz, us-east-1) y probado en vivo con nube/prueba_seguridad.sql.
-- Avisos del linter de Supabase aceptados a propósito: (1) RLS sin políticas = acceso directo denegado; (2) sync_push/sync_estado ejecutables por 'anon' (la autorización la da el token).
-- Diseño: cada instalación (zapatería) envía eventos idempotentes; nadie lee ni escribe las tablas directamente.
-- Solo las funciones de abajo, que validan el token de la instalación, tocan los datos. Row Level Security activo y sin políticas = acceso directo denegado.

create extension if not exists pgcrypto with schema extensions;

create table if not exists public.instalaciones (
  id uuid primary key,                                   -- = instalacion_id local
  cliente text not null,
  plan text not null default 'basico' check (plan in ('basico','pro')),
  token_hash text not null,                              -- sha256 del token; el token en claro solo lo ve quien lo emite, una vez
  activa boolean not null default true,
  creado_en timestamptz not null default now(),
  ultimo_sync timestamptz
);

create table if not exists public.eventos (
  instalacion_id uuid not null references public.instalaciones(id) on delete cascade,
  uuid text not null,                                    -- uuid del evento en la caja: reenviar no duplica
  entidad text not null check (entidad in ('venta','devolucion','movimiento','catalogo')),
  operacion text not null default 'insert',
  payload jsonb not null,
  creado_local timestamptz,
  recibido_en timestamptz not null default now(),
  primary key (instalacion_id, uuid)
);
create index if not exists eventos_entidad_idx on public.eventos (instalacion_id, entidad, creado_local);

-- Eventos que la nube no pudo guardar: se anotan (no se pierden) y NO bloquean el resto del lote ni se reintentan para siempre.
create table if not exists public.eventos_rechazados (
  id bigint generated always as identity primary key,
  instalacion_id uuid not null, uuid text, entidad text, error text, payload jsonb, recibido_en timestamptz not null default now()
);

alter table public.instalaciones enable row level security;
alter table public.eventos enable row level security;
alter table public.eventos_rechazados enable row level security;
revoke all on public.instalaciones, public.eventos, public.eventos_rechazados from anon, authenticated;

-- Recibe un lote (máx. 500 eventos). Devuelve {"aceptados": [uuid, ...]} con los que ya quedaron guardados (nuevos o repetidos).
create or replace function public.sync_push(p_instalacion uuid, p_token text, p_eventos jsonb)
returns jsonb language plpgsql security definer set search_path = public, extensions as $$
declare e jsonb; aceptados text[] := '{}';
begin
  if p_eventos is null or jsonb_typeof(p_eventos) <> 'array' or jsonb_array_length(p_eventos) > 500 then
    raise exception 'lote inválido' using errcode = '22023';
  end if;
  perform 1 from public.instalaciones
   where id = p_instalacion and activa and token_hash = encode(digest(coalesce(p_token, ''), 'sha256'), 'hex');
  if not found then
    raise exception 'instalación o token inválidos' using errcode = '28000';
  end if;
  for e in select * from jsonb_array_elements(p_eventos) loop
    begin
      insert into public.eventos(instalacion_id, uuid, entidad, operacion, payload, creado_local)
      values (p_instalacion, e->>'uuid', e->>'entidad', coalesce(e->>'operacion', 'insert'), e->'payload', nullif(e->>'creado_en', '')::timestamptz)
      on conflict (instalacion_id, uuid) do nothing;
    exception when others then
      insert into public.eventos_rechazados(instalacion_id, uuid, entidad, error, payload) values (p_instalacion, e->>'uuid', e->>'entidad', sqlerrm, e);
    end;
    aceptados := aceptados || (e->>'uuid');                 -- también se confirman los rechazados: un evento defectuoso no debe atascar la cola
  end loop;
  update public.instalaciones set ultimo_sync = now() where id = p_instalacion;
  return jsonb_build_object('aceptados', to_jsonb(aceptados));
end $$;

create or replace function public.sync_estado(p_instalacion uuid, p_token text)
returns jsonb language plpgsql security definer set search_path = public, extensions as $$
begin
  perform 1 from public.instalaciones where id = p_instalacion and activa and token_hash = encode(digest(coalesce(p_token, ''), 'sha256'), 'hex');
  if not found then raise exception 'instalación o token inválidos' using errcode = '28000'; end if;
  return (select jsonb_build_object('ultimo_sync', i.ultimo_sync, 'eventos', (select count(*) from public.eventos where instalacion_id = i.id),
                                    'rechazados', (select count(*) from public.eventos_rechazados where instalacion_id = i.id))
            from public.instalaciones i where i.id = p_instalacion);
end $$;

-- Alta de una instalación (solo el proveedor): devuelve el token UNA vez; en la base queda solo su huella.
create or replace function public.registrar_instalacion(p_id uuid, p_cliente text, p_plan text default 'basico')
returns text language plpgsql security definer set search_path = public, extensions as $$
declare t text := encode(gen_random_bytes(32), 'hex');
begin
  insert into public.instalaciones(id, cliente, plan, token_hash) values (p_id, p_cliente, p_plan, encode(digest(t, 'sha256'), 'hex'));
  return t;
end $$;

revoke all on function public.sync_push(uuid, text, jsonb), public.sync_estado(uuid, text), public.registrar_instalacion(uuid, text, text) from public, anon, authenticated;
grant execute on function public.sync_push(uuid, text, jsonb), public.sync_estado(uuid, text) to anon;     -- las cajas llaman con la clave pública + su token
-- registrar_instalacion: sin permisos para anon/authenticated; solo con la clave de servicio o desde el editor SQL de su proyecto.
