# trustedDialog Snippet-Checker

Streamlit-App zur Prüfung, ob das trustedDialog-Preview-Snippet (Bilder / Gutscheincode)
korrekt in eine Kunden-Mail eingebunden wurde. Einfach eine `.eml`-Datei per Drag & Drop
hochladen – die App findet den `<script type="application/ld+json">`-Block und prüft ihn
gegen die UIM-Spezifikation (Stand Juni 2026).

## Lokal starten

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deployment auf Streamlit Community Cloud (wie bei der bestehenden App der Kollegin)

1. Dieses Verzeichnis (app.py + requirements.txt) in ein GitHub-Repo pushen
2. Auf https://share.streamlit.io ein neues Deployment anlegen und auf app.py zeigen
3. Fertig – die App bekommt eine URL wie `xyz.streamlit.app`

## Was wird geprüft?

- Ist der JSON-LD-Script-Block überhaupt im HTML-Teil der Mail vorhanden?
  (inkl. Erkennung, falls das Script vom Versandsystem als Text escaped wurde)
- Ist der Inhalt gültiges JSON? Häufigster Fehler: die `//`-Kommentare aus der
  offiziellen Vorlage wurden nicht entfernt → JSON technisch ungültig.
- Struktur: `PromotionCard` / `DiscountOffer` mit korrektem `@context` und `@type`
- Pflichtfelder: mind. 1 Bild mit https-Link, `discountCode` (2–50 Zeichen, keine Emojis)
- Optionale Felder: `description` (max. 50 Zeichen), `availabilityStarts`/`Ends`
  (ISO-8601 mit Zeitzone, inkl. Countdown-Hinweis ab 3 Tagen vor Ablauf)
- Bonus: DKIM-Signatur vorhanden & Domain-Alignment zur From-Domain
- **Live-Bildcheck (Button je Snippet):** ruft jede Bild-URL tatsächlich ab und prüft
  Erreichbarkeit, Content-Type, echtes Bildformat und Pixel-Maße gegen die drei
  spezifizierten Auflösungen (1088×464 / 532×352 / 348×260 px). Deckt Fälle ab, in denen
  das JSON-LD selbst korrekt ist, das referenzierte Bild aber nicht (totes CDN, falsches
  Format, falsche Auflösung, Hotlink-Schutz).

## Testdateien

Im Ordner `test_emails/` liegen Beispiel-EMLs zum Ausprobieren:
- `test_korrekt.eml` – sauber eingebundenes, minimales Snippet
- `test_mit_kommentaren.eml` – enthält den häufigsten Kundenfehler (Vorlagen-Kommentare
  nicht entfernt)
- `beispiel_kunde_funktionierend.eml` / `beispiel_kunde_fehlerhaft.eml` – echtes
  Kundenbeispiel (RB Leipzig). Das JSON-LD ist in **beiden** Mails syntaktisch korrekt;
  der Unterschied steckt in den referenzierten Bild-URLs (anderes CDN ohne Dateiendung) -
  genau der Fall, für den der Live-Bildcheck gedacht ist.
