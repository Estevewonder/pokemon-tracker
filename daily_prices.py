#!/usr/bin/env python3
"""
daily_prices.py - Extrae precios diarios TCGPlayer (USD market price) para todas las
cartas en Supabase e inserta en prices_en. Seguro re-ejecutar (upsert por card_id + date).
"""

import subprocess
import sys

def _pip(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package, "-q"])

def ensure_deps():
    deps = {"python-dotenv": "dotenv", "requests": "requests", "supabase": "supabase"}
    missing = [pkg for pkg, mod in deps.items() if not __import__.__module__ or
               not next((True for _ in [__import__(mod)] if True), None)
               if not _try_import(mod)]
    if missing:
        print(f"Instalando dependencias: {', '.join(missing)}")
        for pkg in missing:
            _pip(pkg)

def _try_import(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False

def ensure_deps():
    deps = {"python-dotenv": "dotenv", "requests": "requests", "supabase": "supabase"}
    missing = [pkg for pkg, mod in deps.items() if not _try_import(mod)]
    if missing:
        print(f"Instalando dependencias: {', '.join(missing)}")
        for pkg in missing:
            _pip(pkg)
        print("Listo.\n")

ensure_deps()

import os
import time
import datetime
import requests
from dotenv import load_dotenv
from supabase import create_client

# --- Credenciales ---
load_dotenv()
POKEMON_API_KEY = os.getenv("POKEMON_TCG_API_KEY", "").strip()
_raw_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_URL = _raw_url.removesuffix("/rest/v1")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not all([POKEMON_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Faltan credenciales en .env")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

API_BASE   = "https://api.pokemontcg.io/v2"
HEADERS    = {"X-Api-Key": POKEMON_API_KEY}
RATE_LIMIT = 0.25  # segundos entre requests
TODAY      = datetime.date.today().isoformat()

# Orden de preferencia para elegir el precio de mercado
PRICE_PRIORITY = [
    "holofoil",
    "1stEditionHolofoil",
    "unlimitedHolofoil",
    "reverseHolofoil",
    "normal",
    "1stEdition",
    "unlimited",
]


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def pick_market(prices: dict) -> float | None:
    """Devuelve el primer market price disponible según prioridad."""
    for variant in PRICE_PRIORITY:
        val = prices.get(variant, {}).get("market")
        if val is not None:
            return float(val)
    # Fallback: cualquier variante con market
    for v in prices.values():
        if isinstance(v, dict) and v.get("market") is not None:
            return float(v["market"])
    return None


def get_cards_by_set() -> dict[str, set]:
    """{set_id: {card_id, ...}} para todas las cartas en Supabase."""
    by_set: dict[str, set] = {}
    page_size = 1000
    offset = 0
    while True:
        resp = supabase.table("cards").select("id,set_id") \
            .range(offset, offset + page_size - 1).execute()
        for row in resp.data:
            by_set.setdefault(row["set_id"], set()).add(row["id"])
        if len(resp.data) < page_size:
            break
        offset += page_size
    return by_set


def fetch_prices_for_set(set_id: str, our_ids: set) -> list[dict]:
    """Descarga precios del set desde la API y filtra a nuestras cartas."""
    rows = []
    page = 1
    while True:
        retries = 3
        while retries:
            try:
                resp = requests.get(
                    f"{API_BASE}/cards",
                    headers=HEADERS,
                    params={"q": f"set.id:{set_id}", "page": page, "pageSize": 250},
                    timeout=60,
                )
                if resp.status_code in (429, 504):
                    retries -= 1
                    time.sleep(10)
                    continue
                if resp.status_code == 404:
                    retries -= 1
                    time.sleep(3)
                    continue
                resp.raise_for_status()
                cards = resp.json().get("data", [])
                for card in cards:
                    if card["id"] not in our_ids:
                        continue
                    prices = card.get("tcgplayer", {}).get("prices", {})
                    market = pick_market(prices)
                    if market is not None:
                        rows.append({
                            "card_id":    card["id"],
                            "date":       TODAY,
                            "market_usd": market,
                        })
                if len(cards) < 250:
                    return rows
                page += 1
                time.sleep(RATE_LIMIT)
                break
            except Exception as exc:
                print(f"    WARN {set_id} pág {page}: {exc}")
                return rows
        else:
            print(f"    WARN sin respuesta para {set_id} pág {page} tras reintentos")
            return rows
    return rows


def upsert_batch(rows: list[dict]) -> int:
    """Inserta en chunks de 500. Devuelve total insertado."""
    inserted = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        try:
            supabase.table("prices_en") \
                .upsert(chunk, on_conflict="card_id,date").execute()
            inserted += len(chunk)
        except Exception as exc:
            print(f"  WARN error insertando chunk: {exc}")
    return inserted


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 62)
    print(f" Pokémon TCG Tracker — Precios Diarios  {TODAY}")
    print("=" * 62)

    # 1. Cargar cartas desde Supabase
    print("\n[1/3] Cargando cartas desde Supabase...")
    cards_by_set = get_cards_by_set()
    total_cards = sum(len(v) for v in cards_by_set.values())
    print(f"  {total_cards} cartas en {len(cards_by_set)} sets")

    # 2. Extraer precios de la API set a set
    print(f"\n[2/3] Extrayendo precios desde API...")
    print("-" * 62)

    all_rows = []
    sets = sorted(cards_by_set.keys())
    for i, set_id in enumerate(sets, 1):
        our_ids = cards_by_set[set_id]
        price_rows = fetch_prices_for_set(set_id, our_ids)
        all_rows.extend(price_rows)
        found = len(price_rows)
        total = len(our_ids)
        no_price = total - found
        suffix = f"  (sin precio TCGPlayer: {no_price})" if no_price else ""
        print(f"  [{i:>3}/{len(sets)}] {set_id:<16} {found:>4}/{total} precios{suffix}")
        time.sleep(RATE_LIMIT)

    # 3. Insertar en Supabase
    print(f"\n[3/3] Insertando {len(all_rows)} precios en Supabase...")
    inserted = upsert_batch(all_rows)

    cards_con_precio = len({r["card_id"] for r in all_rows})
    cards_sin_precio = total_cards - cards_con_precio

    print("\n" + "=" * 62)
    print(f" COMPLETADO: {inserted} precios insertados para {TODAY}")
    print(f"  Cartas con precio:    {cards_con_precio}")
    print(f"  Cartas sin precio:    {cards_sin_precio}  (no listadas en TCGPlayer)")
    print("=" * 62)


if __name__ == "__main__":
    main()
