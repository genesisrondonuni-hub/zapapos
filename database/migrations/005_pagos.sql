-- 005: pagos. Verificación de pagos digitales, anulación con rastro, vueltos y unicidad de referencias por banco.
ALTER TABLE pagos ADD COLUMN banco TEXT;
ALTER TABLE pagos ADD COLUMN verificado INTEGER NOT NULL DEFAULT 1 CHECK (verificado IN (0,1));
ALTER TABLE pagos ADD COLUMN verificado_por INTEGER REFERENCES usuarios(id);
ALTER TABLE pagos ADD COLUMN verificado_en TEXT;
ALTER TABLE pagos ADD COLUMN anulado INTEGER NOT NULL DEFAULT 0 CHECK (anulado IN (0,1));
ALTER TABLE pagos ADD COLUMN anulado_motivo TEXT;
ALTER TABLE pagos ADD COLUMN usuario_id INTEGER REFERENCES usuarios(id);

-- Misma referencia digital (por método y banco) no puede cobrarse dos veces. Los puntos de venta reciclan números.
DROP INDEX ux_pago_referencia;
CREATE UNIQUE INDEX ux_pago_ref_digital ON pagos(metodo, COALESCE(banco,''), referencia)
  WHERE referencia IS NOT NULL AND anulado = 0 AND metodo IN ('pago_movil','zelle','usdt','binance_pay');

CREATE TRIGGER trg_pago_update_limitado BEFORE UPDATE ON pagos
WHEN NEW.venta_id IS NOT OLD.venta_id OR NEW.metodo IS NOT OLD.metodo OR NEW.moneda IS NOT OLD.moneda
  OR NEW.monto_cents IS NOT OLD.monto_cents OR NEW.equiv_usd_cents IS NOT OLD.equiv_usd_cents
  OR NEW.referencia IS NOT OLD.referencia OR NEW.banco IS NOT OLD.banco OR OLD.anulado = 1
  OR (SELECT estado FROM ventas WHERE id = OLD.venta_id) <> 'abierta'
BEGIN SELECT RAISE(ABORT, 'pago inmutable: solo se verifica o se anula mientras la venta está abierta'); END;

-- Vuelto entregado al cliente (sale de caja). Neto cobrado = pagos - vueltos.
CREATE TABLE vueltos (
  id INTEGER PRIMARY KEY,
  venta_id INTEGER NOT NULL REFERENCES ventas(id),
  metodo TEXT NOT NULL CHECK (metodo IN ('efectivo_usd','efectivo_bs','pago_movil')),
  moneda TEXT NOT NULL CHECK (moneda IN ('USD','BS')),
  monto_cents INTEGER NOT NULL CHECK (monto_cents > 0),
  equiv_usd_cents INTEGER NOT NULL CHECK (equiv_usd_cents > 0),
  referencia TEXT,
  usuario_id INTEGER REFERENCES usuarios(id),
  anulado INTEGER NOT NULL DEFAULT 0 CHECK (anulado IN (0,1)),
  anulado_motivo TEXT,
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TRIGGER trg_vuelto_no_delete BEFORE DELETE ON vueltos BEGIN SELECT RAISE(ABORT, 'vueltos no se borran'); END;
CREATE TRIGGER trg_vuelto_no_insert BEFORE INSERT ON vueltos
WHEN (SELECT estado FROM ventas WHERE id=NEW.venta_id) <> 'abierta'
BEGIN SELECT RAISE(ABORT, 'venta cerrada: no se pueden agregar vueltos'); END;
CREATE TRIGGER trg_vuelto_update_limitado BEFORE UPDATE ON vueltos
WHEN NEW.venta_id IS NOT OLD.venta_id OR NEW.metodo IS NOT OLD.metodo OR NEW.moneda IS NOT OLD.moneda
  OR NEW.monto_cents IS NOT OLD.monto_cents OR NEW.equiv_usd_cents IS NOT OLD.equiv_usd_cents OR OLD.anulado = 1
  OR (SELECT estado FROM ventas WHERE id = OLD.venta_id) <> 'abierta'
BEGIN SELECT RAISE(ABORT, 'vuelto inmutable: solo se anula mientras la venta está abierta'); END;
