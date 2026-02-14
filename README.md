# ESPOL Malla Scraper

Scraper en Python para extraer la **malla curricular** de una carrera de la ESPOL y generar un **JSON limpio** listo para consumir en una app web (React/Next).

Obtiene:

- Materias por carrera (código + nombre)
- **Prerrequisitos / correquisitos** (desde el modal AJAX)
- Requisitos tipo “N materias aprobadas” (cuando aplica)
- **Créditos** (desde el SVG)
- **Unidad / tipo**: `BASICO | PROFESIONAL | INTEGRACION | CPI` (desde el SVG)
- **Posición en grid**: `grid.row` (semestre/fila) y `grid.col` (columna visual)
- Bloques **CPI** (Complementarias, Prácticas, Itinerario, etc.) también dentro del grid

> Fuente de datos:
> - Página malla: `https://mallacurricular.espol.edu.ec/Malla/Imagen?codCarrera=CI013`
> - SVG: `https://www.academico.espol.edu.ec/imgMallas/CI013v3.svg`
> - AJAX modal requisitos: `POST https://mallacurricular.espol.edu.ec/Malla/MateriasRequisitos`

---

## Output

Genera un archivo:

```
out/<COD_CARRERA>_final.json
```

Ejemplo de objeto:

```json
{
  "id": "CI013:MATG1049",
  "code": "MATG1049",
  "name": "ÁLGEBRA LINEAL",
  "unit": "BASICO",
  "credits": 3,
  "grid": { "row": 2, "col": 4 },
  "requirements": [
    { "code": "MATG1045", "name": "CÁLCULO DE UNA VARIABLE", "type": "PRE-REQUISITO" }
  ],
  "approved_count_requirement": null
}
```

Un bloque CPI:

```json
{
  "id": "CI013:CPI:63",
  "code": null,
  "name": "ITINERARIO",
  "unit": "CPI",
  "credits": 9,
  "grid": { "row": 7, "col": 9 },
  "requirements": [],
  "approved_count_requirement": null
}
```

---

## Requisitos

- Python 3.10+ recomendado
- Paquetes:
  - `httpx`
  - `lxml`
  - `rich`

---

## Instalación

### Windows (PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\activate
py -m pip install -U pip
pip install httpx lxml rich
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install httpx lxml rich
```

---

## Uso

Ejecuta el scraper indicando el código de carrera:

```bash
py .\scrapper.py --career CI013 --concurrency 12
```

Parámetros comunes:

- `--career` Código de carrera (ej: `CI013`)
- `--concurrency` Número de requests concurrentes (default 12)
- `--out` Carpeta de salida (default `out`)
- `--x-threshold` / `--y-threshold` Ajuste del clustering para columnas/filas (si alguna malla queda “corrida”)
- `--col-offset` Offset de columnas si tu layout necesita desplazar el índice (en algunas mallas hay columna vacía)

Ejemplo con ajuste:

```bash
py .\scrapper.py --career CI013 --concurrency 16 --x-threshold 70 --y-threshold 28 --col-offset 0
```

---

## Cómo funciona (resumen técnico)

1. **HTML overlay (Imagen)**  
   Descarga `Malla/Imagen?codCarrera=...`, detecta casillas clickeables (`<a class="casilla">`) y extrae:
   - `mallaid`, `posicion`, `lang`
   - `data-left/data-top` o `style left/top` (para coordenadas base)

2. **Modal AJAX (MateriasRequisitos)**  
   Por cada casilla hace `POST /Malla/MateriasRequisitos` y parsea:
   - `Código`, `Materia`
   - Tabla de `PRE-REQUISITO / CO-REQUISITO`
   - Texto “Esta materia tiene como requisito N materias aprobadas” cuando exista

3. **SVG (académico)**  
   Descarga el SVG `.../imgMallas/<career>v3.svg` y extrae:
   - `unit` (B/P/I) => `BASICO/PROFESIONAL/INTEGRACION`
   - `credits`
   - Centros geométricos (aplicando transforms del SVG) para “anclar” CPI al grid

4. **Grid responsive**  
   Calcula centros de filas/columnas por clustering (HTML), luego:
   - asigna `grid.row/grid.col` a materias normales
   - mapea coordenadas SVG→HTML (regresión lineal) y ubica CPI sin `overlap`

5. **JSON final limpio**  
   El JSON final **no incluye** metadata interna (`layout/meta/source`), solo lo necesario para la app.

---

## Troubleshooting

### “Todas quedaron CPI”
Suele pasar si el detector de `unit` falló y el script puso CPI como fallback.  
Solución: asegúrate de que el fallback sea `UNKNOWN` (no CPI) y revisa logs de cuántas unidades detectó el SVG.

### Materias “corridas” en grid
Ajusta clustering:
- Si junta 2 filas: baja `--y-threshold`
- Si parte una fila: sube `--y-threshold`
- Si junta 2 columnas: baja `--x-threshold`
- Si parte una columna: sube `--x-threshold`

### Rate limits / timeouts
Baja concurrencia:

```bash
py .\scrapper.py --career CI013 --concurrency 6
```

---

## Notas legales / éticas
Este proyecto consume endpoints públicos. Úsalo de forma responsable:
- evita concurrencias excesivas
- cachea resultados si vas a re-scrapear seguido
- no hagas scraping continuo sin necesidad

---

## Roadmap (ideas)
- Generar `index.json` con todas las carreras disponibles
- Subir JSON a bucket + CDN (S3/R2/GCS) desde un pipeline
- Validación de schema (zod/pydantic) y test de consistencia
- Normalización de nombres/códigos y deduplicación avanzada
