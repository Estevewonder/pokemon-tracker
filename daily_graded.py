#!/usr/bin/env python3
"""
daily_graded.py - Precios PSA 10 diarios desde PriceCharting con frecuencia variable.

Fuente: PriceCharting.com, campo "manualonly" (PSA 10)
Destino: prices_graded (grade='10', grader='PSA')

Frecuencia:
  - Todos los días:   Scarlet & Violet, Sword & Shield, Mega Evolution
  - Solo lunes:       WOTC, EX, DP·BW, XY, Sun & Moon

Lanzamiento:
  python3 daily_graded.py                  # ejecución normal
  nohup python3 -u daily_graded.py > graded_daily.log 2>&1 &
"""

import subprocess, sys

def _try_import(mod):
    try: __import__(mod); return True
    except ImportError: return False

def ensure_deps():
    deps = {"python-dotenv": "dotenv", "requests": "requests",
            "supabase": "supabase", "beautifulsoup4": "bs4"}
    missing = [p for p, m in deps.items() if not _try_import(m)]
    if missing:
        for p in missing:
            subprocess.check_call([sys.executable, "-m", "pip", "install", p, "-q"])

ensure_deps()

import os, re, json, time, datetime
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
load_dotenv('/Users/estevewonder/pokemon-tracker/.env')

_raw_url     = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_URL = _raw_url.removesuffix("/rest/v1")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not all([SUPABASE_URL, SUPABASE_KEY]):
    print("ERROR: Faltan credenciales Supabase en .env")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PC_BASE     = "https://www.pricecharting.com"
RATE_LIMIT  = 1.2
TIMEOUT     = 20
PSA10_FIELD = "manualonly"
GRADE       = "10"
GRADER      = "PSA"
TODAY       = datetime.date.today().isoformat()
IS_MONDAY   = datetime.date.today().weekday() == 0

MODERN_ERAS = {"Scarlet & Violet", "Sword & Shield", "Mega Evolution"}

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

def fetch_chart_data(url: str) -> dict:
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return {}
        m = re.search(r'chart_data\s*=\s*(\{.*?\})\s*;?\s*\n', resp.text, re.S)
        if not m:
            return {}
        raw = m.group(1)
        return json.loads(raw[:raw.rfind("}") + 1])
    except Exception:
        return {}


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

def get_set_era_map() -> dict[str, str]:
    era_rows = supabase.table("eras").select("id,name").execute().data
    era_id_to_name = {r["id"]: r["name"] for r in era_rows}
    set_rows = supabase.table("sets").select("id,era_id,name").execute().data
    return {r["id"]: (era_id_to_name.get(r["era_id"], ""), r["name"]) for r in set_rows}


def get_cards(allowed_set_ids: set) -> list[dict]:
    rows, offset, psize = [], 0, 1000
    while True:
        r = supabase.table("cards").select("id,name,collector_number,set_id") \
            .range(offset, offset + psize - 1).execute()
        rows.extend([c for c in r.data if c["set_id"] in allowed_set_ids])
        if len(r.data) < psize:
            break
        offset += psize
    return rows


def upsert_graded(card_id: str, prices: list[dict]) -> int:
    if not prices:
        return 0
    rows = [
        {"card_id": card_id, "date": p["date"],
         "grade": GRADE, "grader": GRADER,
         "price": p["price"], "source": "pricecharting"}
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
            print(f"    WARN chunk: {exc}")
    return inserted


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    mode = "LUNES — todas las eras" if IS_MONDAY else \
           f"día normal — eras modernas ({', '.join(sorted(MODERN_ERAS))})"

    print("=" * 66)
    print(f" Pokémon TCG Tracker — PSA 10 Diario  {TODAY}")
    print(f" Modo: {mode}")
    print("=" * 66)

    # 1. Sets activos según día
    print("\n[1/3] Cargando sets y era map...")
    set_era_name_map = get_set_era_map()   # {set_id: (era_name, set_name)}

    if IS_MONDAY:
        active_set_ids = set(set_era_name_map.keys())
    else:
        active_set_ids = {sid for sid, (era, _) in set_era_name_map.items()
                          if era in MODERN_ERAS}

    era_sets: dict[str, list] = {}
    for sid in active_set_ids:
        era = set_era_name_map[sid][0]
        era_sets.setdefault(era, []).append(sid)

    print(f"  {len(active_set_ids)} sets activos hoy:")
    for era, sids in sorted(era_sets.items()):
        print(f"    {era}: {len(sids)} sets")

    # 2. Cargar cartas
    print("\n[2/3] Cargando cartas y scrapeando PSA 10...")
    print("-" * 66)
    cards = get_cards(active_set_ids)
    print(f"  {len(cards)} cartas a procesar")

    # Mapa set_id → set_name para construir URLs
    set_name_map = {sid: name for sid, (era, name) in set_era_name_map.items()}

    era_stats: dict[str, dict] = {}
    total_pts, total_ins = 0, 0
    not_found, no_psa = 0, 0

    for i, card in enumerate(cards, 1):
        card_id  = card["id"]
        name     = card["name"]
        number   = card["collector_number"]
        set_id   = card["set_id"]
        set_name = set_name_map.get(set_id, set_id)
        era      = set_era_name_map.get(set_id, ("?", ""))[0]

        if era not in era_stats:
            era_stats[era] = {"cards": 0, "found": 0, "pts": 0, "ins": 0}
        era_stats[era]["cards"] += 1

        # Fetch PriceCharting
        url   = get_pc_url(set_name, name, number)
        chart = fetch_chart_data(url)
        if not chart:
            found_url = search_pc(name, set_name, number)
            if found_url:
                time.sleep(RATE_LIMIT)
                chart = fetch_chart_data(found_url)

        if not chart:
            not_found += 1
            time.sleep(RATE_LIMIT)
            if i % 50 == 0:
                print(f"  [{i:>5}/{len(cards)}] {era:<22}  "
                      f"ins={total_ins}  sin_datos={not_found+no_psa}")
            continue

        prices = extract_psa10(chart)
        if not prices:
            no_psa += 1
            time.sleep(RATE_LIMIT)
            continue

        ins = upsert_graded(card_id, prices)
        total_pts += len(prices)
        total_ins += ins
        era_stats[era]["found"] += 1
        era_stats[era]["pts"]   += len(prices)
        era_stats[era]["ins"]   += ins

        if i % 50 == 0:
            print(f"  [{i:>5}/{len(cards)}] {era:<22}  "
                  f"ins={total_ins}  sin_datos={not_found+no_psa}")

        time.sleep(RATE_LIMIT)

    # 3. Resumen
    print("\n" + "=" * 66)
    print(f" COMPLETADO — {TODAY}")
    print(f" Modo: {mode}")
    print("=" * 66)
    print(f"  {'ERA':<22} {'CARTAS':>7}  {'CON PSA10':>9}  {'PUNTOS':>7}  {'INS':>7}")
    print(f"  {'─'*22} {'─'*7}  {'─'*9}  {'─'*7}  {'─'*7}")
    for era, st in sorted(era_stats.items()):
        print(f"  {era:<22} {st['cards']:>7}  {st['found']:>9}  {st['pts']:>7}  {st['ins']:>7}")
    print(f"  {'─'*22} {'─'*7}  {'─'*9}  {'─'*7}  {'─'*7}")
    total_cards = sum(s["cards"] for s in era_stats.values())
    total_found = sum(s["found"] for s in era_stats.values())
    print(f"  {'TOTAL':<22} {total_cards:>7}  {total_found:>9}  {total_pts:>7}  {total_ins:>7}")
    print(f"\n  Sin página PC:       {not_found}")
    print(f"  Sin datos PSA 10:    {no_psa}")
    print("=" * 66)


if __name__ == "__main__":
    main()
