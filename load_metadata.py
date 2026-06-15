#!/usr/bin/env python3
"""
load_metadata.py - Carga inicial de sets y cartas en Supabase desde la Pokémon TCG API.
Filtra por rarezas relevantes según la era. Seguro re-ejecutar (upsert).
"""

import subprocess
import sys

# --- Auto-instalar dependencias ---
def _pip(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package, "-q"])

def ensure_deps():
    deps = {"python-dotenv": "dotenv", "requests": "requests", "supabase": "supabase"}
    missing = []
    for pkg, mod in deps.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Instalando dependencias: {', '.join(missing)}")
        for pkg in missing:
            _pip(pkg)
        print("Dependencias instaladas.\n")

ensure_deps()

# --- Imports ---
import os
import time
import requests
from dotenv import load_dotenv
from supabase import create_client

# --- Credenciales ---
load_dotenv()

POKEMON_API_KEY = os.getenv("POKEMON_TCG_API_KEY", "").strip()
_raw_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
# supabase-py necesita la URL base del proyecto, sin /rest/v1
SUPABASE_URL = _raw_url.removesuffix("/rest/v1")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not all([POKEMON_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Faltan credenciales en .env. Revisa POKEMON_TCG_API_KEY, SUPABASE_URL y SUPABASE_KEY.")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

API_BASE = "https://api.pokemontcg.io/v2"
HEADERS = {"X-Api-Key": POKEMON_API_KEY}
RATE_LIMIT_S = 0.1  # 100ms entre llamadas

# --- Mapeo series API → nombre de era en Supabase ---
SERIES_TO_ERA = {
    "Base":                    "WOTC",
    "Gym":                     "WOTC",
    "Neo":                     "WOTC",
    "Legendary Collection":    "WOTC",
    "e-Card":                  "WOTC",
    "EX":                      "EX",
    "Diamond & Pearl":         "DP·BW",
    "Platinum":                "DP·BW",
    "HeartGold & SoulSilver":  "DP·BW",
    "Black & White":           "DP·BW",
    "XY":                      "XY",
    "Sun & Moon":              "Sun & Moon",
    "Sword & Shield":          "Sword & Shield",
    "Scarlet & Violet":        "Scarlet & Violet",
    "Mega Evolution":          "Mega Evolution",
}

# --- Rarezas válidas por era (strings exactos de la API) ---
ERA_RARITIES = {
    "WOTC": {
        "Rare Holo",
    },
    "EX": {
        "Rare Holo EX",
        "Rare Holo Gold Star",
        "Rare Secret",
    },
    "DP·BW": {
        "Rare Holo LV.X",
        "Rare Prime",
        "LEGEND",
        "Rare Ultra",         # Full Art trainers/supporters
        "Rare Secret",
    },
    "XY": {
        "Rare Holo EX",
        "Rare Ultra",         # EX Full Art + Mega EX FA
        "Rare Secret",
    },
    "Sun & Moon": {
        "Rare Holo GX",
        "Rare Ultra",         # GX Full Art
        "Rare Rainbow",       # Rainbow Rare
        "Rare Secret",        # Hyper Rare / Secret Rare gold
    },
    "Sword & Shield": {
        "Rare Holo V",
        "Rare Holo VMAX",
        "Rare Holo VSTAR",
        "Rare Ultra",         # V/VMAX/VSTAR Alternate Art
        "Rare Rainbow",
        "Rare Secret",        # Gold cards
        "Trainer Gallery Rare Holo",
        "Trainer Gallery Rare Ultra",
        "Trainer Gallery Rare Secret",
        "Amazing Rare",
    },
    "Scarlet & Violet": {
        "Double Rare",
        "Rare Ultra",         # Ultra Rare (ex FA)
        "Illustration Rare",
        "Special Illustration Rare",
        "Hyper Rare",
        "Shiny Rare",
        "Shiny Ultra Rare",
    },
    "Mega Evolution": {
        "Double Rare",
        "Ultra Rare",
        "Illustration Rare",
        "Special Illustration Rare",
        "Mega Hyper Rare",
    },
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_eras() -> dict[str, int]:
    """Devuelve {nombre_era: id} desde Supabase."""
    resp = supabase.table("eras").select("id, name").execute()
    return {row["name"]: row["id"] for row in resp.data}


def fetch_all_sets() -> list[dict]:
    """Descarga todos los sets de la API (una sola página de 250 es suficiente)."""
    resp = requests.get(f"{API_BASE}/sets", headers=HEADERS, params={"pageSize": 250})
    resp.raise_for_status()
    return resp.json()["data"]


def fetch_cards_for_set(set_id: str) -> list[dict]:
    """Descarga todas las cartas de un set con paginación de 250 y retry en 404/429."""
    all_cards = []
    page = 1
    while True:
        retries = 3
        while retries:
            try:
                resp = requests.get(
                    f"{API_BASE}/cards",
                    headers=HEADERS,
                    params={"q": f"set.id:{set_id}", "page": page, "pageSize": 250},
                )
                if resp.status_code in (404, 429):
                    retries -= 1
                    wait = 5 if resp.status_code == 429 else 2
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                batch = resp.json().get("data", [])
                all_cards.extend(batch)
                if len(batch) < 250:
                    return all_cards
                page += 1
                time.sleep(RATE_LIMIT_S)
                break
            except Exception as exc:
                print(f"    WARN: error descargando página {page} de {set_id}: {exc}")
                return all_cards
        else:
            print(f"    WARN: sin respuesta tras reintentos para {set_id} pág {page}")
            return all_cards
    return all_cards


def upsert_sets(sets_data: list[dict], eras: dict[str, int]) -> tuple[int, int]:
    inserted, skipped = 0, 0
    for s in sets_data:
        era_name = SERIES_TO_ERA.get(s.get("series", ""))
        if not era_name or era_name not in eras:
            skipped += 1
            continue
        row = {
            "id":            s["id"],
            "name":          s["name"],
            "series":        s.get("series"),
            "era_id":        eras[era_name],
            "printed_total": s.get("printedTotal", 0),
            "release_date":  s.get("releaseDate"),
            "symbol_url":    s.get("images", {}).get("symbol"),
            "logo_url":      s.get("images", {}).get("logo"),
        }
        try:
            supabase.table("sets").upsert(row).execute()
            inserted += 1
        except Exception as exc:
            print(f"  WARN: error insertando set {s['id']}: {exc}")
    return inserted, skipped


def upsert_cards(cards: list[dict], set_id: str, era_id: int) -> int:
    inserted = 0
    for card in cards:
        row = {
            "id":               card["id"],
            "set_id":           set_id,
            "era_id":           era_id,
            "name":             card.get("name"),
            "rarity":           card.get("rarity"),
            "number":           card.get("number"),
            "collector_number": card.get("number"),
            "artist":           card.get("artist"),
            "supertype":        card.get("supertype"),
            "subtypes":         card.get("subtypes"),
            "types":            card.get("types"),
            "hp":               card.get("hp"),
            "image_small":      card.get("images", {}).get("small"),
            "image_large":      card.get("images", {}).get("large"),
            "tcgplayer_url":    card.get("tcgplayer", {}).get("url"),
        }
        try:
            supabase.table("cards").upsert(row).execute()
            inserted += 1
        except Exception as exc:
            print(f"    WARN: error insertando card {card['id']}: {exc}")
    return inserted


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 62)
    print(" Pokémon TCG Tracker — Carga de Metadata")
    print("=" * 62)

    # 1. Eras
    print("\n[1/4] Leyendo eras desde Supabase...")
    eras = get_eras()
    if not eras:
        print("ERROR: No se encontraron eras. Verifica que la tabla 'eras' tenga datos.")
        sys.exit(1)
    print(f"  Eras: {', '.join(eras.keys())}")

    # 2. Sets
    print("\n[2/4] Descargando sets desde Pokémon TCG API...")
    all_sets = fetch_all_sets()
    print(f"  Total sets en API: {len(all_sets)}")
    time.sleep(RATE_LIMIT_S)

    # 3. Insertar sets
    print("\n[3/4] Insertando sets en Supabase...")
    ins, skip = upsert_sets(all_sets, eras)
    print(f"  {ins} sets insertados, {skip} ignorados (series no mapeada)")

    # 4. Cartas
    relevant_sets = [
        s for s in all_sets
        if SERIES_TO_ERA.get(s.get("series", "")) in eras
    ]
    print(f"\n[4/4] Cargando cartas — {len(relevant_sets)} sets relevantes")
    print("-" * 62)

    total_cards = 0
    for i, s in enumerate(relevant_sets, 1):
        era_name = SERIES_TO_ERA[s["series"]]
        allowed  = ERA_RARITIES.get(era_name, set())
        print(f"  [{i:>3}/{len(relevant_sets)}] {s['name']:<35} ({era_name})")

        raw_cards = fetch_cards_for_set(s["id"])
        filtered  = [c for c in raw_cards if c.get("rarity") in allowed]

        if filtered:
            n = upsert_cards(filtered, s["id"], eras[era_name])
            total_cards += n
            print(f"           → {n}/{len(raw_cards)} cartas con rareza relevante insertadas")
        else:
            print(f"           → 0/{len(raw_cards)} cartas con rareza relevante")

        time.sleep(RATE_LIMIT_S)

    print("\n" + "=" * 62)
    print(f" COMPLETADO: {total_cards} cartas cargadas en Supabase")
    print("=" * 62)


if __name__ == "__main__":
    main()
