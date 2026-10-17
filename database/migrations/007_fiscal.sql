-- 007: base fiscal. Los números de factura/control los asigna SIEMPRE el proveedor (imprenta digital); aquí nunca se inventan.
ALTER TABLE ventas ADD COLUMN cliente_rif TEXT;
ALTER TABLE ventas ADD COLUMN cliente_nombre TEXT;
ALTER TABLE ventas ADD COLUMN cliente_domicilio TEXT;
INSERT INTO config(clave, valor) VALUES ('fiscal_activo','0'), ('fiscal_adaptador','ninguno'), ('fiscal_modo_prueba','0');

CREATE TABLE documentos_fiscales (
  id INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL UNIQUE,
  tipo TEXT NOT NULL CHECK (tipo IN ('factura','nota_credito')),
  venta_id INTEGER NOT NULL REFERENCES ventas(id),
  devolucion_id INTEGER REFERENCES devoluciones(id),
  estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','emitido','error')),
  simulado INTEGER NOT NULL DEFAULT 0 CHECK (simulado IN (0,1)),
  numero TEXT, numero_control TEXT, fecha_emision TEXT,
  receptor_rif TEXT, receptor_nombre TEXT, receptor_domicilio TEXT,
  base_bs_centimos INTEGER NOT NULL, iva_bs_centimos INTEGER NOT NULL, total_bs_centimos INTEGER NOT NULL,
  base_usd_cents INTEGER NOT NULL, iva_usd_cents INTEGER NOT NULL, total_usd_cents INTEGER NOT NULL,
  iva_bp INTEGER NOT NULL, tasa_micro INTEGER NOT NULL,
  respuesta TEXT, error TEXT, intentos INTEGER NOT NULL DEFAULT 0,
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  emitido_en TEXT,
  CHECK (estado <> 'emitido' OR (numero IS NOT NULL AND fecha_emision IS NOT NULL)),
  CHECK ((tipo = 'nota_credito') = (devolucion_id IS NOT NULL))
);
CREATE UNIQUE INDEX ux_fiscal_factura ON documentos_fiscales(venta_id) WHERE tipo = 'factura';
CREATE UNIQUE INDEX ux_fiscal_nc ON documentos_fiscales(devolucion_id) WHERE tipo = 'nota_credito';
CREATE UNIQUE INDEX ux_fiscal_numero ON documentos_fiscales(tipo, numero) WHERE numero IS NOT NULL AND simulado = 0;
CREATE TRIGGER trg_fiscal_no_delete BEFORE DELETE ON documentos_fiscales BEGIN SELECT RAISE(ABORT, 'los documentos fiscales no se borran'); END;
CREATE TRIGGER trg_fiscal_emitido_inmutable BEFORE UPDATE ON documentos_fiscales WHEN OLD.estado = 'emitido'
BEGIN SELECT RAISE(ABORT, 'documento fiscal emitido: inmutable'); END;
