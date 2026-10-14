# Prüfstand 14.10.2026

- 26 automatisierte Tests erfolgreich (`python -m unittest discover -s tests -v`).
- Python kompiliert ohne Fehler.
- Beide GitHub-Workflow-Dateien mit YAML-Parser geprüft; Trigger, Zeitplan, Secrets, Concurrency und Sicherheitsbedingungen zusätzlich im Unit-Test geprüft.
- Datenabruf mit simulierten CoinGecko-, Binance- und Kraken-Antworten getestet: exakte Top-100-Liste, Coin-ID-Zuordnung, Binance/Kraken-Fallback, Volumen, HTTP-429-Wiederholung.
- Kerzenvalidierung, EMA20/50/100/200, bestätigte Pivots, Long-/Short-Ausbrüche und Retests, Score-Summen und Schwelle 75 getestet.
- Vollständiger Ablauf mit simulierten Daten getestet: Persistenz vor Versand, Versandabbruch ohne Wiederholung, unterdrückte Duplikate nach Neustart, Dry-Run ohne Persistenz/Telegram und Stop bei zu geringer Abdeckung.
- Sommer-/Winterzeit einschliesslich Umstellungstagen für Europe/Zurich getestet.
- Live-Daten-Dry-Run gestartet, aber fehlgeschlagen: `Data provider unavailable after five attempts`. Der lokale Netzwerkproxy war bereits beim Git-Clone nicht erreichbar. Deshalb wurde kein echter Marktdatenabruf bestätigt.
- GitHub-Veröffentlichung per Contents-API und Git-Blob-API abgelehnt: HTTP 403 `Resource not accessible by integration`. Auch nach Ankündigung einer Zugriffserweiterung wurde ein erneuter Schreibversuch noch abgelehnt.
- GitHub-Actions-Status über API geprüft: `total_count=0`, keine Läufe.
- Keine Telegram-Zustellung getestet; keine produktive Ausführung bestätigt. Keine Käufe oder Verkäufe.

## Noch erforderlich

Berechtigten GitHub-Zugang mit Schreibrecht für Repository-Inhalte und Workflows freischalten, Dateien veröffentlichen, automatischen Push-Dry-Run prüfen. Telegram-Bot/Chat einrichten und die GitHub-Secrets `TELEGRAM_BOT_TOKEN` und `TELEGRAM_CHAT_ID` setzen. Anschliessend produktiven manuellen Workflow (`dry_run=false`) ausführen und State-Persistenz sowie tatsächliche Telegram-Zustellung prüfen. Kein starkes Signal bedeutet absichtlich keine Nachricht.
