#!/usr/bin/env python3
"""
load_graded.py - Carga histórico de precios PSA 10 desde PriceCharting.com
en la tabla prices_graded de Supabase.

Fuente:  PriceCharting.com (campo "manualonly" del chart_data → PSA 10)
Escala:  valores en céntimos → ÷100 = USD
Grado:   PSA 10 (grade='10', grader='PSA')

Uso:
  python3 load_graded.py --test              # 10 cartas de referencia
  python3 load_graded.py --all               # todas las cartas de la DB
  python3 load_graded.py --id sv3-223        # carta específica
  python3 load_graded.py --all --dry-run     # sin insertar
  python3 load_graded.py --test --dry-run    # prueba de 5 cartas (dry)

Lanzamiento overnight:
  nohup python3 -u load_graded.py --all > graded_log.txt 2>&1 &
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
# CONFIG
# ─────────────────────────────────────────────
load_dotenv('/Users/estevewonder/pokemon-tracker/.env')
_raw = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_URL = _raw.removesuffix("/rest/v1")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not all([SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Faltan credenciales Supabase en .env")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PC_BASE     = "https://www.pricecharting.com"
RATE_LIMIT  = 1.2
TIMEOUT     = 20
PSA10_FIELD = "manualonly"   # PSA 10 en PriceCharting chart_data
GRADE       = "10"
GRADER      = "PSA"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SET_SLUG_OVERRIDES = {
    "Base":                 "base-set",
    "Base Set 2":           "base-set-2",
    "Legendary Collection": "legendary-collection",
    "e-Card":               "skyridge",
}

# 10 cartas de referencia (IDs corregidos tras validación)
TEST_CARD_IDS = [
    "base1-4",     # Charizard — Base Set
    "swsh7-215",   # Umbreon VMAX — Evolving Skies
    "swsh7-218",   # Rayquaza VMAX — Evolving Skies
    "swsh35-74",   # Charizard VMAX Rainbow — Champion's Path
    "sm3-150",     # Charizard-GX Rainbow — Burning Shadows
    "sv8-238",     # Pikachu ex SIR — Surging Sparks
    "sv8pt5-161",  # Umbreon ex SIR — Prismatic Evolutions
    "sv4-251",     # Roaring Moon ex SIR — Paradox Rift
    "sv3-223",     # Charizard ex SIR — Obsidian Flames
    "base1-2",     # Blastoise — Base Set
]

# Primeras 5 para dry-run rápido
DRY_RUN_CARD_IDS = TEST_CARD_IDS[:5]


# ─────────────────────────────────────────────
# SLUGS
# ─────────────────────────────────────────────

def to_pc_slug(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[''&]", "", s)
    s = re.sub(r"-", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", "-", s.strip())
    return re.sub(r"-+", "-", s)


def get_pc_url(set_name: str, card_name: str, number: str) -> str:
    set_slug  = SET_SLUG_OVERRIDES.get(set_name) or to_pc_slug(set_name)
    card_slug = to_pc_slug(card_name)
    return f"{PC_BASE}/game/pokemon-{set_slug}/{card_slug}-{number}"


def search_pc(card_name: str, set_name: str, number: str) -> str | None:
    q   = f"{card_name} {set_name} {number}"
    url = f"{PC_BASE}/search-products?q={requests.utils.quote(q)}&type=prices"
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/game/pokemon" in href and href.endswith(f"-{number}"):
                return PC_BASE + href if href.startswith("/") else href
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────

def fetch_chart_data(url: str) -> tuple[dict, str]:
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return {}, url
        m = re.search(r'chart_data\s*=\s*(\{.*?\})\s*;?\s*\n', resp.text, re.S)
        if not m:
            return {}, url
        raw = m.group(1)
        return json.loads(raw[:raw.rfind("}") + 1]), url
    except Exception:
        return {}, url


def extract_psa10(chart_data: dict) -> list[dict]:
    rows = []
    for ts_ms, price_cents in chart_data.get(PSA10_FIELD, []):
        if price_cents == 0:
            continue
        dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).date()
        rows.append({"date": str(dt), "price": round(price_cents / 100, 2)})
    return rows


# ─────────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────────

def get_cards(card_ids: list[str] | None = None) -> list[dict]:
    rows, offset, psize = [], 0, 1000
    while True:
        q = supabase.table("cards").select("id,name,collector_number,set_id") \
            .range(offset, offset + psize - 1)
        if card_ids:
            q = q.in_("id", card_ids)
        r = q.execute()
        rows.extend(r.data)
        if len(r.data) < psize:
            break
        offset += psize
    return rows


def get_set_names(set_ids: list[str]) -> dict[str, str]:
    r = supabase.table("sets").select("id,name").in_("id", set_ids).execute()
    return {row["id"]: row["name"] for row in r.data}


def upsert_graded(card_id: str, prices: list[dict], dry_run: bool = False) -> int:
    if not prices or dry_run:
        return len(prices) if dry_run else 0
    rows = [
        {
            "card_id": card_id,
            "date":    p["date"],
            "grade":   GRADE,
            "grader":  GRADER,
            "price":   p["price"],
            "source":  "pricecharting",
        }
        for p in prices
    ]
    inserted = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        try:
            supabase.table("prices_graded") \
                .upsert(chunk, on_conflict="card_id,date,grade,grader").execute()
            inserted += len(chunk)
        except Exception as exc:
            print(f"    WARN: error guardando chunk: {exc}")
    return inserted


# ─────────────────────────────────────────────
# LÓGICA POR CARTA
# ─────────────────────────────────────────────

def process_card(card: dict, set_names: dict, dry_run: bool = False) -> dict:
    card_id  = card["id"]
    name     = card["name"]
    number   = card["collector_number"]
    set_name = set_names.get(card["set_id"], card["set_id"])

    result = {
        "card_id":  card_id,
        "name":     name,
        "set":      set_name,
        "status":   "not_found",
        "pts":      0,
        "inserted": 0,
        "d_min":    None,
        "d_max":    None,
        "p_start":  None,
        "p_now":    None,
    }

    url = get_pc_url(set_name, name, number)
    chart, used_url = fetch_chart_data(url)

    if not chart:
        found = search_pc(name, set_name, number)
        if found:
            time.sleep(RATE_LIMIT)
            chart, used_url = fetch_chart_data(found)

    if not chart:
        result["status"] = "not_found"
        return result

    prices = extract_psa10(chart)
    if not prices:
        result["status"] = "no_psa10"
        return result

    result["pts"]    = len(prices)
    result["d_min"]  = prices[0]["date"]
    result["d_max"]  = prices[-1]["date"]
    result["p_start"] = prices[0]["price"]
    result["p_now"]   = prices[-1]["price"]

    inserted = upsert_graded(card_id, prices, dry_run)
    result["inserted"] = inserted
    result["status"]   = "ok"
    return result


# ─────────────────────────────────────────────
# MODOS
# ─────────────────────────────────────────────

def run_cards(card_ids: list[str] | None, dry_run: bool, label: str):
    print(f"\n[1/3] Cargando cartas desde Supabase...")
    cards = get_cards(card_ids)
    if not cards:
        print("  ERROR: no se encontraron las cartas.")
        sys.exit(1)
    print(f"  {len(cards)} cartas a procesar")

    set_ids   = list({c["set_id"] for c in cards})
    set_names = get_set_names(set_ids)

    print(f"\n[2/3] Extrayendo PSA 10 desde PriceCharting{'  (DRY-RUN)' if dry_run else ''}...")
    print("-" * 68)

    results   = []
    total_pts = 0
    total_ins = 0

    for i, card in enumerate(cards, 1):
        set_name = set_names.get(card["set_id"], card["set_id"])
        print(f"\n  [{i:>3}/{len(cards)}] {card['id']:<20}  {card['name']} #{card['collector_number']}")
        print(f"          Set: {set_name}")

        res = process_card(card, set_names, dry_run)
        results.append(res)

        if res["status"] == "ok":
            total_pts += res["pts"]
            total_ins += res["inserted"]
            roi = (res["p_now"] - res["p_start"]) / res["p_start"] * 100
            suffix = "DRY-RUN" if dry_run else f"{res['inserted']} filas insertadas"
            print(f"          ✓  {res['pts']} pts  |  {res['d_min']} → {res['d_max']}")
            print(f"             PSA 10: ${res['p_start']:.0f} → ${res['p_now']:.0f} ({roi:+.0f}%)")
            print(f"             {suffix}")
        elif res["status"] == "no_psa10":
            print(f"          ✗  Encontrada pero sin datos PSA 10")
        else:
            print(f"          ✗  No encontrada en PriceCharting")

        time.sleep(RATE_LIMIT)

    # Resumen
    ok     = [r for r in results if r["status"] == "ok"]
    no_psa = [r for r in results if r["status"] == "no_psa10"]
    bad    = [r for r in results if r["status"] == "not_found"]

    print("\n" + "=" * 68)
    print(f" COMPLETADO {'(DRY-RUN)' if dry_run else ''}")
    print("=" * 68)
    print(f"  Cartas procesadas:        {len(cards)}")
    print(f"  Con datos PSA 10:         {len(ok)}")
    print(f"  Sin datos PSA 10:         {len(no_psa)}")
    print(f"  No encontradas en PC:     {len(bad)}")
    print(f"  Puntos históricos totales:{total_pts}")
    print(f"  Filas insertadas:         {total_ins}")

    if ok:
        print(f"\n  {'CARTA':<22} {'PTS':>4}  {'INICIO':>8}  {'AHORA':>8}  {'ROI':>6}")
        print(f"  {'─'*22} {'─'*4}  {'─'*8}  {'─'*8}  {'─'*6}")
        for r in ok:
            roi = (r["p_now"] - r["p_start"]) / r["p_start"] * 100
            print(f"  {r['card_id']:<22} {r['pts']:>4}  ${r['p_start']:>7.0f}  ${r['p_now']:>7.0f}  {roi:>+5.0f}%")
    if bad:
        print(f"\n  No encontradas ({len(bad)}):")
        for r in bad:
            print(f"    {r['card_id']:<20} {r['name']} ({r['set']})")
    print("=" * 68)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Carga histórico PSA 10 desde PriceCharting")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--test",  action="store_true", help="10 cartas de referencia")
    mode.add_argument("--all",   action="store_true", help="Todas las cartas de la DB")
    mode.add_argument("--id",    type=str,            help="Carta específica (ej: sv3-223)")
    parser.add_argument("--dry-run", action="store_true", help="Mostrar sin insertar")
    args = parser.parse_args()

    dr = args.dry_run

    print("=" * 68)
    print(f" Pokémon TCG Tracker — Histórico PSA 10 (PriceCharting)")
    if dr: print(" *** DRY-RUN: no se insertará nada ***")
    print("=" * 68)

    if args.id:
        run_cards([args.id], dr, label="single")
    elif args.test:
        # dry-run usa las 5 primeras; ejecución real usa las 10
        ids = DRY_RUN_CARD_IDS if dr else TEST_CARD_IDS
        print(f"\n[TEST{'  DRY-RUN' if dr else ''}] {len(ids)} cartas de referencia")
        run_cards(ids, dr, label="test")
    elif args.all:
        run_cards(None, dr, label="all")


if __name__ == "__main__":
    main()
