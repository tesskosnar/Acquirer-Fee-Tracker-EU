# Merchant Fee Tracker

Interaktivní GitHub Pages dashboard pro srovnání veřejných cen acquiringu a platební akceptace napříč celou EU/EHP.

## Původ dat

Původní rozšířený dataset obsahoval řádky vytvořené AI, které nebyly všechny ověřeny jednotlivě a nelze je považovat za spolehlivý zdroj. Revize proto začíná znovu od CEE: nejprve vzniká nezávislý seznam lokálních acquirerů pro každou zemi a až potom se porovnává s databází.

Cena se doplní jen z oficiálního veřejného zdroje poskytovatele. Kontrola nekončí na stránce „Pricing“: prochází také FAQ, sazebníky a právní dokumenty, PDF, kalkulátory a lokální jazykové podstránky. Když skutečnou obchodní sazbu poskytovatel nezveřejňuje, zůstává jako `Individuální nabídka` bez vymyšleného čísla. Starší řádky, které touto revizí ještě neprošly, se ve veřejném dashboardu nezobrazují a nevstupují do mapy ani souhrnných statistik.

Pro objevování dalších jmen se používají i kvalitní oborové zdroje, zejména [Business of Payments](https://businessofpayments.substack.com/). Slouží pouze jako discovery zdroj: zařazení, země působnosti a sazba se vždy znovu potvrzují na oficiálních stránkách poskytovatele. Vedle prvního CEE registru existuje samostatný watchlist pro zbývající EU/EHP, Švýcarsko a Spojené království.

## Jak se cena zobrazuje

- Dashboard ukazuje jeden sloupec **Celková sazba** pro nastavenou částku transakce.
- Pevná transakční složka se převede kurzem a zahrne přímo do výsledného procenta; její rozpad se v tabulce neopakuje.
- Obratové podmínky a názvy tarifů zůstávají v datovém exportu, ale nepřehlcují hlavní tabulku. Pokud má poskytovatel více tarifů pro stejnou metodu, dashboard ukáže jeho nejnižší ověřenou veřejnou variantu.
- Měsíční paušál je oddělený ve sloupci **Další poplatky**, nikoli u názvu poskytovatele.
- **Individuální nabídka** zůstane bez vymyšleného čísla.

## Co dashboard obsahuje

- karty a A2A převody jako oddělené řádky; karetní peněženky se nepovažují za A2A,
- mapa celé EU/EHP obarvená podle sazby (paleta #003f5c → #ffa600, tmavá = levnější),
- kontextové srovnání - klikneš na zemi nebo poskytovatele, teprve pak se rozbalí tabulka,
- nastavitelná částka transakce (výchozí 1000 Kč), žádný zbytečný měsíční kalkulátor,
- reálná sazba pro zadanou transakci: procentní poplatek plus pevná část převedená kurzem se vydělí částkou transakce a zobrazí jako jedno výsledné procento,
- veřejný zdroj u každého řádku s odkazem přímo na konkrétní oficiální stránku,
- týdenní historii a export CSV,
- bezpečný režim: při nefunkčním nebo nejistém parseru se zachová poslední ověřená sazba a označí se ke kontrole (v UI jako jednoduchý stavový puntík, ne syrový interní text).

## Jak se s dashboardem pracuje

Mapa i srovnávací tabulka jsou **kontextové** - ve výchozím stavu vidíš jen mapu a KPI. Klikneš na zemi → tabulka se rozbalí pro tu zemi. Klikneš na jméno poskytovatele v tabulce → přepneš do srovnání toho poskytovatele napříč všemi zeměmi, kde působí (mapa zvýrazní jen ty). Tlačítko "zpět na přehled" nebo druhý klik na stejnou zemi tě vrátí na začátek.

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

U karet se interně ukládají oddělené komponenty. Dashboard je ale nesype uživateli do tabulky: pro Adyen spojí processing fee, 0,60% acquiring markup a evropskou referenci 0,20 % pro spotřebitelskou debetní nebo 0,30 % pro kreditní kartu do jednoho výsledného rozmezí pro zadanou částku. Proměnlivé scheme fees zůstávají součástí metodické poznámky v datech, ne samostatným textem v přehledu; výsledné číslo je proto srovnávací reference, ne smluvní nabídka.

## Nahrání na GitHub

1. Vytvoř nový **public** repozitář, například `Acquirer-Fee-Tracker-EU`.
2. Nahraj **obsah této složky**, ne nadřazenou složku ze ZIPu.
3. Otevři `Settings → Pages` a nastav `Deploy from a branch`, větev `main`, složka `/docs`.
4. V `Actions` spusť `Weekly acquirer fee update` přes `Run workflow`, aby se ověřily zdroje a aktuální kurz.
5. Web bude na adrese `https://tesskosnar.github.io/Acquirer-Fee-Tracker-EU/`.

## Důležitá metodická poznámka

Veřejná cena platební brány není vždy totéž jako čistý acquiring. Dashboard proto viditelně rozlišuje `Acquirer`, `PSP`, `A2A` a `jen brána`. Technické brány bez acquiringu se nevydávají za acquirera a neveřejná cena se nenahrazuje odhadem.
