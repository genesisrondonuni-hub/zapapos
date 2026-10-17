-- 003: plantillas de etiqueta configurables (campos visibles, tipografía, colores).
CREATE TABLE plantillas_etiqueta (
  id INTEGER PRIMARY KEY,
  nombre TEXT NOT NULL UNIQUE COLLATE NOCASE,
  formato TEXT NOT NULL,
  config TEXT NOT NULL,           -- JSON validado por core/etiquetas.py
  actualizada_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
