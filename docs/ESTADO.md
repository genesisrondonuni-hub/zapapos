# Estado del proyecto (octubre 2026)

## Completado (con pruebas automáticas)
bootstrap · database · auth · productos · pricing_engine · inventario · labels · pos · pagos · reportes · pos_ui (servidor, ticket, pantalla) ·
datos_negocio · respaldo/restauración · actualizaciones firmadas · licencias · fiscal_base · nube_sync (Supabase) · app_dueno (v1) · asistente IA (función Edge)

## Pendiente
- **Probar en hardware real**: etiquetas en Brother QL-800 y rollo 40×30, ticket térmico 58 mm, lector de código, cajón monedero.
- **Probar en Windows**: `instalador/instalar.bat`, lanzador y una actualización.
- **Fiscal**: adaptador de la imprenta digital real, pantalla fiscal; confirmar con contador emisión diferida, IGTF y formato del libro de ventas.
- **Conciliación bancaria**: adaptadores por banco (requiere claves API del propio cliente).
- **App del dueño**: más vendidos, margen y stock crítico como consultas en la nube.
- **WhatsApp** (API oficial): al final, por decisión del propietario.
- Desplegar la función Edge `asistente` y definir los secretos de IA (`IA_API_KEY`, `IA_MODEL`, `IA_BASE_URL`, `IA_PROVIDER`).

## Decisiones de diseño clave
- Offline-first: SQLite en modo WAL; una venta cerrada es inmutable; nada se borra (contra-asientos).
- La licencia vencida **nunca** bloquea ventas, cobros, facturación, respaldos ni restauración.
- Nube: eventos idempotentes por uuid; RLS sin políticas; solo funciones `security definer` validan el token de cada instalación.
- Dinero en enteros (centavos / céntimos); tasa = Bs por USD × 1.000.000.
