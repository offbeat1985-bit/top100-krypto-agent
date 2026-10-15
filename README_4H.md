# Krypto-Agent: Top 100 ohne Stablecoins, 4H

Keine Orders, Wallets oder privaten Börsen-API-Schlüssel. Vier Quellen: Binance, Kraken, OKX und KuCoin. Nur starke neue LONG-/SHORT-Signale ab 75 Punkten werden gemeldet.

## Auswahl und Kerzen

CoinGecko wählt die grössten 100 Coins nach Marktkapitalisierung nach Ausschluss der Stablecoins. Die Liste wird mit nachfolgenden Coins aufgefüllt. Coin-IDs stammen aus den aktuellen Stablecoin-Kategorien einschliesslich algorithmischer und renditetragender Varianten. Governance-/Emittenten-/Protokoll-Kategorien werden nicht pauschal ausgeschlossen. Fehlende oder leere Klassifikation stoppt den Lauf. Falsche oder verspätete CoinGecko-Kategorisierung kann weiterhin falsche Ein-/Ausschlüsse verursachen; keine Symbol-/Preisnähe-Heuristik.

Nur abgeschlossene UTC-4H-Kerzen: Binance `4h`, Kraken 240 Minuten, OKX `4H` plus `confirm=1`, KuCoin `4hour`. Mindestens 260 zusammenhängende abgeschlossene Kerzen (etwa 43 Tage). Letzte Kerze muss zum letzten abgeschlossenen Slot gehören; offene und zukünftige Kerzen werden entfernt. Preise, Volumen, Lücken, Konflikte und UTC-Ausrichtung werden geprüft. Keine erfundenen Ersatzdaten.

Ticker-Zuordnung über CoinGecko-Coin-ID und bestätigten Börsen-Marktkatalog. Maximal drei Ticker-Seiten, USD/USDT/USDC, keine veralteten/anomalen Ticker. Bis drei Märkte je Börse werden abwechselnd versucht. OHLCV wird nicht zwischen Börsen/Paaren vermischt. Quotes können von USD abweichen.

## Analyse

EMA20/50/100/200 (Ausrichtung 20, Steigung 10), bestätigte Marktstruktur (20), Volumen (15), Ausbruch (20), Retest (10), Liquiditäts-/Risikoabstand (5): insgesamt 0–100. Alle Perioden beziehen sich jetzt auf 4H, nicht auf Tage. EMA200 entspricht etwa 33 Tagen. Swing-Struktur: 90 Kerzen, zwei Bestätigungskerzen rechts/links; Ausbruch gegen 20 Vorkerzen, Retest innerhalb fünf Vorkerzen bei 0,5% Toleranz; ATR14 ist einfacher Mittelwert der True Ranges. Nur >=75 plus Ausbruch/Retest und positive Distanz zur Struktur-Stopreferenz sind zulässig. Liquiditätszonen sind technische Preisreferenzen, keine Orderbuchdaten. SHORT ist eine bearishe Spot-Analyse.

4H erzeugt möglicherweise mehr Rauschen und Signalwechsel. Keine Backtests oder Renditevalidierung der 4H-Strategie. Die Schwelle75 wurde beibehalten.

## Beschleunigung und Cache

Drei parallele Arbeiter, gemeinsame Limits pro Anbieter: CoinGecko mindestens 2,2 Sekunden, andere Anbieter mindestens 0,4 Sekunden zwischen Requests. Anbieter können strengere Limits haben. Begrenzte Wiederholungen bei HTTP429/5xx, Timeout und gemeinsame Wartezeiten. Bei HTTP403/451 wird derselbe Endpunkt für den Rest des Laufs nicht erneut versucht; andere Quellen bleiben nutzbar. Falls ein Fehler nur einzelne Märkte betrifft, kann dies vorübergehend Abdeckung kosten.

Nicht geheimer Cache `.cache/crypto4h-v1`: Ticker/Marktkataloge maximal sechs Stunden, Kerzenhistorie maximal14 Tage je Börse/Paar. Top-100-Auswahl und Stablecoin-Klassifikation werden pro Lauf neu abgerufen. Bei warmem Cache nur neue Kerzen plus Überlappung nachladen, vollständige Historie erneut prüfen. Beschädigte/abgelaufene Cache-Einträge werden verworfen. Marktkataloge/Ticker können während ihrer sechs Stunden veralten; Kursaktualität wird trotzdem geprüft. Maximal500 abgeschlossene Kerzen werden behalten; rollende Historie kann minimale EMA-Initialisierungsabweichungen erzeugen.

GitHub Actions stellt den Daten-Cache zwischen Läufen wieder her. Er ist verzichtbar; erster Lauf ohne Cache ist langsamer. Keine garantierte Scan-Dauer oder vollständige Abdeckung. Bei noch veralteter letzter Kerze maximal zwei erneute Versuche nach je15 Sekunden; Qualitätsregeln werden nicht gelockert.

## Zeitplan und Betrieb

Cron `1 */4 * * *`: **00:01, 04:01, 08:01, 12:01, 16:01, 20:01 UTC**. Schweizer Winter:01:01/05:01/09:01/13:01/17:01/21:01; Sommer:02:01/06:01/10:01/14:01/18:01/22:01. UTC-Kerzengrenzen bleiben unverändert. GitHub kann verzögert starten oder Läufe auslassen. Versand erst nach Abschluss des Scans, nicht garantiert eine Minute nach Kerzenschluss. Analyse verwendet den letzten abgeschlossenen Slot zum tatsächlichen Scanstart.

Workflow serialisiert mit `cancel-in-progress: false`. GitHub kann einen bereits wartenden Lauf durch einen neueren wartenden Lauf ersetzen; keine Garantie für die Abarbeitung jedes Slots.

Secrets bleiben gültig: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional CoinGecko-Demo-Key `COINGECKO_API_KEY`. In Actions → **4H crypto signals → Run workflow** Dry-Run für Tests aktiviert lassen. Produktion: Dry-Run deaktivieren. Push-Läufe immer Dry-Run; planmässige Läufe produktiv. Ohne starkes neues Signal keine Telegram-Meldung.

**`.state/signals.json` nicht löschen/zurücksetzen/überschreiben.** State bleibt Version1. Neue Schlüssel `4h:<coin-id>` trennen den Zeitrahmen; alte Tages-Schlüssel bleiben erhalten. Beim ersten 4H-Lauf wird eine alte aktive starke Richtung übernommen, um beim Wechsel keine erneute Meldung desselben Signals zu erzeugen. Nach einer schwachen erfolgreich analysierten 4H-Kerze darf eine neue starke Phase gemeldet werden. Kein erneuter Versand derselben/älteren Kerze. Datenfehler setzen aktive Phasen nicht zurück.

Reservierung vor Telegram-Versand, keine automatischen Versandwiederholungen bei Fehler/Timeout. Meldungen können dadurch verloren gehen; Telegram unterstützt keine garantierte Exactly-once-Zustellung. Der State ist vom löschbaren Daten-Cache unabhängig.

Weniger als50 analysierte Coins: Lauf fehlgeschlagen, kein Versand/State-Update. `partial` bedeutet unvollständige Top-100-Abdeckung trotz grüner Action. Scan-Artefakt enthält Timeframe, Quellenanzahl, Abdeckung, Laufdauer, neue starke Signale und Fehler. Kein Produktivbetrieb allein durch bestandene Tests bewiesen.

## Prüfung der Umstellung

46 lokale Tests bestanden; Python kompiliert; beide Workflows mit YAML-Parser geprüft. Tests umfassen Stablecoin-Ausschluss/Auffüllen, 4H-Grenzen, Indikatoren, vier Adapter, Pagination, Cache/inkrementelles Nachladen, Zugriffssperren, State-Migration, Duplikatschutz und Versandablauf. Datenanbieter wurden mit simulierten Antworten geprüft. Direkte Live-Abfragen scheiterten am Netzwerkzugang der Arbeitsumgebung. Neue Live-Abdeckung/Laufzeit/Telegram-Zustellung sind noch auf GitHub zu prüfen. Keine Trades ausgeführt.
