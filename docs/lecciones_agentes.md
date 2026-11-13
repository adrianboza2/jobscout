# Lecciones para agentes (jobscout)

Hechos del repo que un brief o un trabajador ha tenido que adivinar. Una línea cada uno; tope 30.

- Los tres comandos de detalle no tienen las mismas opciones: `--refresh` solo existe en `fetch-details` (LinkedIn); `--unseen-days`, en los tres. `--max-items` tiene tope duro 250 (`HARD_MAX_ITEMS`): para contar el total real, usa `truncated` de `--dry-run --json`.
- `generate_dashboard` no recibe el perfil por sí mismo: lo que necesite el perfil se lo pasa `cmd_dashboard`, y en el payload solo van datos derivados, nunca el perfil.
- En el panel, lo que depende de una constante de Python (ventanas, umbrales) se pinta desde el payload, nunca escrito a mano en el HTML ni con un valor de respaldo en el JS.
- No hay parser de `jobs.salary` a cifra: el sueldo indicado es texto libre; las cifras salen de `salary_estimate_*` de la evaluación.
