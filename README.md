# Finanzas Local

App web local que parsea resúmenes de tarjeta de crédito (Santander Río y Galicia, PDF y xlsx),
categoriza los gastos con reglas locales, detecta gastos fijos (recurrentes) y muestra un
dashboard mensual con análisis y compromisos por cuotas.

100% local — no se manda nada a APIs externas. Tus datos viven en `data/data.db` (SQLite) y los
resúmenes originales se copian a `statements/`.

## Setup

Necesitás:
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (gestor de entornos de Python)

```bash
cd ~/finanzas
uv sync
uv run finanzas
```

Esto:
1. Crea la DB en `data/data.db` con todas las tablas y seedea categorías y reglas para
   merchants AR comunes (Carrefour, Coto, Rappi, Netflix, Edenor, Metrogas, etc.).
2. Arranca el servidor en http://localhost:8000 y abre tu browser.

## Workflow típico

1. **Subí un resumen** (PDF o xlsx) en `/upload`.
2. Las transacciones que matchean alguna regla se categorizan solas.
3. Las que no matchean caen al **review inbox** (`/review`). Las categorizás manualmente; si dejás
   marcado el checkbox "recordar", la próxima vez que aparezca ese merchant se categoriza solo.
4. El **dashboard** te muestra:
   - Gasto del mes con delta vs promedio últimos 3 meses
   - Distribución por categoría (treemap)
   - **Forecast de cuotas próximos 6 meses**
   - Top 10 merchants con delta vs mes pasado
   - Gastos fijos detectados/sugeridos
   - Tendencia 6M
   - Insights estadísticos automáticos
5. **Gastos manuales**: cargá expensas, alquiler, efectivo, etc. en `/manual`. Opcionalmente con
   recurrencia mensual.

## Detección de gastos fijos

Necesita histórico (3+ ocurrencias). Después de subir 2-3 meses de resúmenes, los merchants que
aparecen recurrentemente con monto similar (±15-20%) y cadencia mensual (28-32 días) se
proponen como recurrentes. Vos confirmás o rechazás desde el dashboard.

## Banks soportados

| Banco | PDF | xlsx |
|-------|-----|------|
| Santander Río | ✅ | ✅ |
| Galicia | ✅ | — |

Agregar otros bancos: crear un parser en `src/finanzas/parsers/<banco>.py` que devuelva un
`ParsedStatement`. Después agregarlo al dispatcher en `src/finanzas/parsers/dispatch.py`.

## Estructura

```
finanzas/
├── pyproject.toml
├── seeds/                  # categorías y reglas que se cargan al iniciar la DB
│   ├── categories.yaml
│   └── rules.yaml
├── src/finanzas/
│   ├── __main__.py         # uv run finanzas
│   ├── app.py              # FastAPI + rutas + charts
│   ├── db.py               # SQLite + migraciones + seeding
│   ├── models.py           # dataclasses
│   ├── ingest.py           # orquestador upload → parse → dedup → categorize
│   ├── parsers/            # un parser por banco/formato
│   ├── categorizer.py      # aplica rules → category
│   ├── recurring.py        # detector de gastos fijos
│   ├── insights.py         # frases estadísticas para el dashboard
│   ├── queries.py          # queries reutilizables
│   └── templates/          # Jinja2 + HTMX + Tailwind via CDN
├── data/                   # gitignored — DB SQLite
├── statements/             # gitignored — archivos originales
└── tests/
```

## Comandos útiles

```bash
uv run finanzas               # arrancar app
uv run pytest                 # correr tests
uv run pytest -v              # tests con detalle
FINANZAS_DB=/tmp/test.db uv run finanzas   # usar DB alternativa
```

## Privacidad

No se llama a ninguna API externa. Sin LLMs. La única conexión a internet es por la red de Tailwind/HTMX/Plotly via CDN (assets visuales) cuando abrís el dashboard en el browser.

Tu DB (`data/data.db`) y los resúmenes originales (`statements/`) están gitignored.
