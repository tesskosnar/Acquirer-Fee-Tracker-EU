# Merchant Fee Tracker

Interaktivní GitHub Pages dashboard pro srovnání veřejných cen acquiringu a platební akceptace napříč EU/EHP, Spojeným královstvím a Švýcarskem.

## Původ dat

Původní rozšířený dataset obsahoval řádky vytvořené AI, které nebyly všechny ověřeny jednotlivě a nelze je považovat za spolehlivý zdroj. Revize proto začala znovu od CEE a stejným postupem už prošly i ostatní země EU/EHP, Spojené království a Švýcarsko: nejprve vznikne nezávislý seznam lokálních acquirerů pro každou zemi a až potom se porovnává s databází.

Cena se doplní jen z oficiálního veřejného zdroje poskytovatele. Kontrola nekončí na stránce „Pricing“: prochází také FAQ, sazebníky a právní dokumenty, PDF, kalkulátory a lokální jazykové podstránky. Když skutečnou obchodní sazbu poskytovatel nezveřejňuje, zůstává jako `Individuální nabídka` bez vymyšleného čísla. Starší řádky, které touto revizí ještě neprošly, se ve veřejném dashboardu nezobrazují a nevstupují do mapy ani souhrnných statistik.

Pro objevování dalších jmen se používají i kvalitní oborové zdroje, zejména [Business of Payments](https://businessofpayments.substack.com/). Slouží pouze jako discovery zdroj: zařazení, země působnosti a sazba se vždy znovu potvrzují na oficiálních stránkách poskytovatele. Vedle CEE registru existuje samostatný ověřený registr pro zbývající EU/EHP, Švýcarsko a Spojené království.

## Jak se cena zobrazuje

- Dashboard ukazuje jeden sloupec **Celková sazba** pro jednotnou referenční transakci 20 EUR.
- Pevná transakční složka se převede kurzem a zahrne přímo do výsledného procenta; její rozpad se v tabulce neopakuje.
- Odlišné produkty, online/POS kanály, karetní profily a cenové modely zůstávají jako samostatné řádky. Slučují se jen ekonomicky totožné záznamy.
- Měsíční paušál je vidět ve sloupci **Další poplatky**. Pokud ceník neposkytuje vlastní objemový základ, nabídka nevstupuje do pořadí podle celkové sazby; paušál ani jednorázová aktivace či hardware se svévolně nerozpouštějí do transakce. Výjimkou jsou balíčky s veřejným limitem, z něhož lze efektivní sazbu přímo odvodit.
- **Individuální nabídka** zůstane bez vymyšleného čísla.
- U tarifního rozmezí se řazení, medián a výběr nejlevnější nabídky opírají o horní hranici. Dashboard tak neprezentuje nejlepší obratové pásmo jako obecně dostupnou cenu.
- Znak `≈` označuje modelovaný výsledek (například dopočet IC++ nebo plné využití veřejného balíčku), nikoli přesnou garantovanou sazbu.

## Co dashboard obsahuje

- karty a A2A převody jako oddělené řádky; karetní peněženky se nepovažují za A2A,
- mapa EU/EHP, Spojeného království a Švýcarska obarvená podle konzervativní horní hranice sazby; Island, Malta a Kypr mají samostatné mapové přepínače,
- kontextové srovnání - kliknutí na zemi nebo poskytovatele zúží už zobrazenou tabulku,
- jednotná referenční transakce 20 EUR; fixní poplatek se rovnou promítá do výsledné procentní sazby,
- reálná sazba pro referenční transakci: procentní poplatek plus pevná část převedená kurzem se vydělí 20 EUR a zobrazí jako jedno výsledné procento,
- veřejný zdroj u každého řádku s odkazem přímo na konkrétní oficiální stránku,
- týdenní historii a export CSV,
- bezpečný režim: při nefunkčním nebo nejistém parseru se zachová poslední ověřená sazba a označí se ke kontrole (v UI jako jednoduchý stavový puntík, ne syrový interní text).

## Jak se s dashboardem pracuje

Mapa i srovnávací tabulka jsou **kontextové**. Ve výchozím stavu vidíš celkový přehled; kliknutí na zemi tabulku zúží na daný trh. Kliknutí na jméno poskytovatele přepne do srovnání tohoto poskytovatele napříč zeměmi, kde působí. Filtr lze zrušit tlačítkem nad tabulkou.

## Automatická aktualizace

`.github/workflows/weekly-update.yml` běží každé pondělí. Skript:

1. stáhne poslední dostupné kurzy ČNB,
2. u zdrojů napojených na monitoring kontroluje dostupnost a obsahový hash,
3. u omezené skupiny podporovaných ceníků zkusí vytěžit sazby z definovaných struktur nebo textových kotev,
4. nové hodnoty přijme pouze při dostatečné důvěře parseru,
5. přepočítá náklady, uloží `latest.json`, CSV, change log a časově označený neměnný snapshot,
6. změny commitne zpět do repozitáře.

### Časové údaje nejsou totéž

- `generated_at` na kořeni JSON je pouze okamžik sestavení/exportu datasetu.
- `source_checked_at` u nabídky je čas posledního úspěšného načtení konkrétního zdroje. Při offline buildu, chybě zdroje ani u ručně udržovaného řádku se nepřepisuje časem buildu.
- `price_verified_on` je kalendářní den, kdy byla sazba a její podmínky skutečně věcně ověřena. Datum je oddělené od volného textu `verification`; pokud historický záznam neobsahuje dostatečně přesný důkaz, zůstává prázdné.
- `source_last_attempt_at` a `source_last_attempt_status` popisují poslední pokus. Selhání tedy nepřepíše poslední dobrou cenu ani se neschová za starý úspěšný stav.
- `verification_state` je pevný stav používaný aplikací; věta v `verification` už nerozhoduje o tom, zda je řádek ověřený.

Záměrně se neukládá smyšlený čas ruční kontroly: pokud známe jen den, pole `price_verified_on` obsahuje jen datum `YYYY-MM-DD`.

### Adyen: kontrola po zemích a podle skutečného typu metody

Adyen je výjimka z obecného textového parseru. Jeho ceník mění processing fee a dostupné metody podle zvolené země, proto se už nekontroluje jedním globálním regulárním výrazem. Aktualizace kombinuje:

- vložená country/region data oficiálního ceníku (např. Evropa = 0,11 EUR processing fee),
- veřejný endpoint používaný samotnou stránkou Adyenu pro přesnou dostupnost metody v jednotlivých zemích,
- oficiální typ metody (`Online banking`, `Direct debit`, `Cards`), takže A2A nezmizí jen proto, že její název neobsahuje „A2A“,
- konkrétní payment-method fee z ceníkové tabulky.

U karet se interně ukládají oddělené komponenty. Pro srovnání IC++ nabídky používá dashboard jednotný profil autentizované EEA spotřebitelské debetní karty (a domácí UK debetní karty) při transakci `20 €`. Interchange je `0,20 %`; scheme fee se modeluje jako dvě skutečné varianty Visa Debit a Mastercard Debit včetně procentní i pevné složky. Pro EEA jde o `0,116 % + 0,035 €` až `0,121 % + 0,045 €`, podle [veřejné tabulky Paybyrd](https://www.paybyrd.com/pricing/scheme-fees). Výsledek je označen `≈`, protože Paybyrd publikuje indikativní pozorovaný benchmark, nikoli garantovanou cenu konkrétního acquirera. Nepřidává se žádná plošná přirážka za CEE nebo ne-eurovou měnu; případné měnové či přeshraniční poplatky se započtou jen tehdy, když je uvádí konkrétní ceník. UK a Švýcarsko používají vlastní explicitně zdrojované domácí reference.

Pole `variable_pct_min` a `variable_pct_max` vždy znamenají pouze procentní složku publikovanou poskytovatelem (`variable_pct_basis = provider_published`). Modelované interchange a scheme složky zůstávají oddělené v `pricing_components.comparison_reference` a přičítají se až do výsledné srovnávací sazby.

Fixní transakční poplatky se převádějí do výsledné efektivní procentní sazby na jednotné referenční transakci `20 EUR`. U A2A se zveřejní číslo jen tehdy, když lze z oficiálního ceníku sečíst processing fee a celou cenu konkrétní platební metody.

## Nahrání na GitHub

1. Vytvoř nový **public** repozitář, například `Acquirer-Fee-Tracker-EU`.
2. Nahraj **obsah této složky**, ne nadřazenou složku ze ZIPu.
3. Otevři `Settings → Pages` a nastav `Deploy from a branch`, větev `main`, složka `/docs`.
4. V `Actions` spusť `Weekly acquirer fee update` přes `Run workflow`, aby se ověřily zdroje a aktuální kurz.
5. Web bude na adrese `https://tesskosnar.github.io/Acquirer-Fee-Tracker-EU/`.

## Důležitá metodická poznámka

Veřejná cena platební brány není vždy totéž jako čistý acquiring. Dashboard proto viditelně rozlišuje `Acquirer`, `PSP`, `A2A` a `jen brána`. Technické brány bez acquiringu se nevydávají za acquirera a neveřejná cena se nenahrazuje odhadem.
