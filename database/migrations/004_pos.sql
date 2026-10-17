-- 004: POS. Carrito editable solo mientras la venta está 'abierta'; cerrada/anulada es inmutable.
ALTER TABLE ventas ADD COLUMN cerrada_en TEXT;
ALTER TABLE ventas ADD COLUMN anulada_motivo TEXT;

DROP TRIGGER trg_item_no_delete;
CREATE TRIGGER trg_item_no_delete BEFORE DELETE ON venta_items
WHEN (SELECT estado FROM ventas WHERE id=OLD.venta_id) <> 'abierta'
BEGIN SELECT RAISE(ABORT, 'venta cerrada: sus ítems no se modifican'); END;
CREATE TRIGGER trg_item_no_update BEFORE UPDATE ON venta_items
WHEN (SELECT estado FROM ventas WHERE id=OLD.venta_id) <> 'abierta'
BEGIN SELECT RAISE(ABORT, 'venta cerrada: sus ítems no se modifican'); END;
CREATE TRIGGER trg_item_no_insert BEFORE INSERT ON venta_items
WHEN (SELECT estado FROM ventas WHERE id=NEW.venta_id) <> 'abierta'
BEGIN SELECT RAISE(ABORT, 'venta cerrada: no se pueden agregar ítems'); END;
CREATE TRIGGER trg_pago_no_insert BEFORE INSERT ON pagos
WHEN (SELECT estado FROM ventas WHERE id=NEW.venta_id) <> 'abierta'
BEGIN SELECT RAISE(ABORT, 'venta cerrada: no se pueden agregar pagos'); END;
CREATE TRIGGER trg_venta_cerrada_inmutable BEFORE UPDATE ON ventas
WHEN OLD.estado <> 'abierta'
BEGIN SELECT RAISE(ABORT, 'venta cerrada o anulada: use devolución'); END;

-- Devoluciones (base de la futura nota de crédito fiscal).
CREATE TABLE devoluciones (
  id INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL UNIQUE,
  venta_id INTEGER NOT NULL REFERENCES ventas(id),
  usuario_id INTEGER REFERENCES usuarios(id),
  motivo TEXT NOT NULL CHECK (length(trim(motivo)) > 0),
  subtotal_usd_cents INTEGER NOT NULL,
  iva_usd_cents INTEGER NOT NULL,
  total_usd_cents INTEGER NOT NULL,
  total_bs_centimos INTEGER NOT NULL,
  tasa_micro INTEGER NOT NULL CHECK (tasa_micro > 0),     -- tasa de la venta original
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE devolucion_items (
  id INTEGER PRIMARY KEY,
  devolucion_id INTEGER NOT NULL REFERENCES devoluciones(id),
  variante_id INTEGER NOT NULL REFERENCES variantes(id),
  cantidad INTEGER NOT NULL CHECK (cantidad > 0),
  precio_unit_usd_cents INTEGER NOT NULL
);
CREATE TRIGGER trg_dev_no_update BEFORE UPDATE ON devoluciones BEGIN SELECT RAISE(ABORT, 'devoluciones son inmutables'); END;
CREATE TRIGGER trg_dev_no_delete BEFORE DELETE ON devoluciones BEGIN SELECT RAISE(ABORT, 'devoluciones son inmutables'); END;
CREATE TRIGGER trg_devi_no_update BEFORE UPDATE ON devolucion_items BEGIN SELECT RAISE(ABORT, 'devoluciones son inmutables'); END;
CREATE TRIGGER trg_devi_no_delete BEFORE DELETE ON devolucion_items BEGIN SELECT RAISE(ABORT, 'devoluciones son inmutables'); END;
