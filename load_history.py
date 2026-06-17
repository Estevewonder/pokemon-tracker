#!/usr/bin/env python3
"""
load_history.py - Carga histórico de precios desde PriceCharting.com
para cartas de nuestra base de datos. Datos mensuales, hasta 3 años atrás.
Seguro re-ejecutar (upsert por card_id + date).

Fuente: PriceCharting.com (sin Cloudflare, acceso directo)
Campo:  "used" = precio raw/sin graduar (más cercano a TCGPlayer market)
Escala: los valores en HTML son céntimos → dividir /100 para USD

Uso:
  python3 load_history.py --test          # solo las 10 cartas de prueba
  python3 load_history.py --all           # todas las cartas de la DB (lento)
  python3 load_history.py --id sv3-223    # una carta específica
"""

import subprocess
import sys

def _try_import(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False

def ensure_deps():
    deps = {"python-dotenv": "dotenv", "requests": "requests",
            "supabase": "supabase", "beautifulsoup4": "bs4"}
    missing = [pkg for pkg, mod in deps.items() if not _try_import(mod)]
    if missing:
        print(f"Instalando: {', '.join(missing)}")
        for pkg in missing:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])
        print()

ensure_deps()

import os
import re
import json
import time
import datetime
import argparse
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
load_dotenv('/Users/estevewonder/pokemon-tracker/.env')

_raw_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_URL = _raw_url.removesuffix("/rest/v1")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not all([SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Faltan credenciales Supabase en .env")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PC_BASE    = "https://www.pricecharting.com"
RATE_LIMIT = 1.0     # segundos entre requests (respetar el servidor)
TIMEOUT    = 20
PRICE_FIELD = "used"  # raw/ungraded price (closest to TCGPlayer market)
# Alternativas: "cib" (LP-NM), "new" (NM), "boxonly" (NM-Mint top)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Overrides para sets cuyos slugs difieren del nombre simple
SET_SLUG_OVERRIDES = {
    "Base": "base-set",
    "Base Set 2": "base-set-2",
    "Legendary Collection": "legendary-collection",
    "e-Card": "skyridge",  # PriceCharting usa los nombres individuales
}

# 10 cartas de prueba representativas de distintas eras
TEST_CARD_IDS = [
    "sv3-223",     # Charizard ex Special Illustration Rare — Obsidian Flames
    "sv8pt5-161",  # Umbreon ex Special Illustration Rare — Prismatic Evolutions
    "sv8-238",     # Pikachu ex Special Illustration Rare — Surging Sparks
    "swsh35-74",   # Charizard VMAX Rainbow Rare — Champion's Path
    "swsh7-215",   # Umbreon VMAX Rainbow — Evolving Skies
    "swsh7-218",   # Rayquaza VMAX Rainbow — Evolving Skies
    "sm3-150",     # Charizard-GX Rainbow Rare — Burning Shadows
    "base1-4",     # Charizard Holo — Base Set
    "base1-2",     # Blastoise Holo — Base Set
    "sv4-251",     # Roaring Moon ex Special Illustration Rare — Paradox Rift
]


# ─────────────────────────────────────────────
# URL CONSTRUCTION & SEARCH
# ─────────────────────────────────────────────

def to_pc_slug(text: str) -> str:
    """Convierte un nombre a slug de PriceCharting."""
    s = text.lower()
    s = re.sub(r"[''&]", "", s)
    s = re.sub(r"-", " ", s)              # guiones → espacio (para re-hifenar limpio)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s


def get_pc_url(set_name: str, card_name: str, number: str) -> str:
    """Construye la URL directa de PriceCharting para una carta."""
    set_slug  = SET_SLUG_OVERRIDES.get(set_name) or to_pc_slug(set_name)
    card_slug = to_pc_slug(card_name)
    return f"{PC_BASE}/game/pokemon-{set_slug}/{card_slug}-{number}"


def search_pc(card_name: str, set_name: str, number: str) -> str | None:
    """
    Busca en PriceCharting cuando la URL directa no funciona.
    Devuelve la primera URL de producto que coincida con el número de carta.
    """
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
        return None
    except Exception:
        return None


def fetch_chart_data(url: str) -> tuple[dict, str]:
    """
    Descarga la página de producto y extrae chart_data.
    Devuelve (chart_dict, url_usado).
    """
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return {}, url
        m = re.search(r'chart_data\s*=\s*(\{.*?\})\s*;?\s*\n', resp.text, re.S)
        if not m:
            return {}, url
        raw = m.group(1)
        # El JSON termina al cierre del objeto principal
        data = json.loads(raw[:raw.rfind("}") + 1])
        return data, url
    except Exception:
        return {}, url


# ─────────────────────────────────────────────
# DATA EXTRACTION
# ─────────────────────────────────────────────

def extract_prices(chart_data: dict, field: str = PRICE_FIELD) -> list[dict]:
    """
    Extrae pares (date, market_usd) del campo indicado de chart_data.
    Precios en la fuente son céntimos → ÷ 100 = USD.
    Filtra valores 0 (sin datos).
    """
    series = chart_data.get(field, [])
    rows   = []
    for ts_ms, price_cents in series:
        if price_cents == 0:
            continue
        dt  = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=datetime.timezone.utc).date()
        rows.append({"date": str(dt), "market_usd": round(price_cents / 100, 2)})
    return rows


# ─────────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────────

def get_cards(card_ids: list[str] | None = None) -> list[dict]:
    """
    Devuelve cartas desde Supabase con nombre y set.
    Si card_ids es None, devuelve todas.
    """
    rows   = []
    offset = 0
    psize  = 1000
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
    """Devuelve {set_id: name}."""
    r = supabase.table("sets").select("id,name").in_("id", set_ids).execute()
    return {row["id"]: row["name"] for row in r.data}


def upsert_prices(card_id: str, prices: list[dict]) -> int:
    """Inserta precios históricos en chunks de 200. Devuelve cuántos insertó."""
    if not prices:
        return 0
    rows = [{"card_id": card_id, "date": p["date"], "price_market": p["market_usd"]}
            for p in prices]
    inserted = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        try:
            supabase.table("prices_en") \
                .upsert(chunk, on_conflict="card_id,date").execute()
            inserted += len(chunk)
        except Exception as exc:
            print(f"    WARN: error guardando chunk: {exc}")
    return inserted


# ─────────────────────────────────────────────
# MAIN LOGIC PER CARD
# ─────────────────────────────────────────────

def process_card(card: dict, set_names: dict, verbose: bool = True) -> dict:
    """
    Procesa una carta: busca en PC, extrae histórico, inserta en Supabase.
    Devuelve resumen del resultado.
    """
    card_id    = card["id"]
    card_name  = card["name"]
    number     = card["collector_number"]
    set_id     = card["set_id"]
    set_name   = set_names.get(set_id, set_id)

    result = {
        "card_id":  card_id,
        "name":     card_name,
        "set":      set_name,
        "status":   "not_found",
        "url":      None,
        "points":   0,
        "inserted": 0,
        "date_min": None,
        "date_max": None,
        "samples":  [],
    }

    # 1. Intentar URL directa
    url = get_pc_url(set_name, card_name, number)
    chart, used_url = fetch_chart_data(url)

    # 2. Si no hay chart_data, intentar búsqueda
    if not chart:
        if verbose:
            print(f"    → URL directa sin datos, buscando...")
        found_url = search_pc(card_name, set_name, number)
        if found_url:
            time.sleep(RATE_LIMIT)
            chart, used_url = fetch_chart_data(found_url)

    if not chart:
        result["status"] = "not_found"
        return result

    result["url"] = used_url

    # 3. Extraer precios
    prices = extract_prices(chart)
    if not prices:
        result["status"] = "no_prices"
        return result

    result["points"]   = len(prices)
    result["date_min"] = prices[0]["date"]
    result["date_max"] = prices[-1]["date"]
    # Muestra: primer, medio y último precio
    mid = len(prices) // 2
    result["samples"] = [prices[0], prices[mid], prices[-1]]

    # 4. Insertar en Supabase
    inserted = upsert_prices(card_id, prices)
    result["inserted"] = inserted
    result["status"]   = "ok"
    return result


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Carga histórico de precios desde PriceCharting")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="10 cartas de prueba")
    mode.add_argument("--all",  action="store_true", help="Todas las cartas de la DB")
    mode.add_argument("--id",   type=str,            help="ID de carta específico (ej: sv3-223)")
    args = parser.parse_args()

    if args.id:
        target_ids = [args.id]
    elif args.test:
        target_ids = TEST_CARD_IDS
    elif args.all:
        target_ids = None
    else:
        parser.print_help()
        sys.exit(0)

    print("=" * 66)
    print(" Pokémon TCG Tracker — Histórico de precios (PriceCharting)")
    print("=" * 66)

    # Cargar cartas desde Supabase
    print("\n[1/3] Cargando cartas desde Supabase...")
    cards = get_cards(target_ids)
    if not cards:
        print("  ERROR: no se encontraron las cartas.")
        sys.exit(1)
    print(f"  {len(cards)} cartas a procesar")

    # Obtener nombres de sets
    set_ids  = list({c["set_id"] for c in cards})
    set_names = get_set_names(set_ids)

    # Procesar cada carta
    print(f"\n[2/3] Extrayendo histórico desde PriceCharting...")
    print("-" * 66)

    results    = []
    total_pts  = 0
    total_ins  = 0

    for i, card in enumerate(cards, 1):
        card_id = card["id"]
        set_id  = card["set_id"]
        name    = card["name"]
        num     = card["collector_number"]
        set_name = set_names.get(set_id, set_id)

        slug_set  = SET_SLUG_OVERRIDES.get(set_name) or to_pc_slug(set_name)
        slug_card = to_pc_slug(name)
        direct_url = f"{PC_BASE}/game/pokemon-{slug_set}/{slug_card}-{num}"

        print(f"\n  [{i:>2}/{len(cards)}] {card_id}  {name} #{num}")
        print(f"         Set: {set_name}")
        print(f"         URL: pokemon-{slug_set}/{slug_card}-{num}")

        res = process_card(card, set_names)
        results.append(res)

        if res["status"] == "ok":
            total_pts += res["points"]
            total_ins += res["inserted"]
            samples_str = "  ".join(
                f"{s['date']}: ${s['market_usd']:.2f}" for s in res["samples"]
            )
            print(f"         ✓  {res['points']} puntos  |  {res['date_min']} → {res['date_max']}")
            print(f"            {samples_str}")
            print(f"            {res['inserted']} filas insertadas en Supabase")
        elif res["status"] == "no_prices":
            print(f"         ✗  Encontrada pero sin precios en campo '{PRICE_FIELD}'")
        else:
            print(f"         ✗  No encontrada en PriceCharting")

        time.sleep(RATE_LIMIT)

    # Resumen final
    ok      = [r for r in results if r["status"] == "ok"]
    failed  = [r for r in results if r["status"] != "ok"]

    print("\n" + "=" * 66)
    print(f" COMPLETADO")
    print("=" * 66)
    print(f"  Cartas procesadas:   {len(cards)}")
    print(f"  Encontradas en PC:   {len(ok)}")
    print(f"  No encontradas:      {len(failed)}")
    print(f"  Puntos históricos:   {total_pts}")
    print(f"  Filas insertadas:    {total_ins}")
    print()

    if ok:
        print("  Cartas con datos:")
        for r in ok:
            print(f"    {r['card_id']:<20} {r['points']:>3} pts  {r['date_min']} → {r['date_max']}")

    if failed:
        print(f"\n  No encontradas ({len(failed)}):")
        for r in failed:
            print(f"    {r['card_id']:<20} {r['name']} ({r['set']})")
    print("=" * 66)


if __name__ == "__main__":
    main()
