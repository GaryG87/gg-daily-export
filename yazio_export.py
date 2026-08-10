#!/usr/bin/env python3
"""
yazio_export.py  ·  v1.0
--------------------------------------------------------------------------
Holt Ernaehrungsdaten aus Yazio und schreibt sie nach export/yazio.json.

Gleiche Schutzmechanik wie garmin_export.py:
  * Ausgabedatei wird IMMER geschrieben, auch beim Absturz
  * Versionsnummer und Zeitbudget in der JSON
  * bestehende Historie wird vor dem Login gelesen und im Fehlerfall
    unveraendert zurueckgeschrieben

Die Endpunkte stammen aus dem Quellcode von funmelon64/Yazio-Exporter.
Es ist eine inoffizielle Schnittstelle - Yazio kann sie jederzeit aendern.
Genau dafuer ist das Rohdatenbeispiel da.

Umgebungsvariablen:
  YAZIO_USER    Yazio-E-Mail
  YAZIO_PASS    Yazio-Passwort
  DAYS          Wie viele Tage rueckwirkend (Standard 30)
  DETAIL_DAYS   Fuer wie viele der letzten Tage die Einzelprodukte
                aufgeloest werden (Standard 7, 0 schaltet es ab)
  RAW_SAMPLE    "ja" = Rohdatenbeispiel mitschreiben
"""

import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

VERSION = "yazio_export v1.3"

BASIS = "https://yzapi.yazio.com"
CLIENT_ID = "1_4hiybetvfksgw40o0sog4s884kwc840wwso8go4k8c04goo4c"
CLIENT_SECRET = "6rok2m65xuskgkgogw40wkkk8sw0osg84s8cggsc4woos4s8o"

OUT_PATH = Path("export/yazio.json")
CACHE_PATH = Path("export/yazio_produkte.json")
RAW_PATH = Path("export/yazio_raw_sample.json")

ZEITBUDGET_SEK = 900
MAX_API_CALLS = 600
PAUSE_SEK = 0.25
HISTORIE_MAX_TAGE = 500

start_zeit = time.time()

ergebnis = {
    "version": VERSION,
    "erzeugt_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "status": "gestartet",
    "fehler": None,
    "traceback": None,
    "hinweise": [],
    "api_calls": 0,
    "zeitraum": {},
    "lesehinweise": {
        "einheiten": (
            "Energie in kcal, Makronaehrstoffe in Gramm, Wasser in ml. "
            "Die Werte unter 'naehrstoffe' stammen unveraendert aus Yazio "
            "und behalten deren Schluesselnamen (z. B. vitamin.d); die "
            "Einheit dort ist am ersten Datensatz zu verifizieren, "
            "vermutlich Gramm."
        ),
        "naehrstoffe": (
            "Vitamine und Mineralstoffe sind Summen aus den Einzelposten. "
            "Immer 'naehrstoffe_abdeckung_prozent' danebenlegen: Er sagt, "
            "welcher Anteil der Tageskalorien aus Produkten stammt, die "
            "diesen Wert ueberhaupt hinterlegt haben. Unter etwa 80 Prozent "
            "ist die Summe eine Untergrenze und keine Aussage."
        ),
        "tagesdatum": (
            "Anders als beim Schlaf ist das Datum hier eindeutig: es ist "
            "der Tag, an dem gegessen wurde."
        ),
        "vollstaendigkeit": (
            "Tage ohne Eintraege in Yazio erscheinen mit 0 kcal. Vor jeder "
            "Auswertung pruefen, ob ein Nulltag wirklich ein Fastentag war "
            "oder nur ein nicht getrackter Tag."
        ),
    },
    "tage": [],
    "ziele": {},
}


def hinweis(text):
    print("HINWEIS:", text, flush=True)
    if text not in ergebnis["hinweise"]:
        ergebnis["hinweise"].append(text)


def budget_ok():
    return (time.time() - start_zeit) < ZEITBUDGET_SEK and \
        ergebnis["api_calls"] < MAX_API_CALLS


# ---------------------------------------------------------------------------
# Historie und Produkt-Zwischenspeicher lesen (VOR dem Login)
# ---------------------------------------------------------------------------

historie_tage = {}
produkt_cache = {}

if OUT_PATH.exists():
    try:
        alt = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        for eintrag in alt.get("tage", []):
            if eintrag.get("datum"):
                historie_tage[eintrag["datum"]] = eintrag
        ergebnis["ziele"] = alt.get("ziele", {})
        print(f"Historie geladen: {len(historie_tage)} Tage", flush=True)
    except Exception as exc:
        hinweis(f"Alte yazio.json nicht lesbar, starte neu: {exc}")

if CACHE_PATH.exists():
    try:
        produkt_cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        print(f"Produkt-Zwischenspeicher: {len(produkt_cache)} Eintraege",
              flush=True)
    except Exception as exc:
        hinweis(f"Produkt-Zwischenspeicher nicht lesbar: {exc}")


def schreibe_ausgabe():
    tage = sorted(historie_tage.values(), key=lambda e: e["datum"])
    if len(tage) > HISTORIE_MAX_TAGE:
        tage = tage[-HISTORIE_MAX_TAGE:]
    ergebnis["tage"] = tage
    ergebnis["anzahl"] = {
        "tage": len(tage),
        "tage_mit_eintraegen": sum(
            1 for e in tage if (e.get("energie_kcal") or 0) > 0
        ),
        "produkte_zwischengespeichert": len(produkt_cache),
    }
    ergebnis["laufzeit_sek"] = int(time.time() - start_zeit)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(ergebnis, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    try:
        CACHE_PATH.write_text(
            json.dumps(produkt_cache, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception as exc:
        hinweis(f"Produkt-Zwischenspeicher nicht schreibbar: {exc}")
    print(f"Geschrieben: {OUT_PATH} (Status: {ergebnis['status']})", flush=True)


# ---------------------------------------------------------------------------
# Naehrstoff-Schluessel von Yazio auf lesbare Namen abbilden
# ---------------------------------------------------------------------------

# Bekannte Schluessel werden in lesbare Namen uebersetzt. Alles andere
# wird UNVERAENDERT durchgereicht - so gehen Vitamine, Mineralstoffe und
# spaetere Ergaenzungen von Yazio nicht verloren, auch wenn ich ihre
# Schluesselnamen heute nicht kenne. Weder der Go-Exporter noch die
# oeffentliche API-Beschreibung dokumentieren sie.
LESBAR = {
    "energy.energy": "energie_kcal",
    "nutrient.protein": "protein_g",
    "nutrient.carb": "kohlenhydrate_g",
    "nutrient.sugar": "zucker_g",
    "nutrient.sugaradded": "zuckerzusatz_g",
    "nutrient.fat": "fett_g",
    "nutrient.saturated": "gesaettigte_fettsaeuren_g",
    "nutrient.monounsaturated": "einfach_ungesaettigt_g",
    "nutrient.polyunsaturated": "mehrfach_ungesaettigt_g",
    "nutrient.transfat": "transfettsaeuren_g",
    "nutrient.dietaryfiber": "ballaststoffe_g",
    "nutrient.cholesterol": "cholesterin_g",
    "nutrient.sodium": "natrium_g",
    "nutrient.salt": "salz_g",
    "nutrient.water": "wasser_g",
    "nutrient.alcohol": "alkohol_g",
}


def lesbar(schluessel):
    return LESBAR.get(schluessel, schluessel)


TAGESWERTE = {
    "energie_kcal": "energy",
    "protein_g": "protein",
    "kohlenhydrate_g": "carb",
    "fett_g": "fat",
    "kalorienziel_kcal": "energy_goal",
}


def tageswerte(quelle):
    """Der Endpunkt nutrients-daily liefert flache Schluessel (energy,
    protein, carb, fat) - anders als die Produktabfrage, die punktierte
    Schluessel verwendet. Ballaststoffe und Zucker fehlen hier ganz und
    werden weiter unten aus den Einzelposten aufsummiert."""
    werte = {}
    for name, schluessel in TAGESWERTE.items():
        roh = (quelle or {}).get(schluessel)
        try:
            werte[name] = None if roh is None else round(float(roh), 2)
        except Exception:
            werte[name] = None
    return werte


def naehrwerte(quelle, faktor=1.0):
    """Nimmt JEDEN Naehrstoffschluessel mit, den die Antwort enthaelt."""
    werte = {}
    for schluessel, roh in (quelle or {}).items():
        if roh is None:
            continue
        try:
            werte[lesbar(schluessel)] = round(float(roh) * faktor, 6)
        except Exception:
            continue
    return werte


# ---------------------------------------------------------------------------

class Yazio:
    def __init__(self):
        self.sitzung = requests.Session()
        self.token = None

    def _call(self, methode, pfad, **kwargs):
        if not budget_ok():
            raise TimeoutError(
                f"Budget erschoepft ({ergebnis['api_calls']} Aufrufe, "
                f"{int(time.time() - start_zeit)} s)"
            )
        time.sleep(PAUSE_SEK)
        ergebnis["api_calls"] += 1
        kopf = {"Accept": "application/json"}
        if self.token:
            kopf["Authorization"] = "Bearer " + self.token
        antwort = self.sitzung.request(
            methode, BASIS + pfad, headers=kopf, timeout=30, **kwargs
        )
        if antwort.status_code != 200:
            raise RuntimeError(
                f"{pfad.split('?')[0]} -> HTTP {antwort.status_code}: "
                f"{antwort.text[:200]}"
            )
        return antwort.json()

    def sanft(self, methode, pfad, **kwargs):
        """Wie _call, aber Fehler landen als Hinweis statt als Abbruch."""
        try:
            return self._call(methode, pfad, **kwargs)
        except TimeoutError:
            raise
        except Exception as exc:
            hinweis(str(exc))
            return None

    def anmelden(self, benutzer, passwort):
        zugang = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": benutzer,
            "password": passwort,
            "grant_type": "password",
        }
        # Zwei Schreibweisen im Umlauf: der Go-Exporter schickt JSON, die
        # oeffentliche API-Beschreibung nennt Formularkodierung. Erst JSON,
        # bei Ablehnung das Formular - spart eine Fehlerrunde.
        try:
            daten = self._call("POST", "/v9/oauth/token", json=zugang)
        except TimeoutError:
            raise
        except Exception as exc:
            hinweis(f"Anmeldung per JSON abgelehnt ({exc}), "
                    f"versuche Formularkodierung.")
            daten = self._call("POST", "/v9/oauth/token", data=zugang)
        self.token = daten.get("access_token")
        if not self.token:
            raise RuntimeError("Anmeldung ohne access_token zurueckgekommen.")

    def produkt(self, produkt_id):
        zwischenspeicher = produkt_cache.get(produkt_id)
        # Eintraege aus aelteren Skriptversionen enthalten nur neun
        # Naehrstoffe. Sie werden verworfen und einmalig neu geholt,
        # damit Vitamine und Mineralstoffe nicht dauerhaft fehlen.
        if zwischenspeicher and zwischenspeicher.get("cache_version", 1) >= 3:
            return zwischenspeicher
        daten = self.sanft("GET", f"/v9/products/{produkt_id}")
        if daten is None:
            return None
        # Alles behalten, was Yazio mitschickt - auch Vitamine,
        # Mineralstoffe und Fettsaeure-Unterarten. Kostet keine
        # zusaetzliche Abfrage, nur Speicherplatz.
        roh = daten.get("nutrients") or {}
        eintrag = {
            "name": daten.get("name"),
            "hersteller": daten.get("producer"),
            "kategorie": daten.get("category"),
            "cache_version": 3,
            "je_gramm": naehrwerte(roh),
        }
        produkt_cache[produkt_id] = eintrag
        return eintrag


def main():
    benutzer = (os.getenv("YAZIO_USER") or "").strip()
    passwort = os.getenv("YAZIO_PASS") or ""
    if not benutzer or not passwort:
        raise RuntimeError("YAZIO_USER und YAZIO_PASS als Secret hinterlegen.")

    tage_zurueck = max(1, min(int(os.getenv("DAYS", "30") or "30"), 400))
    detail_tage = max(0, min(int(os.getenv("DETAIL_DAYS", "14") or "14"), 400))

    heute = date.today()
    start = heute - timedelta(days=tage_zurueck)
    ergebnis["zeitraum"] = {
        "von": start.isoformat(),
        "bis": heute.isoformat(),
        "tage_angefragt": tage_zurueck,
        "detailtage": detail_tage,
    }

    yz = Yazio()
    yz.anmelden(benutzer, passwort)
    print("Anmeldung erfolgreich.", flush=True)

    # ---------- Tagesnaehrwerte, monatsweise ----------
    # Monatsweise wie im Original-Exporter: lange Zeitraeume werden von
    # der Schnittstelle nicht zuverlaessig beantwortet.
    roh_monat = None
    abschnitt = start
    while abschnitt <= heute:
        if abschnitt.month == 12:
            monats_ende = date(abschnitt.year, 12, 31)
        else:
            monats_ende = date(abschnitt.year, abschnitt.month + 1, 1) - \
                timedelta(days=1)
        ende = min(monats_ende, heute)

        daten = yz.sanft(
            "GET",
            f"/v9/user/consumed-items/nutrients-daily"
            f"?start={abschnitt.isoformat()}&end={ende.isoformat()}",
        )
        if roh_monat is None and daten:
            roh_monat = daten

        for tag in (daten or []):
            datum = str(tag.get("date", ""))[:10]
            if not datum:
                continue
            vorhanden = historie_tage.get(datum, {})
            vorhanden.update({"datum": datum})
            vorhanden.update(tageswerte(tag))
            historie_tage[datum] = vorhanden

        abschnitt = ende + timedelta(days=1)

    print(f"Tagesnaehrwerte: {len(historie_tage)} Tage", flush=True)

    # ---------- Ziele (aendern sich selten, nur aktueller Stand) ----------
    ziele = yz.sanft("GET", f"/v9/user/goals?date={heute.isoformat()}")
    if ziele:
        ergebnis["ziele"] = {
            "stand": heute.isoformat(),
            "energie_kcal": ziele.get("energy.energy"),
            "protein_g": ziele.get("nutrient.protein"),
            "kohlenhydrate_g": ziele.get("nutrient.carb"),
            "fett_g": ziele.get("nutrient.fat"),
            "schritte": ziele.get("activity.step"),
            "zielgewicht_kg": ziele.get("bodyvalue.weight"),
            "wasser_ml": ziele.get("water"),
        }

    # ---------- Wasser und Einzelprodukte fuer die juengsten Tage ----------
    for versatz in range(detail_tage):
        if not budget_ok():
            hinweis("Budget erreicht, Detailtage unvollstaendig.")
            break
        tag = heute - timedelta(days=versatz)
        d = tag.isoformat()
        eintrag = historie_tage.setdefault(d, {"datum": d})

        wasser = yz.sanft("GET", f"/v9/user/water-intake?date={d}")
        if wasser:
            eintrag["wasser_ml"] = wasser.get("water_intake")

        verzehr = yz.sanft("GET", f"/v9/user/consumed-items?date={d}")
        if not verzehr:
            continue

        posten = []
        for p in verzehr.get("products", []):
            prod = yz.produkt(p.get("product_id")) or {}
            menge = p.get("amount") or 0
            werte = prod.get("je_gramm") or {}
            skaliert = {
                name: round(wert * menge, 4)
                for name, wert in (prod.get("je_gramm") or {}).items()
            }
            posten.append({
                "mahlzeit": p.get("daytime"),
                "name": prod.get("name"),
                "hersteller": prod.get("hersteller"),
                "menge_g": menge,
                "portion": p.get("serving"),
                "portionen": p.get("serving_quantity"),
                "energie_kcal": round(skaliert.get("energie_kcal", 0), 1),
                "protein_g": round(skaliert.get("protein_g", 0), 1),
                "ballaststoffe_g": (
                    None if "ballaststoffe_g" not in skaliert
                    else round(skaliert["ballaststoffe_g"], 1)
                ),
                "naehrstoffe": skaliert,
            })

        for r in verzehr.get("recipe_portions", []):
            posten.append({
                "mahlzeit": r.get("daytime"),
                "name": "Rezept",
                "rezept_id": r.get("recipe_id"),
                "portionen": r.get("portion_count"),
                "hinweis": "Naehrwerte stecken in der Tagessumme, "
                           "hier nicht einzeln aufgeloest",
            })
        for s in verzehr.get("simple_products", []):
            posten.append({
                "mahlzeit": s.get("daytime"),
                "name": s.get("name"),
                "energie_kcal": s.get("energy"),
                "einfach_erfasst": True,
            })

        eintrag["posten"] = posten

        # Yazio liefert als Tagessumme nur Energie und die drei Makros.
        # Alles Weitere - Ballaststoffe, Zucker, Fettsaeuretypen, Mineral-
        # stoffe, Vitamine - wird hier aus den Einzelposten aufsummiert.
        #
        # Wichtig: Fehlt ein Naehrstoff bei einem Produkt, faellt er
        # stillschweigend aus der Summe. Deshalb steht neben jedem Wert,
        # wie viele Posten ihn ueberhaupt hinterlegt hatten. Eine Summe
        # mit niedriger Abdeckung ist eine Untergrenze, kein Messwert.
        essbar = [x for x in posten if x.get("naehrstoffe")]
        summe = {}
        abdeckung = {}
        for x in essbar:
            for name, wert in x["naehrstoffe"].items():
                summe[name] = round(summe.get(name, 0) + wert, 3)
                abdeckung[name] = abdeckung.get(name, 0) + 1

        eintrag["naehrstoffe"] = summe
        eintrag["naehrstoffe_abdeckung"] = abdeckung
        eintrag["ballaststoffe_g"] = summe.get("ballaststoffe_g")
        eintrag["zucker_g"] = summe.get("zucker_g")
        eintrag["energie_kcal_aus_posten"] = round(
            summe.get("energie_kcal", 0), 1
        )
        eintrag["abdeckung"] = {
            "posten": len(essbar),
            "davon_mit_ballaststoffangabe": abdeckung.get(
                "ballaststoffe_g", 0
            ),
            "hinweis": (
                "Weicht energie_kcal_aus_posten stark von energie_kcal ab, "
                "fehlen Posten in der Aufloesung. naehrstoffe_abdeckung "
                "zeigt je Naehrstoff, wie viele der Posten ihn angegeben "
                "hatten - bei niedriger Zahl ist die Summe zu niedrig."
            ),
        }

        if versatz == 0 and os.getenv("RAW_SAMPLE", "ja").lower() in (
            "ja", "true", "1", "yes"
        ):
            RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
            RAW_PATH.write_text(
                json.dumps(
                    {
                        "hinweis": "Rohantworten zur Feldpruefung.",
                        "nutrients_daily_erster_monat": (roh_monat or [])[:2],
                        "goals": ziele,
                        "water_intake": wasser,
                        "consumed_items": verzehr,
                        "produkt_beispiel_je_gramm": (
                            list(produkt_cache.values())[0]
                            if produkt_cache else None
                        ),
                        "alle_gefundenen_naehrstoffschluessel": sorted({
                            k for v in produkt_cache.values()
                            for k in (v.get("je_gramm") or {})
                        }),
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            print(f"Rohdatenbeispiel geschrieben: {RAW_PATH}", flush=True)

    ergebnis["status"] = "ok"


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        ergebnis["status"] = "fehler"
        ergebnis["fehler"] = f"{type(exc).__name__}: {exc}"
        ergebnis["traceback"] = traceback.format_exc()
        print(ergebnis["traceback"], file=sys.stderr, flush=True)
    finally:
        try:
            schreibe_ausgabe()
        except Exception:
            traceback.print_exc()
    sys.exit(0)
