#!/usr/bin/env python3
"""
load_sealed.py - Carga productos sellados (booster boxes, ETBs, bundles...)
y su histórico de precios desde PriceCharting.com en Supabase.

Uso:
  python3 load_sealed.py --test            # 5 booster boxes de prueba
  python3 load_sealed.py --set sv3         # todos los productos de un set
  python3 load_sealed.py --all             # todos los sets de la DB
  python3 load_sealed.py --all --dry-run   # solo muestra, no inserta

Estrategia de descubrimiento por set:
  1. Consulta la página de consola de PriceCharting (/console/pokemon-{slug})
     si existe, extrae todos los productos sellados.
  2. Si no existe, prueba slugs estándar: booster-box, elite-trainer-box, etc.
"""

import subprocess, sys

def _try(mod):
    try: __import__(mod); return True
    except ImportError: return False

def ensure_deps():
    deps = {"python-dotenv": "dotenv", "requests": "requests",
            "supabase": "supabase", "beautifulsoup4": "bs4"}
    missing = [p for p, m in deps.items() if not _try(m)]
    if missing:
        for p in missing:
            subprocess.check_call([sys.executable, "-m", "pip", "install", p, "-q"])

ensure_deps()

import os, re, json, time, datetime, argparse
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
load_dotenv('/Users/estevewonder/pokemon-tracker/.env')
_raw = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_URL = _raw.removesuffix("/rest/v1")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PC_BASE    = "https://www.pricecharting.com"
RATE_LIMIT = 1.2      # segundos entre requests
TIMEOUT    = 20
HEADERS    = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Precio a usar como market: "used" tiene más datos (eBay sold average)
# "new" = precio de producto sellado/mint si existe
MARKET_FIELD = "used"
HIGH_FIELD   = "new"    # para price_high si tiene datos

# ─────────────────────────────────────────────
# MAPEOS DE SLUGS
# ─────────────────────────────────────────────

# Overrides: nombre de set (pokemontcg.io) → slug PriceCharting completo
PC_SET_SLUG_OVERRIDES = {
    "Base":                  "pokemon-base-set",
    "Base Set 2":            "pokemon-base-set-2",
    "Legendary Collection":  "pokemon-legendary-collection",
    "e-Card Expedition":     "pokemon-expedition-base-set",
    "Aquapolis":             "pokemon-aquapolis",
    "Skyridge":              "pokemon-skyridge",
    "Sun & Moon":            "pokemon-sun-&-moon",       # & literal
    "Sword & Shield":        "pokemon-sword-&-shield",
    "Scarlet & Violet":      "pokemon-scarlet-&-violet",
}

# Overrides específicos: (set_id, product_type) → slug del producto
# Cuando el slug estándar "booster-box" no funciona para ese set
PC_PRODUCT_OVERRIDES = {
    # SV: el set base llama al box "base-set-booster-box"
    ("sv1",  "booster_box"): "base-set-booster-box",
    ("sv2",  "booster_box"): "booster-box",
    # SWSH: el set base llama al box "booster-box-base-set"
    ("swsh1","booster_box"): "booster-box-base-set",
    # SM: el set base usa prefijo sm-
    ("sm1",  "booster_box"): "booster-box",  # funciona directo
}

# Tipos permitidos — solo estos 4 entran en la DB
ALLOWED_TYPES = {"booster_box", "etb", "booster_bundle", "sleeved_booster"}

# Tipos de productos a buscar en cada set (slug tentativo → product_type)
PRODUCT_ATTEMPTS = [
    # booster boxes (varios nombres posibles)
    ("booster-box",               "booster_box"),
    ("base-set-booster-box",      "booster_box"),
    ("booster-box-base-set",      "booster_box"),
    # ETBs
    ("elite-trainer-box",         "etb"),
    ("elite-trainer-box-base-set","etb"),
    # Bundles
    ("booster-bundle",            "booster_bundle"),
    ("booster-bundle-3-pack",     "booster_bundle"),
    # Sleeved boosters
    ("sleeved-booster-pack",      "sleeved_booster"),
]

# Slugs a excluir del descubrimiento automático
SLUG_EXCLUSIONS = {
    "booster-pack",
    "code-card",
    "online-code",
    "promo-card",
}

# 5 booster boxes de prueba (test)
TEST_PRODUCTS = [
    {
        "id":           "sv1-booster-box",
        "set_id":       "sv1",
        "name":         "Scarlet & Violet Base Set Booster Box",
        "product_type": "booster_box",
        "pc_url":       f"{PC_BASE}/game/pokemon-scarlet-&-violet/base-set-booster-box",
    },
    {
        "id":           "sv3-booster-box",
        "set_id":       "sv3",
        "name":         "Obsidian Flames Booster Box",
        "product_type": "booster_box",
        "pc_url":       f"{PC_BASE}/game/pokemon-obsidian-flames/booster-box",
    },
    {
        "id":           "swsh1-booster-box",
        "set_id":       "swsh1",
        "name":         "Sword & Shield Base Set Booster Box",
        "product_type": "booster_box",
        "pc_url":       f"{PC_BASE}/game/pokemon-sword-&-shield/booster-box-base-set",
    },
    {
        "id":           "sm1-booster-box",
        "set_id":       "sm1",
        "name":         "Sun & Moon Base Set Booster Box",
        "product_type": "booster_box",
        "pc_url":       f"{PC_BASE}/game/pokemon-sun-&-moon/booster-box",
    },
    {
        "id":           "swsh7-booster-box",
        "set_id":       "swsh7",
        "name":         "Evolving Skies Booster Box",
        "product_type": "booster_box",
        "pc_url":       f"{PC_BASE}/game/pokemon-evolving-skies/booster-box",
    },
]


# ─────────────────────────────────────────────
# CONSTRUCCIÓN DE SLUGS
# ─────────────────────────────────────────────

def to_pc_set_slug(set_name: str) -> str:
    """Nombre de set → slug PriceCharting. Ej: 'Obsidian Flames' → 'pokemon-obsidian-flames'."""
    if set_name in PC_SET_SLUG_OVERRIDES:
        return PC_SET_SLUG_OVERRIDES[set_name]
    s = set_name.lower()
    s = re.sub(r"'", "", s)             # quitar apóstrofes
    s = re.sub(r"[^a-z0-9&\s-]", " ", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return f"pokemon-{s}"


def product_slug_to_type(slug: str) -> str:
    """Slug de producto → tipo normalizado. Devuelve 'other' si no es un tipo permitido."""
    for attempt_slug, ptype in PRODUCT_ATTEMPTS:
        if attempt_slug == slug:
            return ptype
    # Heurística: buscar palabras clave
    if "booster-box" in slug:    return "booster_box"
    if "elite-trainer" in slug:  return "etb"
    if "bundle" in slug:         return "booster_bundle"
    if "sleeved" in slug:        return "sleeved_booster"
    return "other"  # tin, collection_box, special_collection → excluidos


def make_product_id(set_id: str, pc_slug: str, ptype: str) -> str:
    """Genera un ID único para el producto: ej. 'sv3-booster-box'."""
    # Usar el slug de PC limpio como sufijo
    slug_clean = re.sub(r"[^a-z0-9-]", "", pc_slug)
    slug_clean = re.sub(r"-+", "-", slug_clean).strip("-")
    return f"{set_id}-{slug_clean}"


# ─────────────────────────────────────────────
# SCRAPING PRICECHARTING
# ─────────────────────────────────────────────

def fetch_chart_data(url: str) -> dict:
    """Descarga la página y extrae el dict chart_data."""
    try:
        r = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200 or "chart_data" not in r.text:
            return {}
        # Evitar páginas de búsqueda que redireccionaron
        if "search-products" in r.url:
            return {}
        m = re.search(r'chart_data\s*=\s*(\{.*?\})\s*;?\s*\n', r.text, re.S)
        if not m:
            return {}
        raw  = m.group(1)
        data = json.loads(raw[:raw.rfind("}") + 1])
        return data
    except Exception:
        return {}


def extract_price_rows(chart_data: dict, product_id: str) -> list[dict]:
    """Extrae filas para prices_sealed desde chart_data."""
    market_series = chart_data.get(MARKET_FIELD, [])
    high_series   = {ts: pr for ts, pr in chart_data.get(HIGH_FIELD, []) if pr > 0}
    rows = []
    for ts_ms, price_cents in market_series:
        if price_cents == 0:
            continue
        dt         = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).date()
        market_usd = round(price_cents / 100, 2)
        high_usd   = round(high_series.get(ts_ms, 0) / 100, 2) or None
        rows.append({
            "product_id":   product_id,
            "date":         str(dt),
            "price_market": market_usd,
            "price_low":    None,
            "price_high":   high_usd,
            "source":       "pricecharting",
        })
    return rows


def discover_products_via_console(set_slug: str, set_id: str, set_name: str) -> list[dict]:
    """
    Consulta la página de consola de PriceCharting para el set.
    Devuelve lista de productos candidatos con sus URLs.
    """
    console_url = f"{PC_BASE}/console/{set_slug}"
    try:
        r = SESSION.get(console_url, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        time.sleep(RATE_LIMIT)
    except Exception:
        return []

    soup  = BeautifulSoup(r.text, "html.parser")
    found = []
    seen  = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Filtrar: solo links de producto del mismo set
        if not re.match(rf"/game/{re.escape(set_slug)}/", href):
            continue
        prod_slug = href.split("/")[-1]
        if prod_slug in SLUG_EXCLUSIONS or prod_slug in seen:
            continue
        # Excluir slugs que terminan en número de carta (ej. victini-ex-33, dratini-157)
        if re.search(r'-\d+$', prod_slug):
            continue
        # Clasificar y filtrar por tipos permitidos
        ptype = product_slug_to_type(prod_slug)
        if ptype not in ALLOWED_TYPES:
            continue
        seen.add(prod_slug)
        pid = make_product_id(set_id, prod_slug, ptype)
        label = a.get_text(strip=True) or prod_slug.replace("-", " ").title()
        found.append({
            "id":           pid,
            "set_id":       set_id,
            "name":         f"{set_name} {label}",
            "product_type": ptype,
            "pc_url":       PC_BASE + href,
            "pc_slug":      prod_slug,
        })
    return found


def discover_products_via_slugs(set_slug: str, set_id: str, set_name: str) -> list[dict]:
    """
    Intenta slugs estándar cuando la página de consola no existe.
    Solo devuelve productos con chart_data real.
    """
    found = []
    seen_types = set()

    # Overrides primero
    for (sid, ptype), prod_slug in PC_PRODUCT_OVERRIDES.items():
        if sid != set_id:
            continue
        if ptype in seen_types:
            continue
        url = f"{PC_BASE}/game/{set_slug}/{prod_slug}"
        data = fetch_chart_data(url)
        time.sleep(RATE_LIMIT)
        if data:
            pid   = make_product_id(set_id, prod_slug, ptype)
            label = prod_slug.replace("-", " ").title()
            found.append({
                "id":           pid,
                "set_id":       set_id,
                "name":         f"{set_name} {label}",
                "product_type": ptype,
                "pc_url":       url,
                "pc_slug":      prod_slug,
            })
            seen_types.add(ptype)

    # Intentos estándar
    for prod_slug, ptype in PRODUCT_ATTEMPTS:
        if ptype in seen_types:
            continue
        url  = f"{PC_BASE}/game/{set_slug}/{prod_slug}"
        data = fetch_chart_data(url)
        time.sleep(RATE_LIMIT)
        if data:
            pid   = make_product_id(set_id, prod_slug, ptype)
            label = prod_slug.replace("-", " ").title()
            found.append({
                "id":           pid,
                "set_id":       set_id,
                "name":         f"{set_name} {label}",
                "product_type": ptype,
                "pc_url":       url,
                "pc_slug":      prod_slug,
            })
            seen_types.add(ptype)

    return found


# ─────────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────────

def get_all_sets() -> list[dict]:
    rows, offset, psize = [], 0, 500
    while True:
        r = supabase.table("sets").select("id,name,release_date") \
            .range(offset, offset + psize - 1).execute()
        rows.extend(r.data)
        if len(r.data) < psize:
            break
        offset += psize
    return rows


def upsert_product(product: dict, dry_run: bool = False) -> bool:
    row = {
        "id":           product["id"],
        "set_id":       product["set_id"],
        "name":         product["name"],
        "product_type": product["product_type"],
    }
    if dry_run:
        return True
    try:
        supabase.table("sealed_products").upsert(row).execute()
        return True
    except Exception as exc:
        print(f"    WARN sealed_products: {exc}")
        return False


def upsert_prices(rows: list[dict], dry_run: bool = False) -> int:
    if not rows or dry_run:
        return len(rows) if dry_run else 0
    inserted = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        try:
            supabase.table("prices_sealed") \
                .upsert(chunk, on_conflict="product_id,date").execute()
            inserted += len(chunk)
        except Exception as exc:
            print(f"    WARN prices_sealed chunk: {exc}")
    return inserted


# ─────────────────────────────────────────────
# PROCESAMIENTO DE UN PRODUCTO
# ─────────────────────────────────────────────

def process_product(product: dict, dry_run: bool = False) -> dict:
    """Fetch → extract → insert. Devuelve resumen."""
    result = {
        "id":     product["id"],
        "name":   product["name"],
        "status": "no_data",
        "pts":    0,
        "ins":    0,
        "d_min":  None,
        "d_max":  None,
        "p_start": None,
        "p_now":   None,
    }

    chart = fetch_chart_data(product["pc_url"])
    if not chart:
        result["status"] = "not_found"
        return result

    price_rows = extract_price_rows(chart, product["id"])
    if not price_rows:
        result["status"] = "no_prices"
        return result

    result["pts"]   = len(price_rows)
    result["d_min"] = price_rows[0]["date"]
    result["d_max"] = price_rows[-1]["date"]
    result["p_start"] = price_rows[0]["price_market"]
    result["p_now"]   = price_rows[-1]["price_market"]

    ok = upsert_product(product, dry_run)
    if not ok:
        result["status"] = "db_error"
        return result

    ins = upsert_prices(price_rows, dry_run)
    result["ins"]    = ins
    result["status"] = "ok"
    return result


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run_test(dry_run: bool = False):
    """Modo --test: 5 booster boxes hardcoded."""
    print(f"\n[TEST] 5 booster boxes de referencia\n{'─'*66}")
    results = []
    for prod in TEST_PRODUCTS:
        print(f"  {prod['id']}")
        print(f"    URL: {prod['pc_url'].split('pricecharting.com')[1]}")
        res = process_product(prod, dry_run)
        results.append(res)
        suffix = "DRY-RUN" if dry_run else f"{res['ins']} filas"
        if res["status"] == "ok":
            roi = ((res["p_now"] - res["p_start"]) / res["p_start"] * 100) if res["p_start"] else 0
            print(f"    ✓  {res['pts']} pts | {res['d_min']} → {res['d_max']} | "
                  f"${res['p_start']:.0f}→${res['p_now']:.0f} ({roi:+.0f}%) | {suffix}")
        else:
            print(f"    ✗  {res['status']}")
        time.sleep(RATE_LIMIT)
    return results


def run_set(set_id: str, dry_run: bool = False):
    """Modo --set: todos los productos de un set concreto."""
    sets = {s["id"]: s for s in get_all_sets()}
    if set_id not in sets:
        print(f"ERROR: set_id '{set_id}' no encontrado en la DB.")
        return []

    s        = sets[set_id]
    set_name = s["name"]
    set_slug = to_pc_set_slug(set_name)

    print(f"\n[SET] {set_id} — {set_name}")
    print(f"  PC slug: {set_slug}")

    # Intentar consola primero
    products = discover_products_via_console(set_slug, set_id, set_name)
    if products:
        print(f"  Descubiertos via consola: {len(products)} productos")
    else:
        print(f"  Consola no disponible → probando slugs estándar...")
        products = discover_products_via_slugs(set_slug, set_id, set_name)
        print(f"  Encontrados: {len(products)} productos")

    results = []
    for prod in products:
        print(f"\n  {prod['id']} ({prod['product_type']})")
        res = process_product(prod, dry_run)
        results.append(res)
        suffix = "DRY-RUN" if dry_run else f"{res['ins']} filas"
        if res["status"] == "ok":
            print(f"    ✓  {res['pts']} pts | {res['d_min']} → {res['d_max']} | {suffix}")
        else:
            print(f"    ✗  {res['status']}")
        time.sleep(RATE_LIMIT)
    return results


def run_all(dry_run: bool = False):
    """Modo --all: todos los sets de la DB."""
    all_sets = get_all_sets()
    print(f"\n[ALL] {len(all_sets)} sets a procesar")
    print(f"{'─'*66}")

    total_prod  = 0
    total_pts   = 0
    total_ins   = 0
    failed_sets = []

    for i, s in enumerate(all_sets, 1):
        set_id   = s["id"]
        set_name = s["name"]
        set_slug = to_pc_set_slug(set_name)

        print(f"\n[{i:>3}/{len(all_sets)}] {set_id:<12} {set_name}")

        # Consola primero
        products = discover_products_via_console(set_slug, set_id, set_name)
        if not products:
            products = discover_products_via_slugs(set_slug, set_id, set_name)

        if not products:
            print(f"  → Sin productos sellados en PriceCharting")
            failed_sets.append(set_id)
            continue

        print(f"  → {len(products)} productos encontrados")
        set_pts = 0
        set_ins = 0
        for prod in products:
            res = process_product(prod, dry_run)
            if res["status"] == "ok":
                total_prod += 1
                set_pts += res["pts"]
                set_ins += res["ins"]
                suffix = "DRY-RUN" if dry_run else f"{res['ins']} filas"
                roi = ((res["p_now"] - res["p_start"]) / res["p_start"] * 100) if res["p_start"] else 0
                print(f"    ✓ {prod['id']:<30} {res['pts']:>3} pts  "
                      f"${res['p_start']:.0f}→${res['p_now']:.0f} ({roi:+.0f}%)  {suffix}")
            else:
                print(f"    ✗ {prod['id']:<30} {res['status']}")
            time.sleep(RATE_LIMIT)

        total_pts += set_pts
        total_ins += set_ins

    # Resumen final
    print(f"\n{'='*66}")
    print(f" COMPLETADO {'(DRY-RUN)' if dry_run else ''}")
    print(f"{'='*66}")
    print(f"  Sets procesados:    {len(all_sets)}")
    print(f"  Productos con datos:{total_prod}")
    print(f"  Puntos históricos:  {total_pts}")
    print(f"  Filas insertadas:   {total_ins}")
    print(f"  Sets sin datos:     {len(failed_sets)}")
    if failed_sets:
        print(f"  ({', '.join(failed_sets[:20])}{'...' if len(failed_sets)>20 else ''})")


def print_summary(results: list[dict], dry_run: bool):
    ok  = [r for r in results if r["status"] == "ok"]
    bad = [r for r in results if r["status"] != "ok"]
    print(f"\n{'='*66}")
    print(f" RESUMEN {'(DRY-RUN)' if dry_run else ''}")
    print(f"{'='*66}")
    print(f"  Con datos:    {len(ok)}")
    print(f"  Sin datos:    {len(bad)}")
    print(f"  Pts totales:  {sum(r['pts'] for r in ok)}")
    print(f"  Filas ins.:   {sum(r['ins'] for r in ok)}")
    if ok:
        print(f"\n  {'PRODUCTO':<35} {'PTS':>4}  {'INICIO':>8}  {'AHORA':>8}  {'ROI':>6}")
        print(f"  {'─'*35} {'─'*4}  {'─'*8}  {'─'*8}  {'─'*6}")
        for r in ok:
            roi = ((r["p_now"] - r["p_start"]) / r["p_start"] * 100) if r["p_start"] else 0
            print(f"  {r['name'][:35]:<35} {r['pts']:>4}"
                  f"  ${r['p_start']:>7.0f}  ${r['p_now']:>7.0f}  {roi:>+5.0f}%")
    print("=" * 66)


def main():
    parser = argparse.ArgumentParser(description="Carga sellados desde PriceCharting")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test",    action="store_true",  help="5 booster boxes de prueba")
    mode.add_argument("--set",     type=str, metavar="ID", help="Set específico (ej: sv3)")
    mode.add_argument("--all",     action="store_true",  help="Todos los sets")
    parser.add_argument("--dry-run", action="store_true", help="Solo mostrar, no insertar")
    args = parser.parse_args()

    dr = args.dry_run
    print("=" * 66)
    print(f" Pokémon TCG — Sellados histórico (PriceCharting)")
    if dr: print(" *** DRY-RUN: no se insertará nada ***")
    print("=" * 66)

    if args.test:
        results = run_test(dr)
        print_summary(results, dr)
    elif args.set:
        results = run_set(args.set, dr)
        print_summary(results, dr)
    elif args.all:
        run_all(dr)


if __name__ == "__main__":
    main()
