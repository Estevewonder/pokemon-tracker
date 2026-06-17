#!/usr/bin/env python3
"""
fill_missing_artists.py - Rellena el campo artist de cartas con NULL
consultando TCGdex (api.tcgdex.net). Los IDs de pokemontcg.io se
convierten a formato TCGdex antes de consultar.
Seguro re-ejecutar (solo actualiza si TCGdex devuelve illustrator).
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
    deps = {"python-dotenv": "dotenv", "requests": "requests", "supabase": "supabase"}
    missing = [pkg for pkg, mod in deps.items() if not _try_import(mod)]
    if missing:
        print(f"Instalando dependencias: {', '.join(missing)}")
        for pkg in missing:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])
        print("Listo.\n")

ensure_deps()

import os
import re
import time
import requests
from dotenv import load_dotenv
from supabase import create_client

# --- Credenciales ---
load_dotenv()
_raw_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_URL = _raw_url.removesuffix("/rest/v1")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not all([SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Faltan SUPABASE_URL y/o SUPABASE_KEY en .env")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

TCGDEX_BASE = "https://api.tcgdex.net/v2/en/cards"
RATE_LIMIT  = 0.12   # segundos entre llamadas
TIMEOUT     = 10


# ─────────────────────────────────────────────
# Conversión pokemontcg.io ID → TCGdex ID
# ─────────────────────────────────────────────
# Reglas observadas:
#   sv1..sv9     → sv01..sv09
#   sv6pt5       → sv06.5
#   me1..me4     → me01..me04
#   sm*, swsh*   → igual (no cambian)
#   svp, smp, swshp → igual
#   me2.5 alias  → me02.5
def ptcg_to_tcgdex(ptcg_id: str) -> str:
    # Sustituir 'pt5' por '.5' (p.ej. sv8pt5 → sv8.5, swsh12pt5 → swsh12.5)
    s = ptcg_id.replace("pt5", ".5")
    # Para prefijos 'sv' y 'me', zero-pad el número principal
    m = re.match(r'^(sv|me)(\d+)(\.5)?$', s)
    if m:
        prefix, num, half = m.group(1), m.group(2), m.group(3) or ""
        return f"{prefix}{int(num):02d}{half}"
    return s


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_null_artist_cards() -> list[dict]:
    """Devuelve todas las cartas con artist IS NULL desde Supabase."""
    rows = []
    offset = 0
    while True:
        resp = (
            supabase.table("cards")
            .select("id, set_id, collector_number")
            .is_("artist", "null")
            .range(offset, offset + 999)
            .execute()
        )
        rows.extend(resp.data)
        if len(resp.data) < 1000:
            break
        offset += 1000
    return rows


def fetch_illustrator(set_id: str, collector_number: str) -> str | None:
    """Consulta TCGdex con el ID convertido y devuelve illustrator si existe."""
    if not collector_number:
        return None
    tcgdex_set = ptcg_to_tcgdex(set_id)
    card_id    = f"{tcgdex_set}-{collector_number}"
    url        = f"{TCGDEX_BASE}/{card_id}"

    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return None
            data = resp.json()
            illustrator = data.get("illustrator")
            return illustrator.strip() if illustrator else None
        except Exception:
            time.sleep(2)
    return None


def update_artist_batch(updates: list[tuple[str, str]]) -> int:
    """Actualiza artistas en Supabase. updates = [(card_id, artist), ...]"""
    ok = 0
    for card_id, artist in updates:
        try:
            supabase.table("cards").update({"artist": artist}).eq("id", card_id).execute()
            ok += 1
        except Exception as exc:
            print(f"  WARN: error actualizando {card_id}: {exc}")
    return ok


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 62)
    print(" Pokémon TCG Tracker — Rellenando artistas (TCGdex)")
    print("=" * 62)

    # 1. Obtener cartas sin artista
    print("\n[1/3] Consultando Supabase — cartas con artist NULL...")
    cards = get_null_artist_cards()
    print(f"  {len(cards)} cartas sin artista")

    if not cards:
        print("\nNada que actualizar.")
        return

    # 2. Consultar TCGdex carta a carta
    print(f"\n[2/3] Consultando TCGdex ({len(cards)} cartas)...")
    print("-" * 62)

    pending_updates: list[tuple[str, str]] = []
    not_found   = 0
    sets_filled: dict[str, int] = {}

    for i, card in enumerate(cards, 1):
        card_id  = card["id"]
        set_id   = card["set_id"]
        col_num  = card.get("collector_number") or card_id.split("-")[-1]

        artist = fetch_illustrator(set_id, col_num)
        if artist:
            pending_updates.append((card_id, artist))
            sets_filled[set_id] = sets_filled.get(set_id, 0) + 1
        else:
            not_found += 1

        if i % 50 == 0 or i == len(cards):
            pct = i / len(cards) * 100
            found_so_far = len(pending_updates)
            print(f"  [{i:>4}/{len(cards)}] {pct:5.1f}% — encontrados: {found_so_far}")

        time.sleep(RATE_LIMIT)

    # 3. Guardar en Supabase
    print(f"\n[3/3] Guardando {len(pending_updates)} artistas en Supabase...")
    saved = update_artist_batch(pending_updates)

    print("\n" + "=" * 62)
    print(" COMPLETADO")
    print("=" * 62)
    print(f"  Artistas rellenados:  {saved}")
    print(f"  Sin dato en TCGdex:   {not_found}")
    print()

    if sets_filled:
        print("  Sets actualizados:")
        for sid, count in sorted(sets_filled.items(), key=lambda x: -x[1]):
            tcgdex = ptcg_to_tcgdex(sid)
            print(f"    {sid:<20} → {tcgdex:<12} +{count} artistas")
    else:
        print("  Ningún artista encontrado en TCGdex para estas cartas.")

    print("=" * 62)


if __name__ == "__main__":
    main()
