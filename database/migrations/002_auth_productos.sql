-- 002: decisiones del dueño (BCV, IVA sumado, imprenta digital) + auth + auditoría + fotos.
UPDATE config SET valor='bcv' WHERE clave='tasa_default';
UPDATE config SET valor='sumado' WHERE clave='iva_modo_default';
INSERT INTO config(clave, valor) VALUES ('fiscal_via', 'imprenta_digital');

ALTER TABLE usuarios ADD COLUMN intentos_fallidos INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usuarios ADD COLUMN bloqueado_hasta TEXT;
CREATE UNIQUE INDEX ux_usuario_nombre ON usuarios(sucursal_id, nombre COLLATE NOCASE);

ALTER TABLE productos ADD COLUMN foto_path TEXT;

CREATE TABLE auditoria (
  id INTEGER PRIMARY KEY,
  usuario_id INTEGER REFERENCES usuarios(id),
  accion TEXT NOT NULL,
  detalle TEXT,
  creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TRIGGER trg_aud_no_update BEFORE UPDATE ON auditoria
BEGIN SELECT RAISE(ABORT, 'auditoria es inmutable'); END;
CREATE TRIGGER trg_aud_no_delete BEFORE DELETE ON auditoria
BEGIN SELECT RAISE(ABORT, 'auditoria es inmutable'); END;
