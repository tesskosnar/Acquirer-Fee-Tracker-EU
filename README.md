# Merchant Fee Tracker

Interaktivní GitHub Pages dashboard pro srovnání veřejných cen acquiringu a platební akceptace napříč celou EU/EHP (30 zemí; zatím reálně podložená data pro 7 z nich, zbytek je na mapě vidět jako "zatím nedoplněno"). Výchozí scénář je transakce **500 Kč**, nastavitelná; pevné složky v cizích měnách se přepočítávají posledním dostupným kurzem devizového trhu ČNB.

## Jak se s dashboardem pracuje

Mapa i srovnávací tabulka jsou **kontextové** - ve výchozím stavu vidíš jen mapu a KPI. Klikneš na zemi → tabulka se rozbalí pro tu zemi. Klikneš na jméno poskytovatele v tabulce → přepneš do srovnání toho poskytovatele napříč všemi zeměmi, kde působí (mapa zvýrazní jen ty). Tlačítko "zpět na přehled" nebo druhý klik na stejnou zemi tě vrátí na začátek.

## Co dashboard obsahuje

- karty / A2A / peněženky jako oddělené metody,
- rozlišení acquirera, PSP, gateway a A2A schématu (filtr, ne matoucí popisek u každého řádku),
- přepočet libovolné hodnoty transakce a měsíčního počtu transakcí,
- mapa celé EU/EHP obarvená podle sazby (paleta #003f5c → #ffa600, tmavá = levnější),
- srovnání "co poskytovatel nabízí navíc" - u dvoutarifních nabídek (např. PayU) je vidět zaváděcí i standardní sazba zvlášť, ne jen ta levnější,
- veřejný zdroj u každého řádku s odkazem přímo na konkrétní ceník (ne obecnou stránku),
- týdenní historii a export CSV,
- bezpečný režim: při nefunkčním nebo nejistém parseru se zachová poslední ověřená sazba a označí se ke kontrole (v UI jako jednoduchý stavový puntík, ne syrový interní text).

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
