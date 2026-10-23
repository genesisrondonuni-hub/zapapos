-- Iteración A3: separar momento del hecho (ocurrido_en) del momento de encolado (creado_en)
ALTER TABLE outbox ADD COLUMN ocurrido_en TEXT;
UPDATE outbox SET ocurrido_en = creado_en WHERE ocurrido_en IS NULL;
