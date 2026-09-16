# jobscout

Herramienta **local-first** para buscar empleo: recolecta resultados públicos de
búsqueda de LinkedIn a muy bajo volumen, los normaliza y deduplica en SQLite, y
puntúa cada oferta contra un perfil estructurado local. El puntuador
predeterminado es determinista y funciona sin conexión; además hay dos
puntuadores opcionales: uno compatible con OpenAI y otro que reutiliza tu CLI de
OpenCode ya autenticada (sin clave de API).

Nada sale de tu equipo salvo las solicitudes que autorizas explícitamente.
**jobscout no envía postulaciones automáticas.**

**Idiomas:** Español (este archivo) · [English version](README.en.md)

## Qué hace y qué no hace

| Hace | No hace |
|------|---------|
| Recolecta tarjetas públicas de búsqueda (opt-in y acotado) | No inicia sesión, no usa cookies, no resuelve CAPTCHA, no evade antibots |
| Enriquece ofertas con la descripción completa (opt-in y acotado) | No postula, no contacta a nadie, no automatiza candidaturas |
| Deduplica y guarda todo en SQLite local | No envía datos a ningún servidor propio |
| Puntúa con heurística, LLM compatible con OpenAI u OpenCode CLI | No programa ejecuciones ni envía notificaciones |
| Genera un tablero HTML local en español | No escribe el feedback humano en la base de datos |

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
| Se guarda en | tabla `evaluations` de SQLite, con procedencia completa | `localStorage` del navegador, indexado por proyecto + id de oferta |
| ¿`jobscout` lo lee? | sí | no — la CLI nunca lo lee |
| ¿Es portable? | sí, está en la base | solo con el botón **Exportar feedback local (JSON)** |

Cada oferta tiene botones de feedback local (**Aplicar / Revisar / Descartar**,
almacenados como `apply` / `consider` / `reject`) y un motivo opcional. La
etiqueta humana *Revisar* es deliberadamente distinta de la recomendación
*Considerar* del modelo. El feedback es **solo local**: no postula, no toca
LinkedIn y no escribe en SQLite. La exportación descarga
`jobscout-feedback.json` con el esquema `jobscout.feedback.v1`.

**Aún no existe importación de feedback** ni programación automática: el JSON
exportado es el formato para transportarlo a mano.

## Referencia de CLI

Opciones globales: `--db PATH`, `--profile PATH`, `--json` (antes o después del
subcomando) y `--version`.

| Comando | Propósito |
|---------|-----------|
| `init-db` | Crea o verifica el esquema. Idempotente. |
| `ingest-fixture PATH [--source S] [--label L]` | Normaliza, deduplica y guarda un fixture JSON (sin red). |
| `search-linkedin --keywords K [--location L] [--distance N] [--remote] [--employment-type T] [--max-pages N] [--max-items N] [--dry-run \| --confirm-network]` | Búsqueda pública acotada. Deshabilitada sin `--confirm-network`. |
| `fetch-details [--job-id ID… \| --all] [--max-items N] [--dry-run \| --fixture PATH \| --confirm-network] [--label L]` | Enriquece ofertas con la descripción completa. Por defecto, las que aún no están enriquecidas. |
| `list [--limit N] [--min-score S] [--include-inactive]` | Ofertas recientes con su último puntaje. |
| `rank [--top N] [--min-score S]` | Ofertas con puntaje exitoso, mejor primero. |
| `score (--job-id ID… \| --all) [--scorer heuristic\|llm\|opencode] [--limit N] [--rescore]` | Evalúa ofertas e imprime el resultado explicable. |
| `runs [--limit N]` | Ejecuciones de búsqueda recientes y conteos almacenados. |
| `dashboard [--output PATH] [--top N]` | Escribe un informe HTML local autocontenido (solo lectura, sin red). |

Códigos de salida: `0` éxito, `1` error, `2` rechazo (collector deshabilitado,
acceso denegado o solicitud que no se realizó a propósito).

Inspeccionar un plan sin ejecutarlo:

```bash
python -m jobscout search-linkedin --keywords "senior backend engineer" --location "Buenos Aires, Argentina" --max-pages 2 --dry-run
```

Enriquecimiento de detalles:

```bash
python -m jobscout fetch-details --dry-run                                            # plan, sin red ni escrituras
python -m jobscout fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout fetch-details --max-items 5 --confirm-network                      # real, acotado
python -m jobscout fetch-details --all --max-items 10 --confirm-network               # re-extrae todo
```

`fetch-details` no vuelve a puntuar por sí solo: ejecuta
`score --all --rescore` después de enriquecer.

## Puntuación

Los tres puntuadores devuelven la misma forma explicable: `overall_score` (0-100),
`dimensions`, `blockers`, `strengths`, `gaps`, `unknowns`, `confidence` (0-1),
`recommendation` (`apply` / `consider` / `skip` / `need_more_info`) y `rationale`.
Cada evaluación guarda además el puntuador, el modelo, la versión del prompt, el
id de la instantánea del perfil, un `input_fingerprint` y el documento crudo
asociado, para que todo puntaje sea auditable después.

| Puntuador | Cuándo usarlo | Requisitos |
|-----------|---------------|------------|
| `heuristic` (predeterminado) | Siempre disponible, determinista y sin dependencias | ninguno |
| `llm` | Cualquier endpoint `/chat/completions` compatible con OpenAI | `JOBSCOUT_LLM_BASE_URL` y `JOBSCOUT_LLM_API_KEY` |
| `opencode` | Ya usas OpenCode localmente y prefieres no manejar una clave | CLI `opencode` instalada y autenticada |

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

# Modelo predeterminado: opencode-go/deepseek-v4.1-flash
python -m jobscout score --all --scorer opencode --profile profile.yaml
```

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

La procedencia de cada palabra clave (qué búsqueda trajo cada oferta y en qué
posición) se guarda en SQLite, y el tablero la agrega:

1. Ejecuta búsquedas acotadas con las palabras clave que quieras evaluar
   (`search-linkedin ... --confirm-network`) o ingiere fixtures con etiquetas.
2. Enriquece con `fetch-details` y puntúa con `score --all --rescore`.
3. Genera el tablero y abre la sección de **rendimiento de palabras clave**: una
   fila por palabra clave con *Apariciones*, *Ofertas únicas*, *Apariciones
   duplicadas*, *Puntaje promedio*, *Con puntaje*, *Aplicar* y *Considerar*.
4. Prioriza las palabras clave con mejor puntaje promedio y más
   aplicar/considerar; descarta las que solo aportan apariciones duplicadas.
   *Puntaje promedio* solo promedia ofertas con evaluación exitosa y muestra
   *sin dato* si no hay ninguna.
5. Ajusta las palabras clave y repite. **Cada corrida es manual**: no hay
   programación automática ni notificaciones.

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
│   ├── normalize.py          # Normalización de ofertas
│   ├── dedupe.py             # Huellas y deduplicación exacta/probable
│   ├── pipeline.py           # Ingesta de fixtures, búsquedas y detalles
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
- **No hay importación de feedback, programador de tareas, notificaciones ni
  postulación automática** — por diseño.
