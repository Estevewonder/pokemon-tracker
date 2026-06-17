#!/usr/bin/env python3
"""
daily_prices.py - Precios diarios TCGPlayer con frecuencia variable por era.

Frecuencia:
  - Todos los días:   Scarlet & Violet, Sword & Shield, Mega Evolution
  - Solo lunes:       WOTC, EX, DP·BW, XY, Sun & Moon

Columna destino: prices_en.price_market
"""

import subprocess, sys

def _try_import(mod):
    try: __import__(mod); return True
    except ImportError: return False

def ensure_deps():
    deps = {"python-dotenv": "dotenv", "requests": "requests", "supabase": "supabase"}
    missing = [p for p, m in deps.items() if not _try_import(m)]
    if missing:
        for p in missing:
            subprocess.check_call([sys.executable, "-m", "pip", "install", p, "-q"])

ensure_deps()

import os, time, datetime, requests
from dotenv import load_dotenv
from supabase import create_client

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
load_dotenv('/Users/estevewonder/pokemon-tracker/.env')

POKEMON_API_KEY = os.getenv("POKEMON_TCG_API_KEY", "").strip()
_raw_url        = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_URL    = _raw_url.removesuffix("/rest/v1")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY", "").strip()

if not all([POKEMON_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Faltan credenciales en .env")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

API_BASE   = "https://api.pokemontcg.io/v2"
HEADERS    = {"X-Api-Key": POKEMON_API_KEY}
RATE_LIMIT = 0.25
TODAY      = datetime.date.today().isoformat()
IS_MONDAY  = datetime.date.today().weekday() == 0   # lunes = 0

# Eras que se actualizan todos los días
MODERN_ERAS = {"Scarlet & Violet", "Sword & Shield", "Mega Evolution"}

# Orden de preferencia de variante de precio TCGPlayer
PRICE_PRIORITY = [
    "holofoil", "1stEditionHolofoil", "unlimitedHolofoil",
    "reverseHolofoil", "normal", "1stEdition", "unlimited",
]


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def pick_market(prices: dict) -> float | None:
    for variant in PRICE_PRIORITY:
        val = prices.get(variant, {}).get("market")
        if val is not None:
            return float(val)
    for v in prices.values():
        if isinstance(v, dict) and v.get("market") is not None:
            return float(v["market"])
    return None


def get_set_era_map() -> dict[str, str]:
    """{set_id: era_name} para todos los sets de la DB."""
    era_rows = supabase.table("eras").select("id,name").execute().data
    era_id_to_name = {r["id"]: r["name"] for r in era_rows}
    set_rows = supabase.table("sets").select("id,era_id").execute().data
    return {r["id"]: era_id_to_name.get(r["era_id"], "") for r in set_rows}


def get_cards_by_set(allowed_set_ids: set) -> dict[str, set]:
    """{set_id: {card_id, ...}} filtrando solo los sets indicados."""
    by_set: dict[str, set] = {}
    page_size, offset = 1000, 0
    while True:
        resp = supabase.table("cards").select("id,set_id") \
            .range(offset, offset + page_size - 1).execute()
        for row in resp.data:
            if row["set_id"] in allowed_set_ids:
                by_set.setdefault(row["set_id"], set()).add(row["id"])
        if len(resp.data) < page_size:
            break
        offset += page_size
    return by_set


def fetch_prices_for_set(set_id: str, our_ids: set) -> list[dict]:
    rows, page = [], 1
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
                    retries -= 1; time.sleep(10); continue
                if resp.status_code == 404:
                    retries -= 1; time.sleep(3); continue
                resp.raise_for_status()
                cards = resp.json().get("data", [])
                for card in cards:
                    if card["id"] not in our_ids:
                        continue
                    market = pick_market(card.get("tcgplayer", {}).get("prices", {}))
                    if market is not None:
                        rows.append({"card_id": card["id"], "date": TODAY, "price_market": market})
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
    inserted = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        try:
            supabase.table("prices_en").upsert(chunk, on_conflict="card_id,date").execute()
            inserted += len(chunk)
        except Exception as exc:
            print(f"  WARN error insertando chunk: {exc}")
    return inserted


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    mode = "LUNES — todas las eras" if IS_MONDAY else \
           f"día normal — eras modernas ({', '.join(sorted(MODERN_ERAS))})"

    print("=" * 66)
    print(f" Pokémon TCG Tracker — Precios Diarios  {TODAY}")
    print(f" Modo: {mode}")
    print("=" * 66)

    # 1. Determinar sets activos según día de la semana
    print("\n[1/3] Cargando sets y era map desde Supabase...")
    set_era_map = get_set_era_map()

    if IS_MONDAY:
        active_set_ids = set(set_era_map.keys())
    else:
        active_set_ids = {sid for sid, era in set_era_map.items() if era in MODERN_ERAS}

    # Agrupar sets por era para el log
    era_sets: dict[str, list] = {}
    for sid in active_set_ids:
        era = set_era_map.get(sid, "?")
        era_sets.setdefault(era, []).append(sid)

    print(f"  {len(active_set_ids)} sets activos hoy:")
    for era, sids in sorted(era_sets.items()):
        print(f"    {era}: {len(sids)} sets")

    # 2. Cargar cartas de esos sets
    print("\n[2/3] Cargando cartas y extrayendo precios...")
    print("-" * 66)
    cards_by_set = get_cards_by_set(active_set_ids)
    total_cards  = sum(len(v) for v in cards_by_set.values())
    print(f"  {total_cards} cartas a procesar")

    all_rows = []
    era_stats: dict[str, dict] = {}
    sets_sorted = sorted(cards_by_set.keys())

    for i, set_id in enumerate(sets_sorted, 1):
        our_ids  = cards_by_set[set_id]
        era      = set_era_map.get(set_id, "?")
        price_rows = fetch_prices_for_set(set_id, our_ids)
        all_rows.extend(price_rows)

        found    = len(price_rows)
        total    = len(our_ids)
        no_price = total - found
        suffix   = f"  (sin precio: {no_price})" if no_price else ""
        print(f"  [{i:>3}/{len(sets_sorted)}] {set_id:<16} {found:>4}/{total}{suffix}")

        if era not in era_stats:
            era_stats[era] = {"sets": 0, "cards": 0, "prices": 0}
        era_stats[era]["sets"]   += 1
        era_stats[era]["cards"]  += total
        era_stats[era]["prices"] += found

        time.sleep(RATE_LIMIT)

    # 3. Insertar
    print(f"\n[3/3] Insertando {len(all_rows)} precios en Supabase...")
    inserted = upsert_batch(all_rows)

    cards_con_precio = len({r["card_id"] for r in all_rows})
    cards_sin_precio = total_cards - cards_con_precio

    print("\n" + "=" * 66)
    print(f" COMPLETADO: {inserted} precios insertados — {TODAY}")
    print(f" Modo: {mode}")
    print("=" * 66)
    print(f"  {'ERA':<22} {'SETS':>5}  {'CARTAS':>7}  {'CON PRECIO':>10}")
    print(f"  {'─'*22} {'─'*5}  {'─'*7}  {'─'*10}")
    for era, st in sorted(era_stats.items()):
        print(f"  {era:<22} {st['sets']:>5}  {st['cards']:>7}  {st['prices']:>10}")
    print(f"  {'─'*22} {'─'*5}  {'─'*7}  {'─'*10}")
    print(f"  {'TOTAL':<22} {sum(s['sets'] for s in era_stats.values()):>5}"
          f"  {total_cards:>7}  {cards_con_precio:>10}")
    print(f"\n  Sin precio TCGPlayer: {cards_sin_precio}")
    print("=" * 66)


if __name__ == "__main__":
    main()
