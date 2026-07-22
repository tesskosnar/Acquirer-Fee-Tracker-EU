# Merchant Acceptance Fee Tracker

Interaktivní GitHub Pages dashboard pro srovnání veřejných cen acquiringu a platební akceptace. Výchozí scénář je transakce **500 Kč**; pevné složky v cizích měnách se přepočítávají posledním dostupným kurzem devizového trhu ČNB.

## Co dashboard obsahuje

- karty / A2A / peněženky jako oddělené metody,
- rozlišení acquirera, PSP, gateway a A2A schématu,
- přepočet libovolné hodnoty transakce a měsíčního počtu transakcí,
- mapa pokrytých zemí, žebříček a filtry,
- veřejný zdroj u každého řádku,
- týdenní historii a export CSV,
- bezpečný režim: při nefunkčním nebo nejistém parseru se zachová poslední ověřená sazba a označí se ke kontrole.

## Automatická aktualizace

`.github/workflows/weekly-update.yml` běží každé pondělí. Skript:

1. stáhne poslední dostupné kurzy ČNB,
2. kontroluje veřejné ceníky a jejich obsahový hash,
3. zkusí vytěžit sazby z okolí definovaných textových kotev,
4. nové hodnoty přijme pouze při dostatečné důvěře parseru,
5. přepočítá náklady, uloží `latest.json`, CSV, change log a týdenní snapshot,
6. změny commitne zpět do repozitáře.

## Nahrání na GitHub

1. Vytvoř nový **public** repozitář, například `Acquirer-Fee-Tracker-EU`.
2. Nahraj **obsah této složky**, ne nadřazenou složku ze ZIPu.
3. Otevři `Settings → Pages` a nastav `Deploy from a branch`, větev `main`, složka `/docs`.
4. V `Actions` spusť `Weekly acquirer fee update` přes `Run workflow`, aby se ověřily zdroje a aktuální kurz.
5. Web bude na adrese `https://tesskosnar.github.io/Acquirer-Fee-Tracker-EU/`.

## Důležitá metodická poznámka

Veřejná cena platební brány není vždy totéž jako čistý acquirer markup. Některé nabídky jsou blended, jiné mohou obsahovat interchange a scheme fees, jiné jsou pouze technická gateway nebo A2A/wallet. Dashboard je proto rozlišuje a nemíchá individuální nabídky do žebříčku.
