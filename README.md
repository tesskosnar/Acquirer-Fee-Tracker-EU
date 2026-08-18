# Merchant Fee Tracker

Interaktivní GitHub Pages dashboard pro srovnání veřejných cen acquiringu a platební akceptace napříč celou EU/EHP.

## Původ dat

Základní dataset vznikl postupně (7 CEE zemí → 12 → 30), pak byl nahrazen výrazně bohatším datasetem od jiného AI nástroje, který jsem před zapracováním kriticky ověřil: náhodný vzorek přepočtů, křížová kontrola proti už dřív ověřeným hodnotám (Stripe 1,5 %+0,25 EUR sedělo přesně), kontrola zdrojových URL a metodiky. Nešlo o slepé zkopírování - viz `Kontrola`/`verification` pole u každé nabídky a poznámka níž o tom, co "kontrola" u tohohle datasetu znamená.

## Tři typy cen - záměrně nemíchané do jednoho žebříčku

- **Veřejný ceník** (blended) - přímo srovnatelná all-in cena.
- **Procesní/markup složka (IC++)** - jen marže poskytovatele nad interchange, NENÍ to celková cena (skutečný náklad je vyšší o interchange a scheme fee). V UI označeno žlutým štítkem "jen markup".
- **Individuální nabídka** - cena na vyžádání, žádné číslo se nevymýšlí, poskytovatel zůstává v přehledu jako referenční bod.

## Co dashboard obsahuje

- karty / A2A / lokální metody (BLIK, MB WAY, Multibanco...) jako oddělené řádky,
- mapa celé EU/EHP obarvená podle sazby (paleta #003f5c → #ffa600, tmavá = levnější),
- kontextové srovnání - klikneš na zemi nebo poskytovatele, teprve pak se rozbalí tabulka,
- nastavitelná částka transakce (výchozí 1000 Kč), žádný zbytečný měsíční kalkulátor,
- reálná sazba pro zadanou transakci: procentní poplatek plus pevná část převedená kurzem se vydělí částkou transakce a zobrazí jako jedno výsledné procento,
- veřejný zdroj u každého řádku s odkazem přímo na konkrétní ceník,
- týdenní historii a export CSV,
- bezpečný režim: při nefunkčním nebo nejistém parseru se zachová poslední ověřená sazba a označí se ke kontrole (v UI jako jednoduchý stavový puntík, ne syrový interní text).

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

### Adyen: kontrola po zemích a podle skutečného typu metody

Adyen je výjimka z obecného textového parseru. Jeho ceník mění processing fee a dostupné metody podle zvolené země, proto se už nekontroluje jedním globálním regulárním výrazem. Aktualizace kombinuje:

- vložená country/region data oficiálního ceníku (např. Evropa = 0,11 EUR processing fee),
- veřejný endpoint používaný samotnou stránkou Adyenu pro přesnou dostupnost metody v jednotlivých zemích,
- oficiální typ metody (`Online banking`, `Direct debit`, `Cards`), takže A2A nezmizí jen proto, že její název neobsahuje „A2A“,
- konkrétní payment-method fee z ceníkové tabulky.

U karet se ukládají oddělené komponenty: processing fee, 0,60% Adyen acquiring markup, průchozí interchange a průchozí scheme fee. Referenční hodnoty 0,20 % pro EEA spotřebitelské debetní karty a 0,30 % pro kreditní karty se zobrazují jako interchange reference, nikoli jako pevná all-in cena.

U IC++ nabídek se výsledné procento označuje jako minimální známá sazba, protože přesná interchange a scheme fee závisí na konkrétní kartě a nejsou v publikované ceně pevně dané. Tyto neúplné sazby se nezahrnují do mapy, minima, maxima ani mediánu plně porovnatelných nabídek.

## Nahrání na GitHub

1. Vytvoř nový **public** repozitář, například `Acquirer-Fee-Tracker-EU`.
2. Nahraj **obsah této složky**, ne nadřazenou složku ze ZIPu.
3. Otevři `Settings → Pages` a nastav `Deploy from a branch`, větev `main`, složka `/docs`.
4. V `Actions` spusť `Weekly acquirer fee update` přes `Run workflow`, aby se ověřily zdroje a aktuální kurz.
5. Web bude na adrese `https://tesskosnar.github.io/Acquirer-Fee-Tracker-EU/`.

## Důležitá metodická poznámka

Veřejná cena platební brány není vždy totéž jako čistý acquirer markup. Některé nabídky jsou blended, jiné mohou obsahovat interchange a scheme fees, jiné jsou pouze technická gateway nebo A2A/wallet. Dashboard je proto rozlišuje a nemíchá individuální nabídky do žebříčku.
