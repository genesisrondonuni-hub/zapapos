-- Prueba de seguridad y de idempotencia de la nube. Se ejecuta con el editor SQL del proyecto y TERMINA con un error a propósito
-- ('RESULTADO {...}') para deshacer todo lo que creó: no deja datos. Resultado esperado (verificado el 2026-10-13):
--   t1_aceptados=3, t2_reenvio_aceptados=3, en_tabla_eventos=2 (el reenvío no duplica), en_rechazados>=1 (evento defectuoso aislado),
--   t3/t4/t11 = 28000 (token malo, token de otra tienda, instalación desactivada), t5/t6 = 22023 (lote inválido o de 501),
--   t7/t8/t9 = 'bloqueada' (anon no lee, no escribe ni da de alta), token_en_claro_guardado=0.
do $$
declare
  iid uuid := '11111111-1111-1111-1111-111111111111';
  otra uuid := '22222222-2222-2222-2222-222222222222';
  tok text; tok2 text; res jsonb := '{}'::jsonb; r jsonb;
  lote jsonb := '[{"uuid":"a1","entidad":"movimiento","operacion":"insert","payload":{"sku":"GAMO-FLOR-38-NEG","cantidad":10},"creado_en":"2026-10-10T15:00:00.000Z"},
                  {"uuid":"a2","entidad":"venta","operacion":"insert","payload":{"total_usd_cents":1739}},
                  {"uuid":"a3","entidad":"raro","operacion":"insert","payload":{}}]'::jsonb;
begin
  tok := public.registrar_instalacion(iid, 'Prueba ZapaPOS', 'pro');
  tok2 := public.registrar_instalacion(otra, 'Otra tienda', 'basico');
  set local role anon;
  r := public.sync_push(iid, tok, lote);
  res := res || jsonb_build_object('t1_aceptados', jsonb_array_length(r->'aceptados'));
  r := public.sync_push(iid, tok, lote);
  res := res || jsonb_build_object('t2_reenvio_aceptados', jsonb_array_length(r->'aceptados'));
  begin perform public.sync_push(iid, 'token-malo', '[]'::jsonb); res := res || jsonb_build_object('t3_token_malo', 'NO RECHAZADO'); exception when others then res := res || jsonb_build_object('t3_token_malo', sqlstate); end;
  begin perform public.sync_push(iid, tok2, '[]'::jsonb); res := res || jsonb_build_object('t4_token_de_otra_tienda', 'NO RECHAZADO'); exception when others then res := res || jsonb_build_object('t4_token_de_otra_tienda', sqlstate); end;
  begin perform public.sync_push(iid, tok, '{"no":"array"}'::jsonb); res := res || jsonb_build_object('t5_no_es_lista', 'NO RECHAZADO'); exception when others then res := res || jsonb_build_object('t5_no_es_lista', sqlstate); end;
  begin perform public.sync_push(iid, tok, (select jsonb_agg(jsonb_build_object('uuid', g::text, 'entidad', 'movimiento', 'payload', '{}'::jsonb)) from generate_series(1, 501) g)); res := res || jsonb_build_object('t6_lote_501', 'NO RECHAZADO'); exception when others then res := res || jsonb_build_object('t6_lote_501', sqlstate); end;
  begin perform count(*) from public.eventos; res := res || jsonb_build_object('t7_lectura_directa', 'NO BLOQUEADA'); exception when insufficient_privilege then res := res || jsonb_build_object('t7_lectura_directa', 'bloqueada'); end;
  begin insert into public.eventos(instalacion_id, uuid, entidad, payload) values (iid, 'x', 'venta', '{}'); res := res || jsonb_build_object('t8_escritura_directa', 'NO BLOQUEADA'); exception when insufficient_privilege then res := res || jsonb_build_object('t8_escritura_directa', 'bloqueada'); end;
  begin perform public.registrar_instalacion(gen_random_uuid(), 'intruso'); res := res || jsonb_build_object('t9_alta_por_anon', 'NO BLOQUEADA'); exception when insufficient_privilege then res := res || jsonb_build_object('t9_alta_por_anon', 'bloqueada'); end;
  r := public.sync_estado(iid, tok);
  res := res || jsonb_build_object('t10_estado', r - 'ultimo_sync');
  reset role;
  update public.instalaciones set activa = false where id = iid;
  set local role anon;
  begin perform public.sync_push(iid, tok, '[]'::jsonb); res := res || jsonb_build_object('t11_instalacion_desactivada', 'NO RECHAZADA'); exception when others then res := res || jsonb_build_object('t11_instalacion_desactivada', sqlstate); end;
  reset role;
  res := res || jsonb_build_object('en_tabla_eventos', (select count(*) from public.eventos where instalacion_id = iid),
                                   'en_rechazados', (select count(*) from public.eventos_rechazados where instalacion_id = iid),
                                   'token_en_claro_guardado', (select count(*) from public.instalaciones where token_hash = tok));
  raise exception 'RESULTADO %', res::text;
end $$;
