# jobscout

Herramienta **local-first** para buscar empleo: recolecta resultados públicos de
búsqueda de LinkedIn a muy bajo volumen, los normaliza y deduplica en SQLite, y
puntúa cada oferta contra un perfil estructurado local. El puntuador
predeterminado reutiliza tu CLI de OpenCode ya autenticada con el modelo gratuito
MiMo; también hay un puntuador determinista offline y otro compatible con OpenAI.

Nada sale de tu equipo salvo las solicitudes que autorizas explícitamente.
**jobscout no envía postulaciones automáticas.**

**Idiomas:** Español (este archivo) · [English version](README.en.md)

## Qué hace y qué no hace

| Hace | No hace |
|------|---------|
| Recolecta tarjetas públicas de búsqueda (opt-in y acotado) | No inicia sesión, no usa cookies, no resuelve CAPTCHA, no evade antibots |
| Enriquece ofertas con la descripción completa (opt-in y acotado) | No postula, no contacta a nadie, no automatiza candidaturas |
| Deduplica y guarda todo en SQLite local | No envía datos a ningún servidor propio |
| Puntúa con heurística, LLM compatible con OpenAI u OpenCode CLI | No programa ejecuciones ni envía notificaciones remotas (solo avisos locales opt-in, manuales) |
| Genera un tablero HTML local en español | No escribe el feedback humano en la base por su cuenta |

## Inicio rápido (solo fixtures, sin red)

Requisitos: **Python 3.11+** y `PyYAML` (solo para perfiles YAML; los perfiles
JSON no necesitan dependencias). El puntuador heurístico y la suite de pruebas no
necesitan red ni claves.

Instalación editable:

```bash
# POSIX (bash/zsh)
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Sin instalar el paquete también funciona: antepón `PYTHONPATH=src` (POSIX) o
define `$env:PYTHONPATH = "src"` una vez en PowerShell.

Copia las plantillas (`.env` y `profile.yaml` quedan ignorados por Git):

```bash
cp .env.example .env
cp profile.example.yaml profile.yaml
```

```powershell
Copy-Item .env.example .env
Copy-Item profile.example.yaml profile.yaml
```

Recorrido completo con datos sintéticos, de punta a punta:

```bash
python -m jobscout init-db
python -m jobscout ingest-fixture tests/fixtures/linkedin_search_cards_sample.json
python -m jobscout fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout score --all --rescore --scorer heuristic --profile profile.example.yaml
python -m jobscout rank --top 10
python -m jobscout dashboard --top 10
```

Todos los comandos se ejecutan desde la raíz del repositorio y solo escriben
dentro de `data/` y `reports/` (ambos ignorados por Git).

El fixture `tests/fixtures/linkedin_search_sample.json` ya contiene
descripciones completas, así que `fetch-details` no encuentra nada que
enriquecer si lo usas en lugar de `linkedin_search_cards_sample.json`.

**Verificación esperada:** `ingest-fixture` informa ofertas nuevas y duplicadas;
`score` termina con `N job(s), M succeeded`; `rank` lista ofertas con puntaje; el
tablero imprime la ruta del HTML escrito.

## Aviso de seguridad para uso real

La recolección real está **deshabilitada por defecto** y exige
`--confirm-network`. Antes de usarla:

- **Es tu responsabilidad** cumplir los términos de servicio del sitio y la ley
  local aplicable.
- El volumen es deliberadamente bajo: 2 páginas, 40 elementos, ~8 s entre
  solicitudes (±2 s de jitter), 20 s de timeout.
- **No ejecutes recolección real en CI** ni en entornos automatizados.
- Usa `--dry-run` primero: imprime las URLs exactas y no realiza ninguna
  solicitud ni escritura.
- Para probar sin red, usa `--fixture`: nunca toca la red.

## Flujo operativo recomendado (uso real)

Este es el ciclo manual, de punta a punta, para pasar de un perfil a ofertas
revisadas. Ninguna etapa se ejecuta sola y **jobscout nunca postula**: cada
comando que toca la red o el modelo lo lanzas tú.

Prepara el perfil real antes de cualquier barrido:

- Tu perfil local es **`profile.yaml`**, seleccionado con `JOBSCOUT_PROFILE_PATH`
  en `.env` (el valor por defecto es `profile.example.yaml`).
- `profile.example.yaml` es **solo una plantilla**: cópialo a `profile.yaml` y
  edítalo. No conviertas la plantilla en tu perfil real ni la subas a Git.

| # | Etapa | Comando seguro | Qué hace | ¿Automático? |
|---|-------|----------------|----------|--------------|
| 0 | Preparar perfil | `cp profile.example.yaml profile.yaml` (POSIX) o `Copy-Item profile.example.yaml profile.yaml` (PowerShell), y `JOBSCOUT_PROFILE_PATH=profile.yaml` en `.env` | Defines rol objetivo, seniority, skills, dealbreakers y exclusiones. | Decisión humana |
| 1 | Barrido controlado | `python -m jobscout keyword-plan` y luego `python -m jobscout search-linkedin --keywords "..." --dry-run`; para ejecutarlo, cambia `--dry-run` por `--confirm-network` | Descubre ofertas nuevas. `keyword-plan` es offline; cada búsqueda real es un acto humano explícito. | Semiautomático: autorizas cada consulta |
| 2 | Enriquecer | `python -m jobscout fetch-details --max-items 10 --confirm-network` (o `fetch-tecnoempleo-details` para ofertas de Tecnoempleo, o `--fixture` para trabajar offline) | Trae la descripción completa de cada oferta, acotado en volumen. Solo sube a `ok` cuando reconoce el detalle. | Semiautomático: autorizas cada corrida |
| 3 | Puntuar con MiMo | `python -m jobscout score --all --scorer opencode` | El puntuador predeterminado (OpenCode + MiMo) evalúa solo las ofertas que aún no tienen evaluación. | El puntaje es automático; la decisión de postular no |
| 4 | Generar dashboard | `python -m jobscout dashboard --top 25` | Escribe un HTML local con ofertas, puntajes, evidencia del enlace y filtros de preferencia. | Automático y de solo lectura |
| 5 | Revisar ofertas | Abre `reports/jobscout-dashboard.html` | Tú marcas **Aplicar** o **Descartar** en el navegador. | Decisión humana exclusivamente |
| 6 | Exportar/importar feedback | Exporta el JSON desde el tablero y ejecuta `python -m jobscout import-feedback jobscout-feedback.json` | Tus decisiones pasan de `localStorage` a la tabla `job_feedback` de SQLite. | Importar es automático una vez que lo invocas |
| 7 | Calibrar | `python -m jobscout calibrate --scorer opencode` | Compara la recomendación del modelo con tu decisión humana importada. Solo lectura; no ajusta nada. | Automático y de solo lectura |
| 8 | Analizar keywords | `python -m jobscout dashboard` → sección *rendimiento de palabras clave* | Tras varios ciclos, decides qué familias de búsqueda rinden y cuáles descartar. | Decisión humana |
| 9 | Notificar ofertas de alto puntaje (opt-in) | `python -m jobscout notify` (vista previa) o `python -m jobscout notify --deliver` | Lista ofertas activas con puntaje ≥ umbral, sin feedback humano importado y, si lo configuras, con el resultado de preferencia elegido. Solo lee SQLite; nunca escribe, no usa red ni modelo. | Manual y desactivado por defecto |

Notas del ciclo:

- **Nunca se postula automáticamente.** Tampoco hay programador de tareas; las
  notificaciones locales son opt-in y manuales (`jobscout notify`), y cada
  corrida la invocas tú.
- `--rescore` **no es un paso diario**: re-evalúa ofertas ya puntuadas y solo hace
  falta cuando cambias el perfil o el prompt. Para las ofertas nuevas, el ciclo
  normal es `score --all --scorer opencode`, que evalúa justo las que todavía no
  tienen puntaje.
- `fetch-details` no vuelve a puntuar por sí solo; el paso 3 cierra el
  enriquecimiento.
- Cada evaluación queda versionada: guarda el `prompt_version` del puntuador y el
  `input_fingerprint` del perfil, así que un cambio de política es distinguible
  sin reescribir el historial.

### Criterio de seniority y tecnologías (prompt v5)

El prompt de OpenCode está versionado como **`opencode-v5-seniority-aware`**. La
política que aplica MiMo es:

- **Penaliza fuerte, y usa `skip` cuando define el rol:** 5 o más años de
  experiencia exigidos, títulos explícitos `senior`, `staff`, `principal`,
  `lead` o `manager`, y liderazgo u ownership arquitectónico como
  responsabilidad central. Una mención de pasada de "senior" en una oferta por lo
  demás junior o *stretch* no es un `skip` duro.
- **No bloquea por una o dos tecnologías faltantes:** si el rol principal sigue
  siendo Cloud / DevOps / DevSecOps / Platform, esas ausencias se tratan como
  brechas aprendibles.
- **Sí bloquea el stack principal equivocado:** si la mayoría del stack central
  pedido no coincide con tu perfil, es un *mismatch* material y la recomendación y
  el puntaje bajan mucho.
- **Distingue requisitos duros de preferencias:** un rol junior o *stretch*
  razonable tiende a `consider` o `apply` aunque pida más experiencia que la
  disponible, salvo los umbrales fuertes anteriores.
- **Mantiene los dealbreakers** y las exclusiones de SOC/turnos rotativos, que
  además se calculan aparte en los **filtros de preferencia** (offline y sin tocar
  el puntaje ni la evaluación guardada).

El formato de salida no cambia: un único objeto JSON con `overall_score`,
`recommendation`, `confidence` y `rationale`.

## Tablero local (dashboard)

`dashboard` lee la base SQLite existente y escribe **un solo archivo HTML**
autocontenido (CSS, JavaScript y datos embebidos). No usa CDN, no pide assets
externos, no necesita servidor y no hace ninguna solicitud de red. Toda la
interfaz visible está en español neutro/profesional; las claves internas del
esquema y los comandos de CLI permanecen en inglés para no divergir de la base
de datos.

```bash
# POSIX
python -m jobscout dashboard --output reports/jobscout-dashboard.html --top 25
xdg-open reports/jobscout-dashboard.html   # Linux
open reports/jobscout-dashboard.html       # macOS
```

```powershell
# Windows PowerShell
python -m jobscout dashboard --output reports\jobscout-dashboard.html --top 25
Start-Process reports\jobscout-dashboard.html
```

- Ruta de salida predeterminada: `reports/jobscout-dashboard.html` (ignorada por
  Git). `--top` predeterminado: `25`. Las barras inclinadas también funcionan en
  Windows: `--output reports/dashboard.html`.
- El informe nunca embebe la descripción de la oferta, payloads crudos, el
  perfil, datos de contacto, claves de API ni rutas locales absolutas.
- Abre la base con `PRAGMA query_only`: generar el informe no puede insertar,
  actualizar ni borrar filas.
- Una base inexistente es un error; una base vacía o sin puntajes igual genera
  el informe, con una advertencia visible que explica por qué está vacío.

### Recomendación del modelo vs. feedback humano

Son dos señales distintas y el informe las mantiene separadas:

| | Recomendación del modelo | Feedback humano |
|---|---|---|
| Lo produce | `score` (heurística, LLM u OpenCode) | tú, haciendo clic en el informe |
| Se guarda en | tabla `evaluations` de SQLite, con procedencia completa | `localStorage` del navegador, indexado por proyecto + id de oferta; luego, la tabla `job_feedback` de SQLite |
| ¿`jobscout` lo lee? | sí | solo con `import-feedback`, que copia una exportación a `job_feedback` |
| ¿Es portable? | sí, está en la base | con **Exportar feedback local (JSON)** más `import-feedback` |

Cada oferta tiene **dos** botones de feedback local (**Aplicar / Descartar**,
almacenados como `apply` / `reject`) y un motivo opcional. La decisión activa es
binaria: *Aplicar* o *Descartar*. Los botones son **solo del navegador**: no
postulan, no tocan LinkedIn y no escriben en SQLite. **Exportar feedback local
(JSON)** descarga `jobscout-feedback.json` con el esquema `jobscout.feedback.v1`.

Compatibilidad con el histórico: las decisiones `consider` (histórico *Revisar*
del tablero anterior) no se borran ni se convierten solas. Una oferta con
`consider` guardado lo muestra como **decisión histórica (legacy)** y permite
reemplazarlo con Aplicar o Descartar; la exportación mantiene el esquema `v1` y
el valor legacy tal cual, y el importador lo sigue aceptando. La recomendación
del modelo conserva su propio vocabulario (`apply` / `consider` / `skip` /
`need_more_info`), separado de tu decisión.

Importa esa exportación para que las decisiones queden guardadas:

```bash
python -m jobscout import-feedback jobscout-feedback.json
```

- Escribe una fila por oferta en `job_feedback`, junto a la recomendación del
  modelo en `evaluations`, nunca en su lugar.
- Valida primero el envoltorio: un esquema ajeno, la falta de `project_id` o un
  `project_id` que no coincide con esta base hacen fallar el comando sin escribir
  nada. `--project-id ID` acepta el id de la exportación de forma explícita, para
  una base que se movió de lugar.
- Rechaza entradas individuales (id de oferta desconocido, decisión desconocida,
  marca de tiempo mal formada) con un mensaje en stderr, mientras las entradas
  válidas sí se importan, igual que `score --job-id` con una oferta inexistente.
- Es idempotente: reimportar el mismo archivo no cambia nada y una entrada más
  antigua que la decisión guardada nunca la sobrescribe. Una entrada sin decisión
  (el estado "sin revisar" del tablero) se omite y nunca borra una decisión
  guardada.

Todavía no hay programación automática: importar es un comando que ejecutas tú.

### Ofertas sin decisión importada

El rail de filtros incluye **Solo sin decisión importada**, que reduce las ofertas
mostradas a las que **no tienen ninguna fila en `job_feedback`**. Ese es el
backlog real de revisión que existe en SQLite; se lee con un único `SELECT` de
solo lectura sobre la conexión `mode=ro`. No inventa ninguna cifra: no muestra un
contador de "pendientes" que la base no pueda respaldar.

- Es un eje **distinto** de *Estado de revisión* (`revisadas` / `sin revisar`):
  ese filtro lee tus marcas guardadas en `localStorage`, mientras que este lee la
  decisión humana importada en SQLite. El KPI **Revisadas (tu decisión)** sigue
  siendo la señal del navegador y nunca se mezcla con el feedback importado.
- Si todavía no importaste ninguna decisión, el propio panel lo dice y remite a
  `jobscout import-feedback`; no presenta todas las ofertas activas como un
  backlog medido.
- La razón de texto libre de una decisión importada **nunca** entra al HTML.
- El botón es neutro (sin color) y su estado se expone con `aria-pressed`, así que
  no depende del color; no agrega ninguna animación ni consulta `@media` nueva.

**Limitación explícita:** `search_runs.cycle_id` es opcional y de formato libre
(una convención humana), y las corridas de distintas plataformas o ciclos no son
comparables de forma garantizada. Por eso hoy **no** existe una vista de "nuevas
en el último ciclo": no se puede calcular de forma confiable y no se aproxima. La
única condición de aislamiento es la ausencia de una fila en `job_feedback`.

Una vez importado el feedback, `calibrate` produce la evidencia de qué tan bien
coincide el modelo contigo:

```bash
python -m jobscout calibrate                    # última evaluación válida de cada oferta vs. feedback
python -m jobscout calibrate --scorer opencode  # solo evaluaciones de ese puntuador
python -m jobscout calibrate --json             # informe legible por máquina
```

`calibrate` es de **solo lectura y sin red**: abre la base en modo lectura
(`mode=ro`, `PRAGMA query_only`), solo ejecuta `SELECT` y no crea la base. Nunca
ajusta prompts, pesos ni puntuaciones: solo produce evidencia. Compara la
recomendación del modelo con la decisión humana con un mapeo explícito:

| Modelo | Humano |
|--------|--------|
| `apply` | `apply` |
| `consider` | `consider` (legacy) |
| `skip` | `reject` |

La fila `consider` es **legacy**: el tablero binario ya no la ofrece, pero una
decisión histórica se sigue contando y el informe la marca como legacy
(`legacy_total` / `"legacy": true` en cada muestra) en lugar de descartarla.

`need_more_info` **no** forma parte del acuerdo three-way: se informa aparte como
categoría no resuelta y nunca se convierte en `skip`. El informe muestra las
muestras comparables, el acuerdo exacto, la matriz de confusión, los conteos no
resueltos y la distribución de `overall_score` por decisión humana. Una decisión
sin evaluación válida (`status = 'ok'`) se cuenta aparte y queda fuera del cálculo.

### Evidencia del enlace (detalle)

Cada oferta muestra, junto al título, una línea de **evidencia del enlace**
separada de los dos ejes de decisión. Es un hecho guardado, no una
recomendación: se calcula solo con el último intento de `fetch-details` de esa
oferta y nunca altera puntajes, recomendaciones ni la base.

| Estado | Significado |
|--------|-------------|
| `unchecked` | No hay ningún intento de detalle registrado. |
| `skipped` | El último intento se omitió a propósito. |
| `ok` | El último intento tuvo éxito (ok/partial) dentro de la ventana de 21 días. |
| `stale` | El último intento tuvo éxito pero es anterior a la ventana de 21 días. |
| `definitive_missing` | El último intento respondió 404/410. |
| `retryable_failure` | El último intento falló sin recibir una respuesta HTTP. |
| `failed` | El último intento falló con otra respuesta HTTP registrada. |

- La ventana de 21 días es explícita; una evidencia sin fecha utilizable se
  informa como `stale`, nunca como vigente.
- Un `final_url` de redirección se muestra solo como evidencia segura de un
  intento exitoso, con la misma validación de URL que el resto de enlaces, y
  solo si difiere de la URL solicitada. Nunca se presenta como enlace roto.
- Si una oferta visible tiene un 404/410 y hubo un re-avistamiento posterior, el
  informe lo dice: la evidencia fue refutada. La rotura no se inventa ni se
  deduce de una redirección.

### Filtros de preferencia (exclusiones)

Además del puntaje de encaje profesional, cada oferta se contrasta con las
**exclusiones explícitas** del usuario. Es un eje separado: no edita el puntaje,
la recomendación ni la evaluación guardada, y se recalcula al leer la oferta, no
se persiste como una segunda sentencia versionada. La clasificación es
determinista y **sin conexión**; no hay ninguna llamada a un modelo.

| Resultado | Significado |
|-----------|-------------|
| `pass` | No se encontró ninguna coincidencia con una exclusión. |
| `flag` | Coincidencia dudosa (por ejemplo "turnos" sin decir que rotan, o un rol SOC sin evidencia operativa); se muestra para revisión humana. |
| `fail` | Coincidencia clara con una exclusión: turnos rotativos o SOC operativo. |

Exclusiones implementadas:

- **Turnos rotativos** en español e inglés (`turnos rotativos`, `rotación de
  turnos`, `rotating shifts`, `shift rotation`, `24/7 shifts`). Un `24/7` sin
  turnos solo se marca `flag`, y un horario flexible o unas guardias puntuales
  nunca producen `fail`.
- **SOC operativo** solo cuando el término implica monitorización, operaciones,
  guardias/turnos o vigilancia continua (por ejemplo `SOC Analyst` con `24/7`,
  `SIEM monitoring`, `incident monitoring`). Un SOC genérico, un requisito de
  conocimiento o `SOC 2` (cumplimiento) quedan elegibles; un rol SOC sin
  evidencia operativa se marca `flag`.

El informe muestra una banda neutra (sin color) junto al título con el resultado,
la razón y la evidencia, y el resumen agrega cuántas ofertas quedan excluidas y
cuántas por revisar. El CLI expone el mismo veredicto en `list`, `rank` y `score`
(marcador `[preference=...]` en texto y un objeto `preference` en `--json`).

## Referencia de CLI

Opciones globales: `--db PATH`, `--profile PATH`, `--json` (antes o después del
subcomando) y `--version`.

| Comando | Propósito |
|---------|-----------|
| `init-db` | Crea o verifica el esquema. Idempotente. |
| `ingest-fixture PATH [--source S] [--label L]` | Normaliza, deduplica y guarda un fixture JSON (sin red). |
| `search-linkedin --keywords K [--location L] [--distance N] [--remote] [--employment-type T] [--max-pages N] [--max-items N] [--cycle-id ID] [--dry-run \| --confirm-network]` | Búsqueda pública acotada. Deshabilitada sin `--confirm-network`. |
| `search-getmanfred [--keywords K] [--max-documents N] [--max-items N] [--cycle-id ID] [--label L] [--dry-run \| --confirm-network]` | Inventario del sitemap público de GetManfred. `--keywords` aplica un filtro **local** (título/slug), sin fan-out ni endpoint privado. Sin ranking. Deshabilitado sin `--confirm-network`. |
| `search-tecnoempleo --keywords K [--location L] [--max-pages N] [--max-items N] [--cycle-id ID] [--label L] [--dry-run \| --confirm-network]` | Búsqueda pública server-rendered de Tecnoempleo. Respeta `robots.txt` en tiempo de ejecución. Deshabilitada sin `--confirm-network`. |
| `keyword-plan [--family ID…] [--location L] [--remote]` | Lista las familias de palabras clave y el comando exacto por consulta (sin red, sin base de datos). |
| `fetch-details [--job-id ID… \| --all \| --refresh [--stale-days N]] [--max-items N] [--dry-run \| --fixture PATH \| --confirm-network] [--label L]` | Enriquece ofertas de LinkedIn con la descripción completa. Por defecto, las que aún no están enriquecidas; `--refresh` vuelve sobre las ya consultadas con evidencia obsoleta o reintentable. |
| `fetch-tecnoempleo-details [--job-id ID… \| --all] [--max-items N] [--dry-run \| --confirm-network] [--label L]` | Enriquece ofertas de Tecnoempleo desde su página de detalle pública. Vuelve a leer `robots.txt`; sin login ni cookies. Deshabilitado sin `--confirm-network`. |
| `list [--limit N] [--min-score S] [--include-inactive]` | Ofertas recientes con su último puntaje. |
| `rank [--top N] [--min-score S]` | Ofertas con puntaje exitoso, mejor primero. |
| `score (--job-id ID… \| --all) [--scorer heuristic\|llm\|opencode] [--limit N] [--rescore]` | Evalúa ofertas e imprime el resultado explicable. |
| `runs [--limit N]` | Ejecuciones de búsqueda recientes y conteos almacenados. |
| `dashboard [--output PATH] [--top N]` | Escribe un informe HTML local autocontenido (solo lectura, sin red). |
| `import-feedback PATH [--project-id ID]` | Guarda una exportación de feedback del tablero en SQLite (sin red, idempotente). |
| `calibrate [--scorer heuristic\|llm\|opencode]` | Compara la última evaluación válida de cada oferta con el feedback humano importado (solo lectura, sin red). |

Códigos de salida: `0` éxito, `1` error, `2` rechazo (collector deshabilitado,
acceso denegado o solicitud que no se realizó a propósito).

Inspeccionar un plan sin ejecutarlo:

```bash
python -m jobscout search-linkedin --keywords "senior backend engineer" --location "Buenos Aires, Argentina" --max-pages 2 --dry-run
```

Otras fuentes públicas (opt-in, todas deshabilitadas por defecto):

```bash
python -m jobscout search-getmanfred --dry-run                       # plan, sin red: lee el sitemap público
python -m jobscout search-getmanfred --max-items 40 --confirm-network
python -m jobscout search-getmanfred --keywords "Platform Engineer" --max-items 40 --confirm-network  # filtro local
python -m jobscout search-tecnoempleo --keywords "cloud security" --location madrid --dry-run
python -m jobscout search-tecnoempleo --keywords "cloud security" --max-pages 2 --confirm-network
```

`search-getmanfred` es un **inventario**, no una búsqueda: no genera fan-out y
cada posición observada se guarda como `position_kind = none` con rank `0`. El
título se deriva del slug público de la URL y la empresa queda desconocida,
porque el sitemap no la expone. Los postings quedan en estado `partial`.

GetManfred no expone un endpoint de búsqueda por palabra clave. `--keywords`
aplica el término **localmente** al sitemap en una sola descarga: solo se
conservan las ofertas cuyo título/slug normalizado contiene todos los tokens del
término (sensible a acentos y mayúsculas, coincidencia por token, nunca por
substring: `front` no matchea `frontend`). No hay endpoint privado ni una
petición por palabra clave. Cada oferta conserva en
`extra['matched_keywords']` las keywords aprobadas que matcheó, de modo que una
misma oferta observada bajo una o varias keywords no mezcla términos ni
plataformas.

El filtro es **coincidencia exacta por token**: cada token del término aprobado
debe aparecer como token completo en el título/slug normalizado. No hay
sinónimos, alias ni variantes de familia, así que `DevOps` no matchea
`DevSecOps`, `Cloud` no matchea `Cloud Security` y una variante con otro orden
tampoco amplía el resultado. Las variantes candidatas observadas en el sitemap
son **DevOps, DevSecOps, Cloud Engineer, Cloud Security, Azure** y posiblemente
**SRE**; ampliar el catálogo aprobado con esas variantes es una **decisión
aparte** (revisión humana del catálogo), no un comportamiento automático de este
adaptador.

`search-tecnoempleo` usa solo la búsqueda pública server-rendered bajo
`/ofertas-trabajo/`. Antes del primer request vuelve a leer `robots.txt` y falla
cerrado si la ruta está deshabilitada. No usa login, cookies, RSS privado ni API
no autorizada. **Limitación declarada:** sus selectores no están verificados
contra una página en vivo (su `robots.txt` deshabilita crawlers automatizados), así
que confirmá `JOBSCOUT_TECNOEMPLEO_OFFER_PATH_MARKER` antes de una corrida real.

Convención de procedencia: `provider` (plataforma), `query` (palabra clave),
`cycle_id` (ciclo de búsqueda comparable) y `position_kind`
(`search_rank` = posición real en la búsqueda; `none` = inventario sin orden). La
analítica de palabras clave del tablero separa por `provider|keyword|cycle`, así
que el mismo término en LinkedIn, GetManfred y Tecnoempleo nunca se mezcla.

InfoJobs queda **bloqueado**: `https://www.infojobs.net/candidate/my-infojobs.xhtml`
es una página privada autenticada y solo podrá añadirse tras una autorización de
API oficial y credenciales de app explícitas. No hay comando CLI ni ruta de red.

Enriquecimiento de detalles:

```bash
python -m jobscout fetch-details --dry-run                                            # plan, sin red ni escrituras
python -m jobscout fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout fetch-details --max-items 5 --confirm-network                      # real, acotado
python -m jobscout fetch-details --all --max-items 10 --confirm-network               # re-extrae todo
python -m jobscout fetch-details --refresh --stale-days 14 --dry-run                  # plan de refresco, offline
```

`--refresh` es una selección mutuamente excluyente con `--job-id` y `--all`. Solo
considera ofertas activas que ya tienen historial de fetch y cuyo último intento
es uno de estos:

- un `ok`/`partial` anterior al cutoff (`--stale-days`, por defecto `0`);
- un `failed` que nunca recibió respuesta HTTP (`http_status` y `raw_document_id`
  nulos), es decir un fallo transitorio reintentable;
- un `404`/`410` refutado por un re-avistamiento posterior.

Quedan fuera las inactivas, las que nunca se consultaron, los intentos `skipped`
y los fallos que sí registraron una respuesta HTTP. Con `--stale-days 0` no hay
piso de antigüedad. El presupuesto de `--max-items` se gasta primero en la
evidencia más vieja.

`--refresh --dry-run` es de solo lectura y sin red: nunca crea la base ni escribe
filas. Un refresco real conserva sus mismas barreras de red que el resto de
`fetch-details` (`--confirm-network`, o `--fixture` para trabajar offline).

`fetch-details` no vuelve a puntuar por sí solo: ejecuta
`score --all --rescore` después de enriquecer.

Enriquecimiento de detalles de Tecnoempleo:

```bash
python -m jobscout fetch-tecnoempleo-details --dry-run                       # plan, sin red ni escrituras
python -m jobscout fetch-tecnoempleo-details --max-items 10 --confirm-network # real, acotado
python -m jobscout fetch-tecnoempleo-details --all --max-items 20 --confirm-network
```

Selecciona por defecto las ofertas **activas de Tecnoempleo** que todavía no
tienen descripción completa (`--all` amplía a todas las activas de esa
plataforma). Reutiliza el mismo modelo de seguridad que la búsqueda: sin login,
sin cookies, sin RSS privado ni API; vuelve a leer `robots.txt` y falla cerrado si
la ruta de detalle está deshabilitada; límites, pacing y techo de descarga
conservadores.

Extrae descripción, empresa, ubicación, fecha y tecnologías **solo de markup
reconocido** (`itemprop`/clases estables; la fecha prefiere `<time datetime>`).
Un detalle reconocido deja la oferta en `content_status = ok` con descripción no
vacía. Un detalle no reconocido, un 403 o un cuerpo truncado quedan en
`partial`/`failed` con un error honesto: nunca se inventa un `ok`. La decisión de
no reemplazar una descripción completa previa por un intento parcial o fallido la
aplica el pipeline al persistir.

Flujo completo: **búsqueda → enriquecimiento de detalle → scoring**.
`fetch-tecnoempleo-details` no vuelve a puntuar por sí solo; ejecuta
`score --all --rescore` (o `score --job-id ID`) después de enriquecer.

Importación de feedback:

```bash
python -m jobscout import-feedback jobscout-feedback.json            # importa la exportación de esta base
python -m jobscout import-feedback jobscout-feedback.json --json     # informe legible por máquina
python -m jobscout import-feedback old-export.json --project-id ID   # acepta la exportación de una base movida
```

Informe de calibración (offline, solo lectura):

```bash
python -m jobscout calibrate                          # todo el feedback importado
python -m jobscout calibrate --scorer heuristic       # solo evaluaciones de un puntuador
python -m jobscout calibrate --json                   # informe legible por máquina
```

### Notificaciones locales (opt-in, sin programación)

`notify` es una vista previa manual y de solo lectura de las ofertas de alto
puntaje. Está **desactivado por defecto** (`JOBSCOUT_NOTIFY_ENABLED=false`) y,
cuando está desactivado, no crea ni abre la base de datos.

```bash
# Vista previa (solo imprime; nunca envía nada)
python -m jobscout notify
python -m jobscout notify --limit 10 --json

# Entrega local explícita (opt-in); stdout por defecto
python -m jobscout notify --deliver

# Solo las que el filtro de preferencia marca para revisar
JOBSCOUT_NOTIFY_PREFERENCE_OUTCOME=flag python -m jobscout notify
```

Selecciona ofertas **activas** que tienen una evaluación exitosa almacenada con
puntaje ≥ `JOBSCOUT_NOTIFY_SCORE_THRESHOLD` (0–100, por defecto 80), que
opcionalmente coinciden con `JOBSCOUT_NOTIFY_PREFERENCE_OUTCOME`
(`pass`/`flag`/`fail`, vacío = sin filtro) y que **no tienen ninguna fila de
feedback humano importado**. Reutiliza la evaluación y la preferencia guardadas:
no vuelve a puntuar, no llama a un modelo y no hace ninguna solicitud de red.
`--limit` usa el tope existente de 250.

La entrega es una acción separada: sin `--deliver` solo imprime. Con `--deliver`
usa `JOBSCOUT_NOTIFY_DELIVERY` (`stdout` por defecto o `system`). `system` usa
solo un notificador local ya instalado (`notify-send` en Linux o
`terminal-notifier` en macOS) descubierto con `shutil.which`; si no existe,
vuelve a stdout. **No hay programador, daemon, cron, servicio en segundo plano,
ejecución automática, entrega remota ni postulación.**

## Puntuación

Los tres puntuadores devuelven la misma forma explicable: `overall_score` (0-100),
`dimensions`, `blockers`, `strengths`, `gaps`, `unknowns`, `confidence` (0-1),
`recommendation` (`apply` / `consider` / `skip` / `need_more_info`) y `rationale`.
Cada evaluación guarda además el puntuador, el modelo, la versión del prompt, el
id de la instantánea del perfil, un `input_fingerprint` y el documento crudo
asociado, para que todo puntaje sea auditable después.

| Puntuador | Cuándo usarlo | Requisitos |
|-----------|---------------|------------|
| `heuristic` | Siempre disponible, determinista y sin dependencias | ninguno |
| `llm` | Cualquier endpoint `/chat/completions` compatible con OpenAI | `JOBSCOUT_LLM_BASE_URL` y `JOBSCOUT_LLM_API_KEY` |
| `opencode` (predeterminado) | Ya usas OpenCode localmente y prefieres no manejar una clave | CLI `opencode` instalada y autenticada |

### Heurístico (sin conexión)

Determinista y sin dependencias. Dimensiones: `must_have_skills`, `skills`
(deseables), `seniority`, `location`, `language`, `recency` y `compensation`. Los
pesos salen del perfil y se renormalizan sobre las dimensiones aplicables.

### LLM compatible con OpenAI

```bash
export JOBSCOUT_LLM_BASE_URL=https://api.example.com/v1
export JOBSCOUT_LLM_API_KEY=...
python -m jobscout score --all --scorer llm --profile profile.yaml
```

Sin `JOBSCOUT_LLM_BASE_URL` y `JOBSCOUT_LLM_API_KEY` el puntuador LLM se niega a
ejecutarse y sugiere el heurístico. El modelo predeterminado es
`deepseek-v4.1-flash` y siempre es configurable con `JOBSCOUT_LLM_MODEL`. Las
respuestas se analizan de forma defensiva; una salida ilegible se registra como
`parse_error` en lugar de cortar el lote.

### CLI de OpenCode (sin clave de API)

```bash
# Una sola vez: autentica OpenCode con el proveedor del modelo
opencode auth login

# Modelo predeterminado gratuito: opencode/mimo-v2.5-free
python -m jobscout score --all --scorer opencode --profile profile.yaml
```

Si no pasás `--scorer`, se usa OpenCode con MiMo. Para una evaluación offline
explícita, usá `--scorer heuristic`.

Ejecuta `opencode run --pure --model <modelo> [--variant minimal] --format json
<prompt>` como arreglo de argumentos, nunca a través de un shell. La ruta al
ejecutable se resuelve solo por ruta absoluta o búsqueda en `PATH`. La salida del
proceso hijo se acota **mientras corre**: si supera
`JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` o vence el timeout, el proceso se termina.
Reutiliza el mismo prompt endurecido que el puntuador HTTP, así que la oferta se
trata como dato no confiable. Una salida incompleta queda como `parse_error` y
**no se reintenta automáticamente**: jobscout nunca inventa un puntaje. Vuelve a
ejecutar el puntuador para intentar de nuevo.

## Flujo de optimización de palabras clave

La cobertura de búsqueda vive en un catálogo explícito
(`src/jobscout/keywords.py`): familias acotadas de consultas, sin generación
automática ni expansión más allá de esa lista. Las familias `baseline` son las que
ya funcionaban (`Junior Cloud Engineer`, `Junior DevOps Engineer`, `Platform
Engineer`); las familias `security` agregan cobertura de Cloud Security y
DevSecOps (`Cloud Security Engineer`, `Junior Cloud Security Engineer`, `DevSecOps
Engineer`, `Junior DevSecOps Engineer`, `Azure Cloud Security`, `Azure DevSecOps`,
IAM y seguridad de infraestructura). Cada familia declara como máximo
`HARD_MAX_QUERIES_PER_FAMILY` consultas.

```bash
python -m jobscout keyword-plan                          # todas las familias, sin red
python -m jobscout keyword-plan --family cloud-security --family devsecops
python -m jobscout keyword-plan --family cloud-security --location "Madrid, Spain" --remote
python -m jobscout keyword-plan --json                   # informe legible por máquina
```

Cada línea impresa es un comando de vista previa (`--dry-run`). Convertirlo en una
búsqueda real es reemplazar `--dry-run` por `--confirm-network`, siempre una
acción humana explícita. `JOBSCOUT_KEYWORD_FAMILIES` fija qué familias se listan
cuando no pasas `--family`; un id desconocido falla al iniciar.

La procedencia de cada palabra clave (qué búsqueda trajo cada oferta y en qué
posición) se guarda en SQLite, y el tablero la agrega:

1. Ejecuta búsquedas acotadas con las palabras clave del plan
   (`search-linkedin ... --confirm-network`) o ingiere fixtures con etiquetas.
2. Enriquece con `fetch-details` y puntúa con `score --all --rescore`.
3. Exporta tu feedback desde el tablero e impórtalo con `import-feedback`. Sin
   feedback importado, las columnas de decisión humana quedan en *sin dato*.
4. Genera el tablero y abre la sección de **rendimiento de palabras clave**: una
   fila por palabra clave con *Apariciones*, *Ofertas únicas*, *Tasa duplicados*,
   *Puntaje promedio*, *Top 5*, *Top 10*, *Con puntaje*, *Aplicar*, *Considerar*,
   *Revisadas*, *Tasa aplicar (humano)*, *Frescura* y *No activas*.
5. Prioriza las palabras clave con mejor puntaje promedio, más aplicar/considerar
   del modelo y mejor tasa de aprobación humana; descarta las que solo aportan
   apariciones duplicadas o ya no producen ofertas activas.
6. Ajusta las palabras clave y repite. **Cada corrida es manual**: no hay
   programación automática. Las notificaciones locales son opt-in y manuales
   (`jobscout notify`).

Cómo leer las métricas:

- *Puntaje promedio*, *Top 5* y *Top 10* solo promedian ofertas activas con
  evaluación exitosa y muestran *sin dato* si no hay ninguna. *Top 5*/*Top 10*
  responden "¿qué tan buenas son mis mejores ofertas para esta búsqueda?", no
  "¿qué tan bueno es el promedio?".
- *Tasa duplicados* es apariciones repetidas sobre apariciones totales: una
  palabra clave que solo repite lo que ya trajeron otras aporta poco.
- *Tasa aplicar (humano)* usa únicamente las decisiones importadas
  (`apply`/`reject`, más el legacy `consider` que se sigue contando), nunca la
  recomendación del modelo. Se muestra como *sin dato* mientras no haya
  decisiones para esa palabra clave, y `0 %` solo cuando hubo decisiones reales y
  ninguna fue aplicar.
- *Frescura* es la proporción de ofertas de 21 días o menos entre las que tienen
  fecha conocida. jobscout **no** tiene una señal verificada de "oferta expirada
  en LinkedIn", así que no la inventa: *No activas* es la proporción de ofertas
  que jobscout ya desactivó, y es lo único que se afirma sobre ofertas caídas.

La posición observada es solo procedencia: el orden de resultados de LinkedIn
**no** se trata como señal de calidad.

## Privacidad y seguridad

Estas garantías las aplica el código, no solo la documentación:

- Datos locales (perfil, base SQLite y sus sidecars, capturas crudas, informes,
  `.env*` distintos de `.env.example`) están ignorados por Git. Nunca los
  publiques.
- Las URLs configuradas de LinkedIn deben ser **HTTPS en `linkedin.com` o
  `*.linkedin.com`**; la URL base del LLM debe ser HTTPS. Esquemas `file:`,
  `ftp:`, `data:`, `http:`, hosts parecidos y credenciales embebidas se rechazan
  al iniciar.
- Las redirecciones se siguen solo al mismo host (o bajo el dominio padre de
  LinkedIn); una redirección a otro host falla cerrado y nunca se reintenta.
- Solo se reintentan fallas transitorias (timeouts, 5xx). 401/403/429 y estados
  inesperados **fallan de inmediato**.
- Los payloads crudos se guardan solo si están por debajo de
  `JOBSCOUT_RAW_MAX_BYTES` y no parecen contener credenciales.
- El texto de la oferta se trata como dato no confiable: el LLM recibe la
  instrucción de no seguir instrucciones embebidas y los delimitadores de prompt
  se neutralizan.

### Riesgo residual: permisos de herramientas de OpenCode (H2)

`opencode run` (v1.14.30) **no expone una bandera confiable para denegar
herramientas ni para aislar la ejecución**. La única opción relacionada es
`--dangerously-skip-permissions`, que *amplía* el acceso y jobscout nunca la
pasa. La política de herramientas vive en la configuración local de OpenCode, que
jobscout no controla ni verifica.

Lo que sí hace hoy: ejecuta con `--pure` (sin plugins externos) por defecto, corre
desde un directorio neutral (el temporal del sistema, para no cargar las
instrucciones de este repositorio) y neutraliza la falsificación de delimitadores
en el prompt.

Lo que queda: un modelo inyectado por prompt o con comportamiento errático aún
podría intentar llamadas a herramientas permitidas por tu configuración local de
OpenCode. **Es un riesgo residual aceptado (H2).** Si necesitas una garantía
dura, configura un agente restrictivo en tu OpenCode local o no uses el puntuador
de OpenCode con ofertas no confiables.

### Alcance del tablero

El tablero no incluye descripciones, payloads crudos, el perfil ni datos de
contacto, claves de API o rutas locales absolutas. El feedback del navegador se
indexa por un hash de la ruta de la base (no por la ruta en sí). Algunos
navegadores restringen `localStorage` en páginas `file://`; si el tuyo lo hace, el
informe igual se renderiza y filtra, pero el botón de exportación es la única
forma de conservar tus decisiones.

## Estructura del proyecto

```text
.
├── .env.example              # Plantilla de configuración (todas las variables JOBSCOUT_*)
├── profile.example.yaml      # Perfil de ejemplo
├── pyproject.toml            # Metadatos, dependencias y configuración de pytest
├── src/jobscout/
│   ├── cli.py                # Comandos, validación de argumentos y códigos de salida
│   ├── config.py             # Settings, validación de URLs y redacción de secretos
│   ├── db.py                 # Esquema SQLite, consultas y conexión de solo lectura
│   ├── keywords.py           # Catálogo acotado de familias y consultas de búsqueda
│   ├── normalize.py          # Normalización de ofertas
│   ├── dedupe.py             # Huellas y deduplicación exacta/probable
│   ├── pipeline.py           # Ingesta de fixtures, búsquedas y detalles
│   ├── preference.py         # Exclusiones de preferencia (offline, sin tocar el puntaje)
│   ├── notify.py             # Selección y entrega local de notificaciones (opt-in, solo lectura)
│   ├── dashboard.py          # Generador del informe HTML autocontenido
│   ├── url_policy.py         # Validación de URLs y redirecciones
│   ├── collectors/           # LinkedIn guest, fixtures, rate limit, transporte HTTP
│   └── scoring/              # Heurístico, LLM, OpenCode, esquema y perfil
└── tests/
    ├── fixtures/             # Datos sintéticos de búsqueda, detalle y respuestas LLM
    └── test_*.py             # Suite offline
```

## Pruebas

```bash
python -m pytest -q
```

La suite es totalmente offline: el transporte HTTP se reemplaza por un doble de
prueba, las pruebas que deben demostrar que no hay red llegan a bloquear
`socket.socket`, y el CLI de OpenCode se reemplaza por un runner falso inyectado.
Ninguna prueba invoca un modelo ni el binario real de `opencode`.

Prueba de humo con fixtures (la base vive en `data/`, ignorada por Git):

```bash
python -m jobscout --db data/smoke.sqlite3 init-db
python -m jobscout --db data/smoke.sqlite3 ingest-fixture tests/fixtures/linkedin_search_cards_sample.json
python -m jobscout --db data/smoke.sqlite3 fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout --db data/smoke.sqlite3 --profile tests/fixtures/profile_test.yaml score --all --rescore
python -m jobscout --db data/smoke.sqlite3 rank --top 5
python -m jobscout --db data/smoke.sqlite3 dashboard --top 5 --output reports/smoke.html
```

## Notas de publicación y desarrollo

- El repositorio **no incluye pipeline de publicación ni CI**. La instalación
  prevista es local y editable (`pip install -e ".[dev]"`), o directamente con
  `PYTHONPATH=src`.
- Versión actual: `0.1.0`.
- Antes de cualquier publicación o PR, verifica que no se cuelen datos locales:
  `data/`, `reports/`, `profile.yaml` y `.env` están en `.gitignore`, pero conviene
  revisar `git status` igualmente.
- Mensajes de commit en estilo convencional; no agregues atribución de IA.
- Este proyecto es una herramienta de investigación local, no un servicio.

## Limitaciones conocidas y próximos pasos

- La extracción de detalles solo reconoce contenedores de marcado guest conocidos
  (`show-more-less-html__markup`, `description__text` y similares). Un marcado
  nuevo produce `partial` con un motivo explícito en lugar de una descripción
  inventada.
- `fetch-details` no vuelve a puntuar: hay que ejecutar `score --all --rescore`
  después.
- El parser de búsqueda soporta dos formas de tarjeta; una respuesta 200 con
  cuerpo no vacío pero sin tarjetas se registra como nota de diagnóstico y nunca
  corta la ejecución.
- El puntuador de OpenCode requiere un binario `opencode` instalado y autenticado
  localmente; jobscout no gestiona esa autenticación.
- `JOBSCOUT_OPENCODE_COMMAND` debe ser ruta absoluta o un nombre resoluble por
  `PATH`; una ruta relativa se rechaza.
- El parseo de salario es heurístico y solo se usa cuando el perfil define un
  mínimo.
- El tablero embebe solo las `--top` mejores ofertas, pero la analítica de
  palabras clave se calcula sobre toda la base.
- `keyword-plan` no ejecuta búsquedas: imprime el plan acotado y el comando por
  consulta. No existe una corrida por lote que dispare varias búsquedas en vivo;
  cada query es un `search-linkedin` explícito con `--confirm-network`.
- Las métricas humanas del tablero (frecuencia de aprobación) requieren importar
  la exportación de feedback; mientras no se importe, quedan en *sin dato*. La
  comparación entre recomendación del modelo y decisión humana está disponible con
  `calibrate` (solo lectura, sin ajustar prompts ni puntajes).
- **No hay programador de tareas ni postulación automática** — por diseño. Las
  notificaciones locales son opt-in, manuales y de solo lectura
  (`jobscout notify`, desactivadas por defecto). La importación de feedback
  existe, pero solo se ejecuta cuando invocas
  `import-feedback`.
