-- Iteración A2: estado "descartado" en outbox
-- Eventos que superan el tope de intentos se marcan descartados y no bloquean la cola.
ALTER TABLE outbox ADD COLUMN descartado_en TEXT;
ALTER TABLE outbox ADD COLUMN motivo_descarte TEXT;
CREATE INDEX IF NOT EXISTS outbox_pendientes_idx ON outbox (id) WHERE descartado_en IS NULL AND enviado_en IS NULL;
