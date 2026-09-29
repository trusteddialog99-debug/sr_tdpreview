import streamlit as st
import email
from email import policy
from email.parser import BytesParser
import re
import json
import html as htmllib
import io
from datetime import datetime, timezone

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Laut Spezifikation zulässige Bildauflösungen (Breite, Höhe), je nach Anzahl Bilder
EXPECTED_SIZES = {
    1: (1088, 464),
    2: (532, 352),
    3: (348, 260),
}
ALL_EXPECTED_SIZES = set(EXPECTED_SIZES.values())

st.set_page_config(page_title="trustedDialog Snippet-Checker", page_icon="✅", layout="wide")

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

SCRIPT_TAG_RE = re.compile(
    r"<script([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
LD_JSON_ATTR_RE = re.compile(r"ld\+json", re.IGNORECASE)
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\u2700-\u27BF"
    "\u2600-\u26FF"
    "]+",
    flags=re.UNICODE,
)


def parse_eml(file_bytes):
    return BytesParser(policy=policy.default).parsebytes(file_bytes)


def get_domain(addr_header):
    if not addr_header:
        return None
    m = re.search(r"@([\w\.-]+)", addr_header)
    return m.group(1).lower() if m else None


def extract_html_parts(msg):
    """Liefert Liste von (html_string, part_beschreibung)."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html":
                try:
                    payload = part.get_content()
                except Exception:
                    raw = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    payload = raw.decode(charset, errors="replace")
                parts.append((payload, ctype))
    else:
        if msg.get_content_type() == "text/html":
            try:
                payload = msg.get_content()
            except Exception:
                raw = msg.get_payload(decode=True) or b""
                charset = msg.get_content_charset() or "utf-8"
                payload = raw.decode(charset, errors="replace")
            parts.append((payload, "text/html"))
    return parts


def find_json_ld_scripts(html_text):
    """Findet <script type=application/ld+json> Blöcke inkl. Position."""
    found = []
    for m in SCRIPT_TAG_RE.finditer(html_text):
        attrs, content = m.group(1), m.group(2)
        if LD_JSON_ATTR_RE.search(attrs):
            found.append({
                "attrs": attrs.strip(),
                "raw_content": content,
                "start": m.start(),
                "end": m.end(),
            })
    return found


def detect_escaped_script(html_text):
    """Prüft, ob das Script evtl. HTML-encodiert (escaped) im Body steckt."""
    unescaped = htmllib.unescape(html_text)
    if "ld+json" in unescaped.lower() and "ld+json" not in html_text.lower():
        return True
    return False


def strip_comment_only_lines(json_text):
    """Entfernt Zeilen, die (getrimmt) mit '//' beginnen - wie im UIM-Template.
    Zeilen mit https:// etc. werden NICHT angefasst, da dort '//' nicht am
    Zeilenanfang steht."""
    lines = json_text.splitlines()
    cleaned = []
    had_comments = False
    for line in lines:
        if line.strip().startswith("//"):
            had_comments = True
            continue
        cleaned.append(line)
    return "\n".join(cleaned), had_comments


def try_parse_json(raw_content):
    """Versucht JSON zu parsen. Gibt (data, had_comments, error) zurück."""
    # 1. direkter Versuch
    try:
        data = json.loads(raw_content)
        return data, False, None
    except json.JSONDecodeError:
        pass
    # 2. Kommentarzeilen entfernen und erneut versuchen
    cleaned, had_comments = strip_comment_only_lines(raw_content)
    try:
        data = json.loads(cleaned)
        return data, had_comments, None
    except json.JSONDecodeError as e:
        return None, had_comments, str(e)


def is_valid_https_url(url):
    return isinstance(url, str) and url.strip() != "" and url.strip().lower().startswith("https://")


def check_emojis(text):
    return bool(EMOJI_RE.search(text or ""))


def parse_iso8601(value):
    if not value:
        return None, None
    v = value.strip()
    # Python benötigt exaktes Format, +02:00 wird unterstützt ab 3.11 auch ohne Doppelpunkt
    try:
        dt = datetime.fromisoformat(v)
        return dt, None
    except ValueError as e:
        return None, str(e)


def analyze_json_ld(data):
    """Prüft die geparste JSON-LD Struktur gegen die trustedDialog Spec.
    Gibt Liste von (level, titel, text) zurück. level: 'ok' | 'warn' | 'error'
    Und ein dict mit den extrahierten Werten fürs Report."""
    results = []
    extracted = {"images": [], "discount": None}

    if not isinstance(data, list):
        results.append(("error", "Struktur",
                         "Das JSON-LD muss laut Spezifikation ein Array [ ... ] aus Objekten sein. "
                         f"Gefunden wurde stattdessen: {type(data).__name__}."))
        return results, extracted

    promotion_cards = []
    discount_offers = []
    unknown_objects = []

    for obj in data:
        if not isinstance(obj, dict):
            unknown_objects.append(obj)
            continue
        otype = obj.get("@type", "")
        if otype == "PromotionCard":
            promotion_cards.append(obj)
        elif otype == "DiscountOffer":
            discount_offers.append(obj)
        elif otype.lower() == "promotioncard":
            results.append(("error", "Schreibweise @type",
                             f"@type wurde als \"{otype}\" gefunden - korrekt wäre exakt \"PromotionCard\" (Groß-/Kleinschreibung beachten)."))
            promotion_cards.append(obj)
        elif otype.lower() == "discountoffer":
            results.append(("error", "Schreibweise @type",
                             f"@type wurde als \"{otype}\" gefunden - korrekt wäre exakt \"DiscountOffer\" (Groß-/Kleinschreibung beachten)."))
            discount_offers.append(obj)
        else:
            unknown_objects.append(obj)

    if unknown_objects:
        results.append(("warn", "Unbekannte Objekte",
                         f"{len(unknown_objects)} Objekt(e) im Array haben ein unbekanntes oder fehlendes \"@type\" "
                         "(erwartet: \"PromotionCard\" oder \"DiscountOffer\")."))

    # --- PromotionCards / Bilder ---
    if not promotion_cards:
        results.append(("error", "Bilder (PromotionCard)",
                         "Es wurde kein PromotionCard-Objekt gefunden. Mindestens ein Bild ist verpflichtend, "
                         "falls die Bilder-Vorschau genutzt werden soll."))
    else:
        if len(promotion_cards) > 3:
            results.append(("warn", "Bilder (PromotionCard)",
                             f"Es wurden {len(promotion_cards)} PromotionCard-Objekte gefunden. "
                             "Spezifiziert sind maximal 3 Bilder (Bild 1-3)."))

        filled_images = 0
        for i, card in enumerate(promotion_cards, start=1):
            ctx = card.get("@context", "")
            if ctx != "http://schema.org/":
                results.append(("warn", f"Bild {i}: @context",
                                 f"@context ist \"{ctx}\" - laut Vorlage sollte es exakt \"http://schema.org/\" sein."))
            image = card.get("image", None)
            extracted["images"].append(image)
            if image is None:
                results.append(("error", f"Bild {i}: image-Feld fehlt",
                                 "Das Feld \"image\" fehlt komplett im PromotionCard-Objekt. "
                                 "Laut Vorlage muss der Key immer vorhanden sein (ggf. mit leerem String)."))
                continue
            if image.strip() == "":
                results.append(("ok", f"Bild {i}",
                                 "\"image\" ist bewusst leer gelassen (kein Bild an dieser Position) - laut Spec zulässig."))
                continue
            if not is_valid_https_url(image):
                results.append(("error", f"Bild {i}: Link ungültig",
                                 f"\"image\" muss über einen https-fähigen Link eingebunden werden. Gefunden: {image}"))
            else:
                filled_images += 1
                results.append(("ok", f"Bild {i}", f"Gültiger https-Bildlink erkannt: {image}"))

        if filled_images == 0:
            results.append(("error", "Bilder (PromotionCard)",
                             "Keines der PromotionCard-Objekte enthält einen gültigen https-Bildlink. "
                             "Mind. ein Bild ist verpflichtend."))

    # --- DiscountOffer / Gutscheincode ---
    if not discount_offers:
        results.append(("ok", "Gutscheincode",
                         "Kein DiscountOffer-Objekt im Code vorhanden (Variante \"Nur Bilder\" - das ist zulässig, "
                         "sofern kein Gutscheincode gewünscht ist)."))
    else:
        if len(discount_offers) > 1:
            results.append(("warn", "Gutscheincode",
                             f"Es wurden {len(discount_offers)} DiscountOffer-Objekte gefunden - laut Vorlage sollte es genau eines sein."))
        offer = discount_offers[0]
        extracted["discount"] = offer

        ctx = offer.get("@context", "")
        if ctx != "http://schema.org/":
            results.append(("warn", "DiscountOffer: @context",
                             f"@context ist \"{ctx}\" - laut Vorlage sollte es exakt \"http://schema.org/\" sein."))

        if "discountCode" not in offer:
            results.append(("error", "discountCode fehlt",
                             "Das Feld \"discountCode\" fehlt im DiscountOffer-Objekt. Es muss immer vorhanden sein "
                             "(ggf. als leerer String \"\", falls kein Code gewünscht ist)."))
        else:
            code = offer.get("discountCode") or ""
            if code.strip() == "":
                results.append(("ok", "discountCode",
                                 "\"discountCode\" ist bewusst leer - kein Gutscheincode wird angezeigt (laut Spec zulässig)."))
            else:
                if len(code) < 2:
                    results.append(("error", "discountCode zu kurz",
                                     f"\"discountCode\" muss mind. 2 Zeichen enthalten (aktuell {len(code)})."))
                elif len(code) > 50:
                    results.append(("error", "discountCode zu lang",
                                     f"\"discountCode\" darf max. 50 Zeichen enthalten (aktuell {len(code)})."))
                if check_emojis(code):
                    results.append(("error", "discountCode: Emoji gefunden",
                                     "Im \"discountCode\" sind Emojis nicht erlaubt (Achtung: alle anderen Zeichen "
                                     "inkl. Leerzeichen/Bindestriche werden als Teil des Codes interpretiert)."))
                if len(code) >= 2 and len(code) <= 50 and not check_emojis(code):
                    results.append(("ok", "discountCode", f"Gültiger Gutscheincode erkannt: \"{code}\""))

        desc = offer.get("description")
        if desc:
            if len(desc) > 50:
                results.append(("error", "description zu lang",
                                 f"\"description\" darf max. 50 Zeichen enthalten (aktuell {len(desc)})."))
            else:
                results.append(("ok", "description", f"Gültige Beschreibung: \"{desc}\" ({len(desc)}/50 Zeichen)."))

        starts_raw = offer.get("availabilityStarts")
        ends_raw = offer.get("availabilityEnds")
        starts_dt, starts_err = parse_iso8601(starts_raw) if starts_raw else (None, None)
        ends_dt, ends_err = parse_iso8601(ends_raw) if ends_raw else (None, None)

        if starts_raw and starts_err:
            results.append(("error", "availabilityStarts ungültig",
                             f"\"{starts_raw}\" ist kein gültiges ISO-8601-Datum mit Zeitzone (z.B. 2026-01-01T00:00:00+02:00). Fehler: {starts_err}"))
        if ends_raw and ends_err:
            results.append(("error", "availabilityEnds ungültig",
                             f"\"{ends_raw}\" ist kein gültiges ISO-8601-Datum mit Zeitzone (z.B. 2026-12-31T23:59:59+02:00). Fehler: {ends_err}"))

        if starts_dt and ends_dt:
            if ends_dt <= starts_dt:
                results.append(("error", "Zeitraum ungültig",
                                 "\"availabilityEnds\" liegt vor oder gleich \"availabilityStarts\"."))
            else:
                results.append(("ok", "Gültigkeitszeitraum",
                                 f"{starts_dt.isoformat()} bis {ends_dt.isoformat()}"))
                now = datetime.now(timezone.utc)
                try:
                    ends_utc = ends_dt.astimezone(timezone.utc)
                    delta_days = (ends_utc - now).days
                    if 0 <= delta_days <= 3:
                        results.append(("warn", "Countdown aktiv",
                                         f"Gültigkeitsende ist in {delta_days} Tag(en) - ab 3 Tagen vor Ablauf wird ein Countdown angezeigt."))
                except Exception:
                    pass

    return results, extracted


def check_image_url(url, timeout=8, max_bytes=10 * 1024 * 1024):
    """Ruft eine Bild-URL live ab und prüft Erreichbarkeit, Content-Type, Format und Maße."""
    result = {
        "url": url, "ok": False, "status_code": None, "content_type": None,
        "width": None, "height": None, "format": None, "size_kb": None,
        "matches_spec_resolution": None, "error": None,
    }
    if not REQUESTS_AVAILABLE:
        result["error"] = "Das Paket 'requests' ist nicht installiert."
        return result
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; trustedDialog-checker/1.0)"}
        resp = requests.get(url, timeout=timeout, headers=headers, stream=True)
        result["status_code"] = resp.status_code
        result["content_type"] = resp.headers.get("Content-Type")

        content = b""
        for chunk in resp.iter_content(chunk_size=65536):
            content += chunk
            if len(content) > max_bytes:
                break
        result["size_kb"] = round(len(content) / 1024, 1)

        if resp.status_code != 200:
            result["error"] = f"HTTP-Status {resp.status_code} - Bild nicht erreichbar (Link tot, Hotlink-Schutz, oder Auth erforderlich)."
            return result

        if not PIL_AVAILABLE:
            result["error"] = "Pillow ist nicht installiert - Bildmaße können nicht geprüft werden."
            return result

        try:
            img = Image.open(io.BytesIO(content))
            result["width"], result["height"] = img.size
            result["format"] = img.format
            result["ok"] = True
            if (result["width"], result["height"]) in ALL_EXPECTED_SIZES:
                result["matches_spec_resolution"] = True
            else:
                result["matches_spec_resolution"] = False
        except Exception as e:
            result["error"] = (f"Antwort (Status {resp.status_code}, Content-Type: {result['content_type']}) "
                                f"konnte nicht als Bild gelesen werden ({e}). Vermutlich wird kein echtes Bild "
                                "ausgeliefert (z.B. HTML-Fehlerseite statt Bilddatei).")
    except requests.exceptions.RequestException as e:
        result["error"] = f"Verbindung fehlgeschlagen: {e}"
    return result


def check_dkim(msg):
    dkim_header = msg.get("DKIM-Signature")
    from_header = msg.get("From", "")
    from_domain = get_domain(from_header)
    info = {"present": bool(dkim_header), "d_domain": None, "from_domain": from_domain, "aligned": None}
    if dkim_header:
        m = re.search(r"[;\s]d=([^;]+)", dkim_header)
        if m:
            d_domain = m.group(1).strip().lower()
            info["d_domain"] = d_domain
            if from_domain:
                info["aligned"] = from_domain == d_domain or from_domain.endswith("." + d_domain) or d_domain.endswith("." + from_domain)
    return info


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def run_app():
    st.title("✅ trustedDialog Snippet-Checker")
    st.caption("Prüft EML-Dateien auf korrekte Einbindung des trustedDialog-Preview-Snippets "
               "(Bilder/Gutscheincode) gemäß UIM-Spezifikation, Stand Juni 2026.")

    with st.expander("ℹ️ Was wird geprüft?", expanded=False):
        st.markdown("""
    - Ist der `<script type="application/ld+json">`-Block überhaupt im HTML der Mail vorhanden?
    - Ist der Inhalt gültiges JSON? (häufigster Fehler: die `//`-Kommentare aus der Vorlage wurden nicht entfernt)
    - Struktur: `PromotionCard`(s) und `DiscountOffer` korrekt benannt (`@context`, `@type`)
    - Pflichtfelder: mind. 1 Bild mit https-Link **oder** gültiger `discountCode`
    - Optionale Felder: `description`, `availabilityStarts`/`Ends` (Format & Zeitraum, Countdown-Hinweis)
    - Bonus: DKIM-Signatur vorhanden & Domain-Alignment zur From-Domain
    """)

    uploaded_file = st.file_uploader("EML-Datei hier reinziehen (Drag & Drop)", type=["eml"])

    if uploaded_file is not None:
        file_bytes = uploaded_file.read()
        try:
            msg = parse_eml(file_bytes)
        except Exception as e:
            st.error(f"Die Datei konnte nicht als E-Mail (EML) gelesen werden: {e}")
            st.stop()

        col1, col2, col3 = st.columns(3)
        col1.metric("Betreff", msg.get("Subject", "-"))
        col2.metric("Von", msg.get("From", "-"))
        col3.metric("Datum", msg.get("Date", "-"))

        st.divider()

        # --- DKIM ---
        st.subheader("🔐 DKIM-Signatur (Bonus-Check)")
        dkim_info = check_dkim(msg)
        if not dkim_info["present"]:
            st.error("Keine DKIM-Signatur im Header gefunden. Ohne DKIM funktioniert trustedDialog grundsätzlich nicht.")
        else:
            st.success(f"DKIM-Signatur vorhanden. Signierende Domain (d=): `{dkim_info['d_domain']}`")
            if dkim_info["aligned"] is True:
                st.success(f"DKIM-Domain und From-Domain (`{dkim_info['from_domain']}`) sind aligned. ✅")
            elif dkim_info["aligned"] is False:
                st.warning(f"DKIM-Domain (`{dkim_info['d_domain']}`) und From-Domain (`{dkim_info['from_domain']}`) "
                           "sind NICHT aligned. Laut Best-Practice-Checkliste sollten beide dieselbe (Sub-)Domain nutzen.")

        st.divider()

        # --- HTML/Script Suche ---
        st.subheader("🔎 Suche nach dem trustedDialog-Snippet")
        html_parts = extract_html_parts(msg)

        if not html_parts:
            st.error("Es wurde kein HTML-Teil (text/html) in der E-Mail gefunden. Ohne HTML-Body kann kein "
                     "JSON-LD-Snippet eingebunden sein.")
            st.stop()

        all_scripts = []
        for idx, (html_text, ctype) in enumerate(html_parts):
            scripts = find_json_ld_scripts(html_text)
            for s in scripts:
                s["part_index"] = idx
            all_scripts.extend(scripts)
            if not scripts and detect_escaped_script(html_text):
                st.error(f"HTML-Teil {idx+1}: Es sieht so aus, als sei der Script-Tag HTML-encodiert im Quelltext "
                         "gelandet (z.B. `&lt;script&gt;` statt `<script>`). Das Versandsystem hat den Code vermutlich "
                         "escaped statt ihn als aktives Script einzubinden - dadurch wird JSON-LD von den Mail-Anbietern "
                         "nicht interpretiert.")

        if not all_scripts:
            st.error("Es wurde **kein** `<script type=\"application/ld+json\">`-Block in der E-Mail gefunden. "
                     "Der Kunde hat das Snippet vermutlich gar nicht oder an der falschen Stelle (z.B. nur als Bild "
                     "oder reiner Text) eingebunden.")
            with st.expander("HTML-Quelltext zur manuellen Prüfung anzeigen"):
                for idx, (html_text, ctype) in enumerate(html_parts):
                    st.text_area(f"HTML-Teil {idx+1}", html_text, height=300)
            st.stop()

        st.success(f"{len(all_scripts)} JSON-LD-Script-Block(e) gefunden.")

        for i, script in enumerate(all_scripts, start=1):
            st.markdown(f"### Snippet {i} (HTML-Teil {script['part_index']+1})")
            if script["attrs"].strip() != ' type="application/ld+json"' and 'type="application/ld+json"' not in script["attrs"]:
                st.warning(f"Script-Tag-Attribute weichen von der Vorlage ab: `<script{script['attrs']}>`. "
                           "Bitte prüfen, ob `type=\"application/ld+json\"` exakt (mit Anführungszeichen) gesetzt ist.")

            with st.expander("Gefundener Rohinhalt des Script-Blocks", expanded=False):
                st.code(script["raw_content"].strip(), language="json")

            data, had_comments, error = try_parse_json(script["raw_content"])

            if had_comments and data is not None:
                st.warning("⚠️ Der Code enthält `//`-Kommentarzeilen aus der Original-Vorlage (z.B. `// Bild 1 "
                           "(verpflichtend)`). Das ist **kein gültiges JSON** - viele Mail-Clients/Parser brechen "
                           "an dieser Stelle ab und zeigen die Preview deshalb nicht an. **Diese Kommentare müssen "
                           "vor dem Versand entfernt werden.**")

            if error:
                st.error(f"❌ Das JSON konnte nicht geparst werden - der Code ist syntaktisch ungültig: {error}")
                st.info("Häufige Ursachen: fehlende/zusätzliche Kommas, nicht entfernte `//`-Kommentare, "
                         "falsche Anführungszeichen (z.B. typografische „ “ statt \" \"), fehlende schließende Klammern.")
                continue

            results, extracted = analyze_json_ld(data)

            errors = [r for r in results if r[0] == "error"]
            warns = [r for r in results if r[0] == "warn"]
            oks = [r for r in results if r[0] == "ok"]

            st.markdown(f"**Ergebnis: {len(errors)} Fehler, {len(warns)} Warnungen, {len(oks)} OK**")

            for level, title, text in results:
                if level == "error":
                    st.error(f"**{title}:** {text}")
                elif level == "warn":
                    st.warning(f"**{title}:** {text}")
                else:
                    st.success(f"**{title}:** {text}")

            # --- Live-Bildcheck ---
            valid_images = [img for img in extracted.get("images", []) if is_valid_https_url(img)]
            if valid_images:
                st.markdown("#### 🌐 Live-Bildcheck")
                st.caption("Ruft jede Bild-URL tatsächlich ab und prüft Erreichbarkeit, Content-Type, Format und "
                           "Pixel-Maße gegen die Spezifikation. Wichtig für Fälle, in denen das JSON selbst "
                           "korrekt ist, das referenzierte Bild aber nicht (Format/Größe/toter Link).")
                run_check = st.button(f"Bild-URLs jetzt abrufen ({len(valid_images)} Bild(er)) - Snippet {i}", key=f"imgcheck_{i}")
                if run_check:
                    expected_label = EXPECTED_SIZES.get(len(valid_images))
                    if expected_label:
                        st.info(f"Erwartete Auflösung bei {len(valid_images)} Bild(ern): "
                                 f"**{expected_label[0]}×{expected_label[1]} px** pro Bild (laut Spezifikation).")
                    for n, img_url in enumerate(valid_images, start=1):
                        with st.spinner(f"Prüfe Bild {n}..."):
                            r = check_image_url(img_url)
                        if r["ok"] and r["matches_spec_resolution"]:
                            st.success(f"**Bild {n}:** OK - {r['format']} {r['width']}×{r['height']}px, "
                                       f"{r['size_kb']} KB, HTTP {r['status_code']}")
                        elif r["ok"] and not r["matches_spec_resolution"]:
                            st.warning(f"**Bild {n}:** Bild ist erreichbar und gültig ({r['format']} "
                                       f"{r['width']}×{r['height']}px), entspricht aber **keiner** der spezifizierten "
                                       f"Auflösungen (1088×464 / 532×352 / 348×260 px).")
                        else:
                            st.error(f"**Bild {n}:** {r['error']}")
                        st.caption(img_url)

            st.divider()

    else:
        st.info("Bitte eine .eml-Datei hochladen, um die Prüfung zu starten.")


if __name__ == "__main__":
    run_app()
