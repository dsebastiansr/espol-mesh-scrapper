import argparse
import asyncio
import json
import logging
import math
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional, Tuple

import httpx
from lxml import etree, html

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
)

# ----------------- Logging -----------------
console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, console=console, markup=False)],
)
log = logging.getLogger("malla-scraper")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ----------------- URLs / Const -----------------
BASE = "https://mallacurricular.espol.edu.ec"
IMG_URL = f"{BASE}/Malla/Imagen"
REQ_URL = f"{BASE}/Malla/MateriasRequisitos"

SVG_BASE = "https://www.academico.espol.edu.ec/imgMallas"

CODE_RE = re.compile(r"([A-Z]{3,}\d{3,})")
UNIT_RE = re.compile(r"\b([BPI])\b")  # en SVG como token
CREDITS_RE = re.compile(r"\bCREDITOS\b\s*([0-9]+)")  # texto normalizado sin tildes

UNIT_MAP = {"B": "BASICO", "P": "PROFESIONAL", "I": "INTEGRACION"}

# CPI canonical blocks (normalizamos a mayúsculas sin tildes)
CPI_CANON = [
    ("COMPLEMENTARIAS DE HUMANISTICA", ["COMPLEMENTARIAS", "HUMAN"]),
    ("COMPLEMENTARIAS DE ARTES, DEPORTE E IDIOMA", ["COMPLEMENTARIAS", "ARTE"]),
    ("PRACTICAS DE SERVICIO COMUNITARIO", ["PRACTICAS", "SERVICIO", "COMUNIT"]),
    ("ITINERARIO", ["ITINERARIO"]),
    ("PRACTICAS PREPROFESIONALES EMPRESARIALES", ["PRACTICAS", "PREPROF"]),
]


# ----------------- Helpers -----------------
def _strip_accents(s: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", s)
        if unicodedata.category(ch) != "Mn"
    )

def _u(s: str) -> str:
    s = s.replace("\u00A0", " ")
    s = re.sub(r"\s+", " ", s).strip().upper()
    return _strip_accents(s)

def _normalize_group_text_no_spaces(s: str) -> str:
    s = s.replace("\u00A0", "")
    s = re.sub(r"\s+", "", s)
    return s.strip()

def _normalize_group_text_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u00A0", " ")).strip()

def _to_int(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except:
        return None

def _to_float(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except:
        return None

def _parse_style(style: str | None) -> dict[str, str]:
    if not style:
        return {}
    out = {}
    for part in style.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k.strip().lower()] = v.strip()
    return out

def _parse_px(s: str | None) -> Optional[float]:
    if not s:
        return None
    s = s.strip().lower().replace("px", "").strip()
    try:
        return float(s)
    except:
        return None


# ----------------- Modal parsing (AJAX HTML) -----------------
def parse_requirements_fragment(fragment_html: str) -> dict[str, Any]:
    doc = html.fromstring(fragment_html)

    def value_after_label(label_text: str) -> Optional[str]:
        xp = (
            f"//label[normalize-space()='{label_text}']"
            "/ancestor::div[contains(@class,'row')][1]"
            "//div[contains(@class,'col-md-10')][1]"
        )
        nodes = doc.xpath(xp)
        if not nodes:
            return None
        return nodes[0].text_content().strip()

    code = value_after_label("Código:")
    name = value_after_label("Materia:")

    reqs: list[dict[str, str]] = []
    for tr in doc.cssselect("table.tbl-requisitos tbody tr"):
        tds = [re.sub(r"\s+", " ", td.text_content()).strip() for td in tr.cssselect("td")]
        if len(tds) >= 3 and tds[0]:
            reqs.append({"code": tds[0], "name": tds[1], "type": tds[2]})

    full = re.sub(r"\s+", " ", doc.text_content()).strip()
    approved_count = None
    m = re.search(r"requisito\s+(\d+)\s+materias\s+aprobadas", full, re.IGNORECASE)
    if m:
        approved_count = int(m.group(1))

    return {
        "code": code,
        "name": name,
        "requirements": reqs,
        "approved_count_requirement": approved_count,
    }


# ----------------- HTML overlay parsing -----------------
async def fetch_positions(client: httpx.AsyncClient, career: str) -> list[dict[str, Any]]:
    log.info(f"GET Imagen malla: {career}")
    r = await client.get(IMG_URL, params={"codCarrera": career})
    r.raise_for_status()

    doc = html.fromstring(r.text)
    anchors = doc.cssselect("a.casilla[onclick*='consultarMateriasRequisitos']")

    out: list[dict[str, Any]] = []
    for a in anchors:
        onclick = a.get("onclick", "")
        m = re.search(
            r'consultarMateriasRequisitos\((\d+)\s*,\s*(\d+)\s*,\s*"?(\w+)"?\)',
            onclick,
        )
        if not m:
            continue

        mallaid, posicion, lang = int(m.group(1)), int(m.group(2)), m.group(3)

        st = _parse_style(a.get("style") or "")

        # left/top: prefer data-left/top si existen
        x = _to_int(a.get("data-left")) or _to_int(st.get("left"))
        y = _to_int(a.get("data-top")) or _to_int(st.get("top"))

        w = _parse_px(st.get("width"))
        h = _parse_px(st.get("height"))

        out.append(
            {
                "mallaid": mallaid,
                "posicion": posicion,
                "lang": lang,
                "layout": {"x": x, "y": y, "w": w, "h": h},  # interno
            }
        )

    # dedup
    seen = set()
    uniq = []
    for x in out:
        key = (x["mallaid"], x["posicion"], x["lang"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(x)

    log.info(f"Encontradas {len(uniq)} casillas clickeables (posiciones).")
    return uniq


async def fetch_requirements_html(client: httpx.AsyncClient, mallaid: int, posicion: int, lang: str) -> str:
    r = await client.post(REQ_URL, data={"mallaid": mallaid, "posicion": posicion, "lang": lang})
    r.raise_for_status()
    return r.text


# ----------------- SVG download -----------------
def download_svg_bytes(svg_url: str) -> bytes:
    r = httpx.get(svg_url, timeout=30.0, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.content


# ----------------- SVG: unit + credits maps -----------------
def extract_code_to_unit_from_svg(svg_bytes: bytes) -> dict[str, str]:
    root = etree.fromstring(svg_bytes, parser=etree.XMLParser(recover=True))
    out: dict[str, str] = {}

    groups = root.xpath("//*[local-name()='g']")
    for g in groups:
        raw = "".join(g.itertext())
        if not raw:
            continue

        # 1) detectar código
        no_spaces = _normalize_group_text_no_spaces(raw)
        mcode = CODE_RE.search(no_spaces)
        if not mcode:
            continue
        code = mcode.group(1)

        # 2) detectar unidad SOLO si un <text> COMPLETO es B/P/I
        best_letter = None
        best_x = float("inf")

        for t in g.xpath(".//*[local-name()='text']"):
            txt = "".join(t.itertext())  # <-- clave (incluye tspan)
            txt = re.sub(r"\s+", " ", txt).strip().upper()

            if txt in ("B", "P", "I"):
                # ayuda a desambiguar si aparecen varias letras
                x = _to_float(t.get("x")) or float("inf")
                if x < best_x:
                    best_x = x
                    best_letter = txt

        if best_letter:
            out.setdefault(code, UNIT_MAP.get(best_letter, best_letter))

    return out


def extract_code_to_credits_from_svg(svg_bytes: bytes) -> dict[str, int]:
    root = etree.fromstring(svg_bytes, parser=etree.XMLParser(recover=True))
    out: dict[str, int] = {}

    groups = root.xpath("//*[local-name()='g']")
    for g in groups:
        raw = "".join(g.itertext())
        if not raw:
            continue

        no_spaces = _normalize_group_text_no_spaces(raw)
        mcode = CODE_RE.search(no_spaces)
        if not mcode:
            continue
        code = mcode.group(1)

        up = _u(raw)
        mcred = CREDITS_RE.search(up)
        if not mcred:
            continue

        credits = int(mcred.group(1))
        if code not in out:
            out[code] = credits

    return out


# ----------------- SVG transforms (para ubicar CPI correctamente) -----------------
def _mat_mul(A, B):
    return [
        [
            A[0][0]*B[0][0] + A[0][1]*B[1][0] + A[0][2]*B[2][0],
            A[0][0]*B[0][1] + A[0][1]*B[1][1] + A[0][2]*B[2][1],
            A[0][0]*B[0][2] + A[0][1]*B[1][2] + A[0][2]*B[2][2],
        ],
        [
            A[1][0]*B[0][0] + A[1][1]*B[1][0] + A[1][2]*B[2][0],
            A[1][0]*B[0][1] + A[1][1]*B[1][1] + A[1][2]*B[2][1],
            A[1][0]*B[0][2] + A[1][1]*B[1][2] + A[1][2]*B[2][2],
        ],
        [
            A[2][0]*B[0][0] + A[2][1]*B[1][0] + A[2][2]*B[2][0],
            A[2][0]*B[0][1] + A[2][1]*B[1][1] + A[2][2]*B[2][1],
            A[2][0]*B[0][2] + A[2][1]*B[1][2] + A[2][2]*B[2][2],
        ],
    ]

def _mat_apply(M, x: float, y: float):
    nx = M[0][0]*x + M[0][1]*y + M[0][2]
    ny = M[1][0]*x + M[1][1]*y + M[1][2]
    return nx, ny

def _parse_transform(transform: str):
    I = [[1,0,0],[0,1,0],[0,0,1]]
    if not transform:
        return I

    M = I
    for fn, args in re.findall(r"([a-zA-Z]+)\(([^)]*)\)", transform):
        parts = [p for p in re.split(r"[,\s]+", args.strip()) if p]
        vals = [float(p) for p in parts] if parts else []

        fn = fn.lower()
        if fn == "translate":
            tx = vals[0] if len(vals) > 0 else 0.0
            ty = vals[1] if len(vals) > 1 else 0.0
            T = [[1,0,tx],[0,1,ty],[0,0,1]]
            M = _mat_mul(M, T)

        elif fn == "scale":
            sx = vals[0] if len(vals) > 0 else 1.0
            sy = vals[1] if len(vals) > 1 else sx
            S = [[sx,0,0],[0,sy,0],[0,0,1]]
            M = _mat_mul(M, S)

        elif fn == "matrix":
            if len(vals) == 6:
                a,b,c,d,e,f = vals
                MM = [[a,c,e],[b,d,f],[0,0,1]]
                M = _mat_mul(M, MM)

        elif fn == "rotate":
            ang = vals[0] if len(vals) > 0 else 0.0
            rad = ang * math.pi / 180.0
            cosv = math.cos(rad)
            sinv = math.sin(rad)
            R = [[cosv,-sinv,0],[sinv,cosv,0],[0,0,1]]
            if len(vals) == 3:
                cx, cy = vals[1], vals[2]
                T1 = [[1,0,cx],[0,1,cy],[0,0,1]]
                T2 = [[1,0,-cx],[0,1,-cy],[0,0,1]]
                M = _mat_mul(M, T1)
                M = _mat_mul(M, R)
                M = _mat_mul(M, T2)
            else:
                M = _mat_mul(M, R)

    return M

def _cumulative_transform(el):
    mats = []
    cur = el
    while cur is not None:
        t = cur.get("transform")
        if t:
            mats.append(_parse_transform(t))
        cur = cur.getparent()
    M = [[1,0,0],[0,1,0],[0,0,1]]
    for m in reversed(mats):
        M = _mat_mul(M, m)
    return M

def _bbox_of_rect_abs(rect):
    x = float(rect.get("x") or 0.0)
    y = float(rect.get("y") or 0.0)
    w = float(rect.get("width") or 0.0)
    h = float(rect.get("height") or 0.0)

    M = _cumulative_transform(rect)

    pts = [
        _mat_apply(M, x, y),
        _mat_apply(M, x+w, y),
        _mat_apply(M, x, y+h),
        _mat_apply(M, x+w, y+h),
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    return minx, miny, (maxx - minx), (maxy - miny)

def _get_biggest_rect(g: etree._Element):
    rects = g.xpath(".//*[local-name()='rect']")
    best = None
    best_area = -1.0
    for r in rects:
        w = _to_float(r.get("width")) or 0.0
        h = _to_float(r.get("height")) or 0.0
        area = w * h
        if area > best_area:
            best_area = area
            best = r
    return best

def _pick_canonical_cpi_name(up_noacc: str) -> Optional[str]:
    for name, kws in CPI_CANON:
        if all(k in up_noacc for k in kws):
            return name
    return None

def extract_svg_centers(svg_bytes: bytes) -> tuple[dict[str, tuple[float,float]], dict[int, tuple[float,float]], dict[int, int], dict[int, str]]:
    """
    Devuelve:
      - code_svg_center: code -> (cx, cy)  (coords SVG reales, con transform aplicado)
      - cpi_center_by_cas: casillero -> (cx, cy)
      - cpi_credits_by_cas: casillero -> credits
      - cpi_name_by_cas: casillero -> canonical name
    """
    root = etree.fromstring(svg_bytes, parser=etree.XMLParser(recover=True))

    code_svg_center: dict[str, tuple[float,float]] = {}
    cpi_center_by_cas: dict[int, tuple[float,float]] = {}
    cpi_credits_by_cas: dict[int, int] = {}
    cpi_name_by_cas: dict[int, str] = {}

    itinerario_kept = 0

    groups = root.xpath("//*[local-name()='g']")
    for g in groups:
        raw = "".join(g.itertext())
        if not raw:
            continue

        rect = _get_biggest_rect(g)
        if rect is None:
            continue

        ax, ay, aw, ah = _bbox_of_rect_abs(rect)
        cx = ax + aw/2.0
        cy = ay + ah/2.0

        nospaces = _normalize_group_text_no_spaces(raw)
        mcode = CODE_RE.search(nospaces)
        if mcode:
            code = mcode.group(1)
            if code not in code_svg_center:
                code_svg_center[code] = (cx, cy)
            continue

        up = _u(raw)

        # CPI: requiere casillero y nombre canónico
        mcas = re.search(r"CASILLERO\s*(\d+)", up)
        if not mcas:
            continue
        cas = int(mcas.group(1))

        cname = _pick_canonical_cpi_name(up)
        if not cname:
            continue

        if cname == "ITINERARIO":
            if itinerario_kept >= 2:
                continue
            itinerario_kept += 1

        mcred = CREDITS_RE.search(up)
        credits = int(mcred.group(1)) if mcred else None

        cpi_center_by_cas[cas] = (cx, cy)
        if credits is not None:
            cpi_credits_by_cas[cas] = credits
        cpi_name_by_cas[cas] = cname

    return code_svg_center, cpi_center_by_cas, cpi_credits_by_cas, cpi_name_by_cas


# ----------------- Grid (row/col) from HTML -----------------
def _cluster_centers(values: list[float], threshold: float) -> list[float]:
    if not values:
        return []
    values = sorted(values)
    centers: list[float] = []
    counts: list[int] = []
    for v in values:
        if not centers:
            centers.append(v)
            counts.append(1)
            continue
        if abs(v - centers[-1]) <= threshold:
            c = centers[-1]
            n = counts[-1]
            centers[-1] = (c * n + v) / (n + 1)
            counts[-1] = n + 1
        else:
            centers.append(v)
            counts.append(1)
    return centers

def _nearest_index(centers: list[float], v: float) -> int:
    best_i = 0
    best_d = float("inf")
    for i, c in enumerate(centers):
        d = abs(v - c)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i  # 0-based

def assign_grid_from_html(items: list[dict[str, Any]], y_threshold: float, x_threshold: float, col_offset: int) -> dict[str, Any]:
    sem = [it for it in items if it.get("code") and it.get("_layout", {}).get("x") is not None and it.get("_layout", {}).get("y") is not None]
    xs = [float(it["_layout"]["x"]) for it in sem]
    ys = [float(it["_layout"]["y"]) for it in sem]

    row_centers = _cluster_centers(ys, y_threshold)
    col_centers = _cluster_centers(xs, x_threshold)

    for it in sem:
        x = float(it["_layout"]["x"])
        y = float(it["_layout"]["y"])
        row = _nearest_index(row_centers, y) + 1
        col = _nearest_index(col_centers, x) + 1 + col_offset
        it["grid"] = {"row": row, "col": col}

    return {"row_centers": row_centers, "col_centers": col_centers, "col_offset": col_offset}


# ----------------- Fit SVG -> HTML (linear) -----------------
def _linreg(pairs: list[tuple[float, float]]) -> Optional[tuple[float, float]]:
    # y = a*x + b
    n = len(pairs)
    if n < 2:
        return None
    sx = sum(x for x,_ in pairs)
    sy = sum(y for _,y in pairs)
    sxx = sum(x*x for x,_ in pairs)
    sxy = sum(x*y for x,y in pairs)
    den = n*sxx - sx*sx
    if abs(den) < 1e-9:
        return None
    a = (n*sxy - sx*sy)/den
    b = (sy - a*sx)/n
    return a, b

def place_cpi_into_grid(
    items: list[dict[str, Any]],
    *,
    row_centers: list[float],
    col_centers: list[float],
    code_svg_center: dict[str, tuple[float,float]],
    cpi_center_by_cas: dict[int, tuple[float,float]],
    col_offset: int,
) -> None:
    # build control points from codes that exist in BOTH svg and html
    pairs_x: list[tuple[float,float]] = []
    pairs_y: list[tuple[float,float]] = []

    for it in items:
        code = it.get("code")
        if not code:
            continue
        if code not in code_svg_center:
            continue
        lay = it.get("_layout", {})
        hx = lay.get("x")
        hy = lay.get("y")
        if hx is None or hy is None:
            continue
        sx, sy = code_svg_center[code]
        pairs_x.append((sx, float(hx)))
        pairs_y.append((sy, float(hy)))

    fit_x = _linreg(pairs_x)
    fit_y = _linreg(pairs_y)
    if not fit_x or not fit_y:
        log.warning("No se pudo ajustar SVG->HTML; CPI se queda sin grid.")
        return

    ax, bx = fit_x
    ay, by = fit_y

    for it in items:
        if it.get("unit") != "CPI":
            continue
        cas = it.get("_casillero")
        if cas is None or cas not in cpi_center_by_cas:
            continue
        sx, sy = cpi_center_by_cas[cas]
        hx = ax * sx + bx
        hy = ay * sy + by

        row = _nearest_index(row_centers, hy) + 1
        col = _nearest_index(col_centers, hx) + 1 + col_offset
        it["grid"] = {"row": row, "col": col}


# ----------------- Main -----------------
async def main(career: str, version:int, concurrency: int, out_dir: str, x_threshold: float, y_threshold: float, col_offset: int):
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    svg_url = f"{SVG_BASE}/{career}v{version}.svg"
    svg_bytes = download_svg_bytes(svg_url)

    code_to_unit = extract_code_to_unit_from_svg(svg_bytes)
    code_to_credits = extract_code_to_credits_from_svg(svg_bytes)

    code_svg_center, cpi_center_by_cas, cpi_credits_by_cas, cpi_name_by_cas = extract_svg_centers(svg_bytes)

    # Build CPI items (internos: guardamos _casillero y _layout para poder ubicar, luego limpiamos)
    cpi_items: list[dict[str, Any]] = []
    for cas, (cx, cy) in sorted(cpi_center_by_cas.items(), key=lambda x: x[0]):
        name = cpi_name_by_cas.get(cas)
        if not name:
            continue
        credits = cpi_credits_by_cas.get(cas)
        cpi_items.append({
            "id": f"{career}:CPI:{cas}",
            "code": None,
            "name": name,
            "unit": "CPI",
            "credits": credits,
            "requirements": [],
            "approved_count_requirement": None,
            "grid": {"row": None, "col": None},
            "_casillero": cas,
            "_layout": {"x": None, "y": None, "w": None, "h": None},  # no usamos layout CPI aquí
        })

    timeout = httpx.Timeout(30.0)
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html, */*; q=0.01",
    }

    async with httpx.AsyncClient(timeout=timeout, limits=limits, headers=headers, follow_redirects=True) as client:
        positions = await fetch_positions(client, career)

        sem = asyncio.Semaphore(concurrency)

        async def one(p: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                frag = await fetch_requirements_html(client, p["mallaid"], p["posicion"], p["lang"])
                parsed = parse_requirements_fragment(frag)

                code = parsed.get("code")
                name = parsed.get("name")

                unit = code_to_unit.get(code) if code else None
                # fallback por tu regla (si no se detecta B/P/I)
                if code and not unit:
                    unit = "CPI"

                credits = code_to_credits.get(code) if code else None

                lay = p.get("layout") or {"x": None, "y": None, "w": None, "h": None}
                rec = {
                    "id": f"{career}:{code}" if code else f"{career}:UNKNOWN:{p['posicion']}",
                    "code": code,
                    "name": name,
                    "unit": unit,
                    "credits": credits,
                    "requirements": parsed.get("requirements", []),
                    "approved_count_requirement": parsed.get("approved_count_requirement"),
                    "grid": {"row": None, "col": None},
                    "_layout": lay,  # interno
                }
                return rec

        results: list[dict[str, Any]] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"Scrape requisitos ({career})", total=len(positions))
            tasks = [asyncio.create_task(one(p)) for p in positions]
            for coro in asyncio.as_completed(tasks):
                results.append(await coro)
                progress.advance(task)

    # Combina materias + CPI
    final = results + cpi_items

    # 1) Grid para materias con código (HTML)
    grid_meta = assign_grid_from_html(final, y_threshold=y_threshold, x_threshold=x_threshold, col_offset=col_offset)

    # 2) Ubicar CPI dentro del mismo grid (SVG -> HTML -> nearest centers)
    place_cpi_into_grid(
        final,
        row_centers=grid_meta["row_centers"],
        col_centers=grid_meta["col_centers"],
        code_svg_center=code_svg_center,
        cpi_center_by_cas=cpi_center_by_cas,
        col_offset=col_offset,
    )

    # Limpieza final: SIN _layout ni _casillero
    clean_final = []
    for it in final:
        g = it.get("grid") or {}
        clean_final.append({
            "id": it.get("id"),
            "code": it.get("code"),
            "name": it.get("name"),
            "unit": it.get("unit"),
            "credits": it.get("credits"),
            "grid": {"row": g.get("row"), "col": g.get("col")},
            "requirements": it.get("requirements", []),
            "approved_count_requirement": it.get("approved_count_requirement"),
        })

    out_path = Path(out_dir) / f"{career}_final.json"
    out_path.write_text(json.dumps(clean_final, ensure_ascii=False, indent=2), encoding="utf-8")

    # stats
    cpi_null = sum(1 for x in clean_final if x["unit"] == "CPI" and (x["grid"]["row"] is None or x["grid"]["col"] is None))
    log.info(f"Listo. Total={len(clean_final)} | CPI={sum(1 for x in clean_final if x['unit']=='CPI')} | CPI sin grid={cpi_null}")
    log.info(f"Guardado: {out_path}")
    log.info(f"SVG: {svg_url}")
    log.info(f"Grid detectado: filas={len(grid_meta['row_centers'])} cols={len(grid_meta['col_centers'])} col_offset={col_offset}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--career", default="CI013")
    ap.add_argument("--version", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--out", default="out")
    ap.add_argument("--x-threshold", type=float, default=60.0)
    ap.add_argument("--y-threshold", type=float, default=25.0)
    ap.add_argument("--col-offset", type=int, default=0)  # tú ya lo dejaste bien con +1 eliminado
    args = ap.parse_args()

    asyncio.run(main(
        career=args.career,
        version=args.version,
        concurrency=args.concurrency,
        out_dir=args.out,
        x_threshold=args.x_threshold,
        y_threshold=args.y_threshold,
        col_offset=args.col_offset,
    ))
