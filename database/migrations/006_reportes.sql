-- 006: reportes. Costo congelado por línea (margen real), reembolsos de devoluciones y cierres de caja inmutables.
ALTER TABLE venta_items ADD COLUMN costo_unit_usd_cents INTEGER;      -- NULL = dato anterior a esta migración

INSERT INTO config(clave, valor) VALUES ('utc_offset_min', '-240');   -- Venezuela: UTC-4, sin horario de verano

CREATE TABLE devolucion_reembolsos (
  id INTEGER PRIMARY KEY,
  devolucion_id INTEGER NOT NULL REFERENCES devoluciones(id),
  metodo TEXT NOT NULL CHECK (metodo IN ('efectivo_usd','efectivo_bs','pago_movil','zelle','usdt')),
  moneda TEXT NOT NULL CHECK (moneda IN ('USD','BS','USDT')),
  monto_cents INTEGER NOT NULL CHECK (monto_cents > 0),
  equiv_usd_cents INTEGER NOT NULL CHECK (equiv_usd_cents > 0),
  referencia TEXT,
  usuario_id INTEGER REFERENCES usuarios(id),
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TRIGGER trg_remb_no_update BEFORE UPDATE ON devolucion_reembolsos BEGIN SELECT RAISE(ABORT, 'reembolsos son inmutables'); END;
CREATE TRIGGER trg_remb_no_delete BEFORE DELETE ON devolucion_reembolsos BEGIN SELECT RAISE(ABORT, 'reembolsos son inmutables'); END;

CREATE TABLE cierres_caja (
  id INTEGER PRIMARY KEY,
  sucursal_id INTEGER NOT NULL REFERENCES sucursales(id),
  usuario_id INTEGER REFERENCES usuarios(id),
  desde TEXT NOT NULL,
  hasta TEXT NOT NULL,
  fondo_usd_cents INTEGER NOT NULL, fondo_bs_centimos INTEGER NOT NULL,
  esperado_usd_cents INTEGER NOT NULL, esperado_bs_centimos INTEGER NOT NULL,
  contado_usd_cents INTEGER NOT NULL CHECK (contado_usd_cents >= 0), contado_bs_centimos INTEGER NOT NULL CHECK (contado_bs_centimos >= 0),
  diferencia_usd_cents INTEGER NOT NULL, diferencia_bs_centimos INTEGER NOT NULL,
  retirado_usd_cents INTEGER NOT NULL DEFAULT 0 CHECK (retirado_usd_cents >= 0),
  retirado_bs_centimos INTEGER NOT NULL DEFAULT 0 CHECK (retirado_bs_centimos >= 0),
  dejado_usd_cents INTEGER NOT NULL, dejado_bs_centimos INTEGER NOT NULL,
  notas TEXT,
  detalle TEXT NOT NULL,                       -- JSON: cobros por método del período
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TRIGGER trg_cierre_no_update BEFORE UPDATE ON cierres_caja BEGIN SELECT RAISE(ABORT, 'los cierres de caja son inmutables'); END;
CREATE TRIGGER trg_cierre_no_delete BEFORE DELETE ON cierres_caja BEGIN SELECT RAISE(ABORT, 'los cierres de caja son inmutables'); END;
