# Benchmark de modelos

Herramienta local para comparar el puntuador LLM de jobscout entre modelos con
las mismas ofertas. Se usa al cambiar el prompt o el perfil (por ejemplo, al
implementar el estimador salarial) para ver si la calidad de las decisiones se
mantiene.

No es un test: usa red y modelos reales, tarda y cuesta llamadas. Nunca se
ejecuta desde la suite (`pytest`); se lanza a mano.

## Qué mide

- **ok**: respuestas válidas del modelo (sin rescate heurístico).
- **secs**: mediana de segundos por oferta.
- **mean_A / mean_neg**: nota media de tus «apply» frente a los grupos B+C.
- **gap / auc**: cuánto separa lo bueno de lo malo (auc 1 = siempre pone tus
  apply por encima; 0,5 = azar).
- **A_not_skip**: tus «apply» que el modelo no descarta.
- **profile_drop**: cuánto baja la nota de tus «apply» con un perfil frontend
  senior (más = entiende el perfil).
- **drift**: diferencia media al repetir la misma oferta (menos = más estable).

## Cómo ejecutarlo

Desde la raíz del repositorio:

Por defecto `bench.py` usa `selection-2026-10-20.json`: las mismas 18 ofertas
del benchmark de referencia, para que comparar con la tabla de abajo sea justo.
Solo genera una selección nueva si quieres otro conjunto (y entonces repite
también los modelos de referencia). Ojo: el grupo C de 2026-10-20 salió de
ofertas todavía sin puntuar; `select_offers.py` hoy exige una nota previa, así
que no lo reproduce exactamente.

```bash
# 1. (Opcional) nueva selección (solo lectura de la BD real).
.venv/bin/python scripts/benchmark/select_offers.py
# → scripts/benchmark/selection.json; pásala con --selection

# 2. Lanza un bench.py por modelo, en paralelo.
mkdir -p reports/bench-2026-10-20
.venv/bin/python scripts/benchmark/bench.py opencode-go/longcat-2.5-preview-free reports/bench-2026-10-20 &
.venv/bin/python scripts/benchmark/bench.py opencode/deepseek-v4.1-flash-free reports/bench-2026-10-20 &
.venv/bin/python scripts/benchmark/bench.py opencode-go/beam-free reports/bench-2026-10-20 &
wait

# 3. Agrega los resultados en una tabla y un HTML.
.venv/bin/python scripts/benchmark/report.py reports/bench-2026-10-20 \
  reports/bench-2026-10-20.html
```

`select_offers.py` admite `--db` y `--out`; `bench.py` admite `--variant`
(vacío por defecto), `--selection`, `--contrast-profile`, `--quota-wait` y
`--quota-retries`; `report.py` admite `--db`.

Los modelos gratuitos de OpenCode comparten una sola cuota (unas 55 llamadas
seguidas, 2026-10-23). Lanzarlos en paralelo hace que el resultado dependa de
cuándo corrió cada uno: con modelos gratuitos, lanza un modelo detrás de otro con
`--quota-wait 600`, que espera y reintenta la misma llamada al ver
"Free usage limit reached", y nunca en la cuota que necesita un sweep.

`bench.py` copia la BD real a `out_dir/<modelo>/bench.sqlite3` y escribe solo
ahí y en `out_dir/<modelo>.jsonl`: la BD original nunca se modifica.
`select_offers.py` y `report.py` abren la BD real en modo solo lectura
(`file:...?mode=ro`).

El informe HTML debe guardarse en `reports/`, que es local y está ignorado por
Git.

## Resultados del 2026-10-20

18 ofertas, 30 llamadas por modelo, sin variante:

| modelo | ok | s/oferta | auc | gap | mean_A | apply no descartados | profile_drop | drift |
|---|---|---|---|---|---|---|---|---|
| opencode-go/longcat-2.5-preview-free | 27/30 | 19.9 | 1.00 | 45.2 | 66.8 | 6/6 | 61.3 | 2.0 |
| opencode/deepseek-v4.1-flash-free | 30/30 | 9.8 | 1.00 | 39.4 | 61.2 | 8/8 | 54.2 | 4.5 |
| opencode-go/beam-free | 30/30 | 9.0 | 0.99 | 36.5 | 56.2 | 8/8 | 52.8 | 7.0 |
| opencode/big-pickle | 27/30 | 16.5 | 0.96 | 36.3 | 59.4 | 6/7 | 54.7 | 5.0 |
| opencode-go/minimax-m3.1-flash-free | 30/30 | 10.4 | 0.91 | 32.1 | 62.6 | 7/8 | 55.5 | 11.0 |
| opencode/glm-5.3-flash-free | 30/30 | 21.0 | 0.94 | 36.2 | 58.5 | 7/8 | 55.1 | 8.0 |
| opencode-go/deepseek-v4.1-flash | 30/30 | 9.9 | 0.90 | 29.2 | 55.8 | 6/8 | 49.0 | 7.5 |
