# Pokémon TCG Tracker

## Proyecto
Sistema de tracking de precios de cartas Pokémon TCG para una newsletter semanal en Substack. Extrae precios diariamente, almacena histórico y permite análisis de mercado EN (USD).

## Stack técnico
- Python: scripts de extracción de datos
- Pokémon TCG API (dev.pokemontcg.io): metadata de cartas + precios TCGPlayer USD
- Supabase: base de datos PostgreSQL
- GitHub Actions: automatización diaria a las 3am
- PriceCharting: volcado histórico puntual una sola vez

## Estado actual
- [x] Supabase proyecto creado: pokemon-tracker
- [x] Tablas creadas en Supabase (eras, sets, cards, prices_en)
- [x] GitHub repositorio creado: pokemon-tracker
- [x] API key Pokémon TCG obtenida
- [ ] Archivo .env con credenciales
- [x] Script carga inicial de metadata (load_metadata.py) — ~3.400 cartas, 109 sets. Cobertura WOTC → Scarlet & Violet completa
- [ ] Volcado histórico PriceCharting (load_history.py)
- [x] Script precios diario (daily_prices.py) — 3.033 precios insertados en primera ejecución
- [x] GitHub Actions (.github/workflows/daily.yml) — cron 1:00am y 1:30am UTC (3:00/3:30am Madrid verano)

## Credenciales necesarias (guardar en .env, nunca subir a GitHub)
POKEMON_TCG_API_KEY=tu_key_aqui
SUPABASE_URL=tu_url_aqui
SUPABASE_KEY=tu_anon_key_aqui

## Schema Supabase ya creado
- eras: WOTC / EX / DP·BW / XY / Sun & Moon / Sword & Shield / Scarlet & Violet
- sets: metadata de sets con era_id
- cards: tabla maestra ~4.500 cartas EN filtradas por rareza
- prices_en: serie temporal diaria USD, unique por card_id + date

## Filtro de rarezas por era
- WOTC: Holo Rare
- EX: EX card, Gold Star, Secret Rare
- DP/BW: Lv.X, Prime, Legend card, Full Art, Secret Rare
- XY: EX Full Art, Mega EX FA, Secret Rare
- Sun & Moon: GX Full Art, Rainbow Rare, Hyper Rare, Secret Rare
- Sword & Shield: V Full Art, VMAX, VSTAR, Alternate Art, Trainer Gallery, Gold Secret
- Scarlet & Violet: Double Rare, Ultra Rare FA ex, Illustration Rare, Special IR, Hyper Rare, Shiny
- Mega Evolution: Double Rare (Pokémon ex), Ultra Rare FA, Illustration Rare, Special IR, Mega Hyper Rare (gold)
  API series name: "Mega Evolution" | Sets: me1, me2, me2pt5, me3, me4

## Próximo paso
Crear load_metadata.py: script Python que llama a la Pokémon TCG API,
filtra por las rarezas definidas arriba, y carga cards + sets en Supabase.
Paginación de 250 cartas por llamada. Rate limiting 100ms entre llamadas.
Mapear set.series al era_id correspondiente en la tabla eras.
