# Pedidos 611

Herramienta de sugerido de compra para 611 Logística, según venta promedio y stock actual.

- `generar_datos.py` lee la réplica BI de Gescom (Postgres) y genera `data.json`: venta neta diaria por artículo (facturas − notas de crédito, en unidades), stock por depósito, unidad/factor de compra y última compra.
- `index.html` hace el cálculo en el navegador. Parámetros: ventana del promedio (7/14/30/60/90, N días, rango de fechas o mismo período del año anterior para estacionalidad), días a reponer, depósitos que cuentan como stock y demora por proveedor (Pepsico 5, resto 10 por defecto).
- Alertas: **en falta** (stock 0 con venta) y **críticos** (cobertura menor que la demora del proveedor).
- GitHub Action actualiza `data.json` cada hora.

- Redondeo por camada / pallet: cargar `bultos_por_camada` y `bultos_por_pallet` en `paletizado.csv` (separado por `;`, se edita desde GitHub). El sugerido va a la camada más cercana (7 con camada 5 → 5; 8 → 10) y completa el pallet si le falta poco (29 de 30 → 30; tolerancia configurable, 10% por defecto). Artículos sin dato quedan sin redondear.

Fórmula: `sugerido (bultos) = ceil((venta/día × (demora + días a reponer) − stock) / factor de bulto)`.
