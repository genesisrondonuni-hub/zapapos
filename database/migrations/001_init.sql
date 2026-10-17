-- 001_init: esquema base offline-first, doble moneda, auditoría inmutable.
-- Dinero en enteros: USD en centavos, Bs en céntimos. Tasa = Bs por USD x 1.000.000.

CREATE TABLE sucursales (
  id INTEGER PRIMARY KEY,
  codigo TEXT NOT NULL UNIQUE,
  nombre TEXT NOT NULL,
  activa INTEGER NOT NULL DEFAULT 1 CHECK (activa IN (0,1))
);

CREATE TABLE usuarios (
  id INTEGER PRIMARY KEY,
  sucursal_id INTEGER NOT NULL REFERENCES sucursales(id),
  nombre TEXT NOT NULL,
  rol TEXT NOT NULL CHECK (rol IN ('dueno','cajero')),
  pin_hash TEXT NOT NULL,
  activo INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0,1))
);

CREATE TABLE categorias (
  id INTEGER PRIMARY KEY,
  nombre TEXT NOT NULL UNIQUE,
  margen_bp INTEGER NOT NULL DEFAULT 0 CHECK (margen_bp >= 0) -- puntos base: 4000 = 40%
);

CREATE TABLE marcas (
  id INTEGER PRIMARY KEY,
  nombre TEXT NOT NULL UNIQUE
);

CREATE TABLE productos (
  id INTEGER PRIMARY KEY,
  categoria_id INTEGER NOT NULL REFERENCES categorias(id),
  marca_id INTEGER NOT NULL REFERENCES marcas(id),
  modelo TEXT NOT NULL,
  genero TEXT NOT NULL CHECK (genero IN ('dama','caballero','nino','nina','unisex')),
  margen_bp INTEGER CHECK (margen_bp IS NULL OR margen_bp >= 0), -- NULL = usa el de la categoría
  activo INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0,1))
);

-- SKU por variante (talla x color), nunca por producto.
CREATE TABLE variantes (
  id INTEGER PRIMARY KEY,
  producto_id INTEGER NOT NULL REFERENCES productos(id),
  sku TEXT NOT NULL UNIQUE,
  talla TEXT NOT NULL,
  color TEXT NOT NULL,
  costo_usd_cents INTEGER NOT NULL DEFAULT 0 CHECK (costo_usd_cents >= 0),
  precio_usd_cents INTEGER NOT NULL DEFAULT 0 CHECK (precio_usd_cents >= 0),
  stock_minimo INTEGER NOT NULL DEFAULT 0 CHECK (stock_minimo >= 0),
  activo INTEGER NOT NULL DEFAULT 1 CHECK (activo IN (0,1)),
  UNIQUE (producto_id, talla, color)
);

CREATE TABLE tasas_cambio (
  id INTEGER PRIMARY KEY,
  tipo TEXT NOT NULL CHECK (tipo IN ('bcv','paralelo','manual')),
  tasa_micro INTEGER NOT NULL CHECK (tasa_micro > 0),
  vigente_desde TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  creado_por INTEGER REFERENCES usuarios(id)
);

CREATE TABLE precios_historial (
  id INTEGER PRIMARY KEY,
  variante_id INTEGER NOT NULL REFERENCES variantes(id),
  costo_usd_cents INTEGER NOT NULL,
  precio_usd_cents INTEGER NOT NULL,
  tasa_id INTEGER REFERENCES tasas_cambio(id),
  motivo TEXT NOT NULL,
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Kardex append-only: cantidad con signo; nada se edita ni se borra.
CREATE TABLE inventario_movimientos (
  id INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL UNIQUE,
  sucursal_id INTEGER NOT NULL REFERENCES sucursales(id),
  variante_id INTEGER NOT NULL REFERENCES variantes(id),
  tipo TEXT NOT NULL CHECK (tipo IN
    ('compra','venta','devolucion','ajuste_pos','ajuste_neg','traslado_in','traslado_out','reversa')),
  cantidad INTEGER NOT NULL CHECK (cantidad <> 0),
  motivo TEXT NOT NULL CHECK (length(trim(motivo)) > 0),
  usuario_id INTEGER REFERENCES usuarios(id),
  referencia TEXT,
  reversa_de INTEGER REFERENCES inventario_movimientos(id),
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX ix_mov_variante ON inventario_movimientos(sucursal_id, variante_id);

CREATE TRIGGER trg_mov_no_update BEFORE UPDATE ON inventario_movimientos
BEGIN SELECT RAISE(ABORT, 'inventario_movimientos es inmutable: use contra-asiento'); END;
CREATE TRIGGER trg_mov_no_delete BEFORE DELETE ON inventario_movimientos
BEGIN SELECT RAISE(ABORT, 'inventario_movimientos es inmutable: use contra-asiento'); END;

CREATE VIEW stock_actual AS
SELECT sucursal_id, variante_id, SUM(cantidad) AS stock
FROM inventario_movimientos GROUP BY sucursal_id, variante_id;

CREATE TABLE ventas (
  id INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL UNIQUE,
  sucursal_id INTEGER NOT NULL REFERENCES sucursales(id),
  usuario_id INTEGER REFERENCES usuarios(id),
  estado TEXT NOT NULL DEFAULT 'abierta' CHECK (estado IN ('abierta','cerrada','anulada')),
  tasa_id INTEGER NOT NULL REFERENCES tasas_cambio(id),
  tasa_micro INTEGER NOT NULL CHECK (tasa_micro > 0),      -- snapshot de la tasa aplicada
  iva_modo TEXT NOT NULL DEFAULT 'sumado' CHECK (iva_modo IN ('incluido','sumado')),
  iva_bp INTEGER NOT NULL DEFAULT 1600,
  subtotal_usd_cents INTEGER NOT NULL DEFAULT 0,
  iva_usd_cents INTEGER NOT NULL DEFAULT 0,
  total_usd_cents INTEGER NOT NULL DEFAULT 0,
  total_bs_centimos INTEGER NOT NULL DEFAULT 0,
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE venta_items (
  id INTEGER PRIMARY KEY,
  venta_id INTEGER NOT NULL REFERENCES ventas(id),
  variante_id INTEGER NOT NULL REFERENCES variantes(id),
  cantidad INTEGER NOT NULL CHECK (cantidad > 0),
  precio_unit_usd_cents INTEGER NOT NULL CHECK (precio_unit_usd_cents >= 0)
);

-- Una venta = N pagos heterogéneos.
CREATE TABLE pagos (
  id INTEGER PRIMARY KEY,
  venta_id INTEGER NOT NULL REFERENCES ventas(id),
  metodo TEXT NOT NULL CHECK (metodo IN
    ('pago_movil','zelle','usdt','binance_pay','efectivo_bs','efectivo_usd','punto_debito','punto_credito')),
  moneda TEXT NOT NULL CHECK (moneda IN ('USD','BS','USDT')),
  monto_cents INTEGER NOT NULL CHECK (monto_cents > 0),     -- en la moneda del pago
  equiv_usd_cents INTEGER NOT NULL CHECK (equiv_usd_cents > 0),
  referencia TEXT,
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- Evita registrar dos veces la misma referencia de Pago Móvil/Zelle/etc.
CREATE UNIQUE INDEX ux_pago_referencia ON pagos(metodo, referencia) WHERE referencia IS NOT NULL;

CREATE TRIGGER trg_venta_no_delete BEFORE DELETE ON ventas
BEGIN SELECT RAISE(ABORT, 'ventas no se borran: anule con nota de crédito'); END;
CREATE TRIGGER trg_item_no_delete BEFORE DELETE ON venta_items
BEGIN SELECT RAISE(ABORT, 'venta_items no se borran'); END;
CREATE TRIGGER trg_pago_no_delete BEFORE DELETE ON pagos
BEGIN SELECT RAISE(ABORT, 'pagos no se borran'); END;

-- Fiscal: SIN datos precargados. El formato real SENIAT se define en el módulo fiscal.
CREATE TABLE fiscal_correlativos (
  sucursal_id INTEGER NOT NULL REFERENCES sucursales(id),
  serie TEXT NOT NULL,
  ultimo_numero INTEGER NOT NULL DEFAULT 0 CHECK (ultimo_numero >= 0),
  PRIMARY KEY (sucursal_id, serie)
);

CREATE TABLE outbox (
  id INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL UNIQUE,
  entidad TEXT NOT NULL,
  operacion TEXT NOT NULL CHECK (operacion IN ('insert','update','delete')),
  payload TEXT NOT NULL,
  intentos INTEGER NOT NULL DEFAULT 0,
  proximo_intento TEXT,
  enviado_en TEXT,
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE config (
  clave TEXT PRIMARY KEY,
  valor TEXT NOT NULL
);
-- Valores por defecto editables; tasa_default y alícuota IVA deben ser confirmadas por el dueño.
INSERT INTO config(clave, valor) VALUES
  ('tasa_default', 'POR_DEFINIR'),
  ('iva_bp', '1600'),
  ('iva_modo_default', 'POR_DEFINIR'),
  ('redondeo_default', 'sin');
