# Top-100 Krypto-Signal-Agent

Automatische technische Analyse der täglichen CoinGecko-Top-100 nach Marktkapitalisierung. Kein Handel, keine Wallet-Verbindung und keine privaten Börsen-API-Schlüssel.

## Aktivierung

1. Repository **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`: Token des selbst angelegten Telegram-Bots (BotFather).
   - `TELEGRAM_CHAT_ID`: Zielchat. Bot dort hinzufügen bzw. zunächst `/start` senden.
   - Optional `COINGECKO_API_KEY`: CoinGecko **Demo** API-Key; öffentlicher Zugriff kann rate-limitiert werden.
2. GitHub Actions muss aktiviert sein. Der Workflow verwendet `contents: write` zum Speichern der Duplikatsperre. Branch-Regeln dürfen den State-Commit nicht blockieren.
3. **Actions → Daily crypto signals → Run workflow**: zunächst `dry_run=true`, Bericht prüfen; anschliessend `dry_run=false` für produktiven Scan. Ohne starkes neues Signal kommt absichtlich keine Telegram-Nachricht.
4. Tägliche Ausführung läuft automatisch. Bot-Token und Chat-ID niemals in Issues, Code, Logs oder Chat kopieren.

Ein Push des Agenten oder Signal-Workflows startet automatisch einen vollständigen **Live-Daten-Test ohne Telegram und ohne State-Änderungen**. Der Test-Workflow prüft zusätzlich alle Unit-Tests. Eine grüne Ausführung allein bestätigt nicht die Telegram-Zustellung.

## Daten und Qualität

- CoinGecko `/coins/markets`: exakt 100 eindeutige Coin-IDs, USD, `market_cap_desc`. Stablecoins bleiben Teil der Top 100; für sie entstehen üblicherweise keine starken technischen Signale.
- CoinGecko `/coins/{id}/tickers`: Coin-ID-Zuordnung zu Binance-/Kraken-Spotmärkten in USD, USDT oder USDC. Keine Zuordnung allein anhand möglicherweise mehrdeutiger Symbole. Anomale und veraltete Ticker werden verworfen. Erste Ticker-Seite und maximal acht passende Märkte begrenzen den Aufwand.
- Börsen-Marktkatalog bestätigt das Handelspaar. Binance `/api/v3/klines` (`1d`, 400 Kerzen), alternativ Kraken `/0/public/OHLC` (1440 Minuten, bis 720 Kerzen). OHLCV und Volumen stammen immer aus demselben Markt; Basisvolumen wird relativ zum eigenen 20-Tage-Mittel verglichen.
- Nur **abgeschlossene UTC-Tageskerzen**, mindestens 260 zusammenhängende Tage, letzte Kerze zwingend gestern. Offene und zukünftige Kerzen werden entfernt. Preis-/Volumengültigkeit, UTC-Ausrichtung, Datenlücken, widersprüchliche Duplikate und Aktualität werden geprüft.
- Börsenlimits, regionale Binance-Sperren, nicht gelistete Coins, kurze Historie oder CoinGecko-Limits können die vollständige Abdeckung verhindern. Keine erfundenen Kurse oder Ersatzsignale. Interne Datenlücken stehen im Workflow-Artefakt. Bei weniger als 50 analysierbaren Coins schlägt der Lauf fehl und sendet nichts. `partial` bedeutet ausdrücklich **keine vollständige Top-100-Analyse**, auch wenn der Workflow technisch erfolgreich ist.
- Öffentliche Datenabfragen verwenden Timeout, begrenzte Wiederholungen und CoinGecko-Abstände. Ein Demo-Key verbessert die Zuverlässigkeit. Die gesamte Marktauswahl ist während des Scans zeitversetzt.

## Wellenanker-inspirierter Score

Eine eigenständige, transparente Heuristik, keine offizielle oder nachgewiesen profitable Wellenanker-Strategie. LONG und SHORT werden spiegelbildlich aus abgeschlossenen Kerzen berechnet.

| Komponente | Punkte | Bedingung für LONG (SHORT umgekehrt) |
|---|---:|---|
| EMA-Ausrichtung | 20 | Schluss > EMA20 > EMA50 > EMA100 > EMA200 |
| EMA-Richtung | 10 | EMA20 und EMA50 höher als vor fünf Tagen |
| Marktstruktur | 20 | Die letzten zwei bestätigten Swing-Hochs und Swing-Tiefs steigen; 90 Tage, jeweils zwei Kerzen links/rechts |
| Volumen | 15 / 8 / 0 | Volumen ≥1,5 / ≥1,2 / darunter im Verhältnis zum Mittel der 20 Vortage |
| Ausbruch | 20 | Schluss über den Hochs der 20 Vortage plus positive Tageskerze |
| Retest | 10 | In den fünf Vortagen erfolgter Schlussausbruch; aktuelle Kerze berührt die Ausbruchszone (0,5% Toleranz), hält sie und schliesst positiv; alle Zwischenkerzen halten die Zone |
| Liquiditäts-/Risikoabstand | 5 | Abstand zur letzten bestätigten Swing-Tief-Referenz zwischen 0,5 und 3 ATR14 |

Gesamt 0–100. Liquiditätszonen sind technische Hoch-/Tief-Referenzen, keine tatsächlichen Orderbuchdaten. EMA wird mit dem einfachen Mittel der ersten Periodenwerte initialisiert, danach rekursiv berechnet. ATR14 ist der einfache Mittelwert der letzten 14 True Ranges. Preise sind in der jeweiligen Quote-Währung, USDT/USDC können von USD abweichen.

Nur Score **≥75**, bestätigter Ausbruch **oder** Retest und positive Distanz zur Stop-Referenz sind zulässig. Telegram und Signal-Liste enthalten ausschliesslich neue starke Signale. Stop-Referenz ist ein Strukturwert, keine garantierte Ausführung oder Positionsgrössenempfehlung. SHORT beschreibt eine bearishe Spot-Analyse, nicht automatisch einen handelbaren Short.

## Duplikatsperre und Zustellung

`.state/signals.json` ist der dauerhafte, nicht geheime State im Default-Branch. Pro Coin werden letzter abgeschlossener Tag und aktive starke Richtungen gespeichert. Eine durchgehend starke LONG-/SHORT-Phase erzeugt nur eine Meldung; nach einer erfolgreich analysierten schwachen Tageskerze darf eine neue starke Phase gemeldet werden. Derselbe oder ein älterer Tag erzeugt nie erneut eine Meldung. Datenfehler setzen die Phase nicht zurück. Ältere Coin-States bleiben beim Wechsel der Top 100 erhalten.

Alle Benachrichtigungen werden **vor** Telegram dauerhaft reserviert. Der gesamte Signal-Workflow wird über GitHub-Concurrency serialisiert; State-Updates verwenden SHA-Vergleich und werden bei Fehlern nicht wiederholt. Fehlender/ungültiger State blockiert produktive Meldungen. Den State nicht löschen oder zurücksetzen, sonst geht die historische Duplikatsperre verloren.

Telegram bietet für `sendMessage` keine Idempotenzschlüssel. Deshalb wird jede Nachricht nur einmal versucht. Bei Timeout oder Prozessabbruch **können Meldungen verloren gehen**, obwohl sie reserviert sind; dafür gibt es keinen automatischen erneuten Versand und keine Doppelmeldungen durch Workflow-Neustarts. Der Lauf schlägt bei Versandfehler fehl. Mehrere Nachrichten pro Lauf sind möglich. Garantierte Exactly-once-Zustellung ist mit Telegram nicht möglich.

## Zeitplan und Betrieb

Zwei UTC-Crons: 06:00 und 07:00. Ein `Europe/Zurich`-Gate lässt im Sommer nur 06:00 UTC und im Winter nur 07:00 UTC zu: jeweils **08:00 Schweizer Zeit**. Sommer-/Winterwechsel werden über die Python-Zeitzonendaten berechnet. Das Gate verwendet die auslösende Cron-Identität und toleriert verzögerte Runnerstarts. GitHub kann geplante Jobs verzögern oder auslassen; dies ist kein Echtzeit-SLA. Extreme Verzögerungen über einen Datumswechsel können die Tageszuordnung verschieben. Bei öffentlichen Repositories können inaktive Zeitpläne nach 60 Tagen deaktiviert werden; erfolgreiche State-Commits erzeugen regelmässige Aktivität.

- Laufbericht: Actions → Ausführung → `scan-report-…` (`latest.json`); 30 Tage Aufbewahrung.
- Bericht enthält Abdeckungsstatus, interne Datenlücken, neue starke Signale mit Score-Komponenten und Anzahl bestätigter Telegram-Antworten.
- Fehlende Telegram-Secrets führen zum sichtbaren Fehlschlag statt einem scheinbar produktiven Erfolg.
- Keine Nachricht ohne neue starke Signale. Der Agent sendet auch keine täglichen Nullsignal-/Statusmeldungen.
- Keine Renditevalidierung oder Backtests durchgeführt. Vor Vertrauen in die Signale Paper-Trading und unabhängige Strategieprüfung durchführen.

## Lokal testen

Python 3.12, nur Standardbibliothek, keine zusätzlichen Pakete:

```sh
python -m unittest discover -s tests -v
python -m compileall -q agent.py tests
python agent.py --dry-run
```

Für produktive GitHub-State-Verwendung zusätzlich `GITHUB_REPOSITORY`, `GITHUB_TOKEN`, Telegram-Secrets. Keine Secrets in Shell-History oder Kommandozeilenargumente schreiben.
