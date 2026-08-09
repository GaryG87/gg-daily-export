#!/usr/bin/env python3
"""
garmin_export.py  ·  v1.0
--------------------------------------------------------------------------
Holt Garmin-Connect-Daten und schreibt sie nach export/garmin.json.

Grundregeln (aus den Learnings des TPPWB-Exports):
  * Es wird IMMER eine Ausgabedatei geschrieben - auch beim Absturz.
    Der Traceback landet in der JSON, weil Claude die Actions-Logs
    nicht lesen kann.
  * Versionsnummer steht in der JSON, damit sofort erkennbar ist,
    ob der neue Stand gelaufen ist.
  * Hartes Zeitbudget und Obergrenze fuer API-Aufrufe.
  * Bestehende Historie wird zuerst gelesen und bei Fehlern
    unveraendert zurueckgeschrieben - es gehen nie Daten verloren.

Umgebungsvariablen:
  GARMIN_USER        Garmin-Connect-E-Mail        (Weg A: Login mit Passwort)
  GARMIN_PASS        Garmin-Connect-Passwort      (Weg A)
  GARMIN_TOKENS_B64  Base64-Token aus garmin_token.py (Weg B: MFA-tauglich)
  DAYS               Wie viele Tage rueckwirkend geholt werden (Standard 10)
  RAW_SAMPLE         "ja" = Rohdatenbeispiel eines Tages mitschreiben
"""

import base64
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

VERSION = "garmin_export v1.0"

OUT_PATH = Path("export/garmin.json")
RAW_PATH = Path("export/garmin_raw_sample.json")

ZEITBUDGET_SEK = 900        # globales Limit, danach wird sauber abgebrochen
MAX_API_CALLS = 400         # Obergrenze, damit keine Schleife durchdreht
PAUSE_SEK = 0.5             # Pause zwischen Aufrufen (Rate-Limit-Schutz)
HISTORIE_MAX_TAGE = 500     # aeltere Tage werden aus der JSON entfernt
CALL_TIMEOUT_HINT = 30

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
        "schlaf": (
            "Der Schlafdatensatz eines Tages beschreibt die Nacht DAVOR. "
            "Die Felder nacht_von/nacht_bis enthalten die tatsaechlichen "
            "Zeitstempel - immer diese verwenden, nicht das Tagesdatum."
        ),
        "einheiten": (
            "Schlaf in Minuten, Gewicht in kg, Distanz in Metern, "
            "Dauer in Sekunden, Kalorien in kcal."
        ),
    },
    "tage": [],
    "aktivitaeten": [],
    "gewicht": [],
}


def hinweis(text):
    print("HINWEIS:", text, flush=True)
    if text not in ergebnis["hinweise"]:
        ergebnis["hinweise"].append(text)


def budget_ok():
    return (time.time() - start_zeit) < ZEITBUDGET_SEK and \
        ergebnis["api_calls"] < MAX_API_CALLS


def call(funktion, *args, **kwargs):
    """Ein API-Aufruf mit Zaehler, Pause und Fehlerabfang."""
    if not budget_ok():
        raise TimeoutError(
            f"Zeit- oder Aufrufbudget erschoepft "
            f"({ergebnis['api_calls']} Aufrufe, "
            f"{int(time.time() - start_zeit)} s)"
        )
    time.sleep(PAUSE_SEK)
    ergebnis["api_calls"] += 1
    try:
        return funktion(*args, **kwargs)
    except Exception as exc:
        hinweis(f"{getattr(funktion, '__name__', 'call')}{args}: {exc}")
        return None


def dig(objekt, *pfad, default=None):
    """Sicheres Durchgreifen durch verschachtelte Dicts/Listen."""
    aktuell = objekt
    for schluessel in pfad:
        if aktuell is None:
            return default
        try:
            if isinstance(schluessel, int):
                aktuell = aktuell[schluessel]
            else:
                aktuell = aktuell.get(schluessel)
        except Exception:
            return default
    return default if aktuell is None else aktuell


def sek_zu_min(wert):
    if wert in (None, ""):
        return None
    try:
        return round(float(wert) / 60)
    except Exception:
        return None


def ms_zu_zeit(wert):
    """Garmin liefert '...TimestampLocal' als Millisekunden-Zeitstempel,
    der bereits die lokale Wanduhrzeit abbildet."""
    if wert in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(
            float(wert) / 1000.0, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return wert


def gramm_zu_kg(wert):
    if wert in (None, ""):
        return None
    try:
        return round(float(wert) / 1000.0, 2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Bestehende Historie einlesen (VOR dem Login - damit sie bei einem
# Absturz unveraendert erhalten bleibt)
# ---------------------------------------------------------------------------

historie_tage = {}
historie_akt = {}
historie_gewicht = {}

if OUT_PATH.exists():
    try:
        alt = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        for eintrag in alt.get("tage", []):
            if eintrag.get("datum"):
                historie_tage[eintrag["datum"]] = eintrag
        for eintrag in alt.get("aktivitaeten", []):
            if eintrag.get("id") is not None:
                historie_akt[str(eintrag["id"])] = eintrag
        for eintrag in alt.get("gewicht", []):
            if eintrag.get("datum"):
                historie_gewicht[eintrag["datum"]] = eintrag
        print(f"Historie geladen: {len(historie_tage)} Tage, "
              f"{len(historie_akt)} Aktivitaeten", flush=True)
    except Exception as exc:
        hinweis(f"Alte garmin.json nicht lesbar, starte neu: {exc}")


def schreibe_ausgabe():
    """Wird IMMER aufgerufen - auch im Fehlerfall."""
    tage = sorted(historie_tage.values(), key=lambda e: e["datum"])
    if len(tage) > HISTORIE_MAX_TAGE:
        tage = tage[-HISTORIE_MAX_TAGE:]
    grenze = tage[0]["datum"] if tage else ""

    ergebnis["tage"] = tage
    ergebnis["aktivitaeten"] = sorted(
        historie_akt.values(),
        key=lambda e: (e.get("start") or ""),
    )
    ergebnis["gewicht"] = sorted(
        (e for e in historie_gewicht.values() if e["datum"] >= grenze),
        key=lambda e: e["datum"],
    )
    ergebnis["anzahl"] = {
        "tage": len(ergebnis["tage"]),
        "aktivitaeten": len(ergebnis["aktivitaeten"]),
        "gewichtsmessungen": len(ergebnis["gewicht"]),
    }
    ergebnis["laufzeit_sek"] = int(time.time() - start_zeit)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(ergebnis, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"Geschrieben: {OUT_PATH} (Status: {ergebnis['status']})", flush=True)


# ---------------------------------------------------------------------------
# Rohdaten-Beispiel: Schluessel mit Personenbezug entfernen
# ---------------------------------------------------------------------------

VERBOTENE_SCHLUESSEL = (
    "latitude", "longitude", "email", "fullname", "displayname",
    "profileimage", "address", "userprofile", "uuid", "deviceid",
    "phonenumber", "ownerid", "locationname", "gps",
)


def saeubern(objekt, tiefe=0):
    if tiefe > 6:
        return "…"
    if isinstance(objekt, dict):
        sauber = {}
        for schluessel, wert in objekt.items():
            if any(v in str(schluessel).lower() for v in VERBOTENE_SCHLUESSEL):
                continue
            sauber[schluessel] = saeubern(wert, tiefe + 1)
        return sauber
    if isinstance(objekt, list):
        gekuerzt = objekt[:3]
        rest = len(objekt) - len(gekuerzt)
        ausgabe = [saeubern(e, tiefe + 1) for e in gekuerzt]
        if rest > 0:
            ausgabe.append(f"… {rest} weitere Eintraege gekuerzt")
        return ausgabe
    return objekt


# ---------------------------------------------------------------------------
# Hauptlauf
# ---------------------------------------------------------------------------

def main():
    from garminconnect import Garmin

    tage_zurueck = int(os.getenv("DAYS", "10") or "10")
    tage_zurueck = max(1, min(tage_zurueck, 180))

    heute = date.today()
    start = heute - timedelta(days=tage_zurueck)
    ergebnis["zeitraum"] = {
        "von": start.isoformat(),
        "bis": heute.isoformat(),
        "tage_angefragt": tage_zurueck,
    }

    # ---------- Anmeldung ----------
    tokens_b64 = (os.getenv("GARMIN_TOKENS_B64") or "").strip()
    benutzer = (os.getenv("GARMIN_USER") or "").strip()
    passwort = os.getenv("GARMIN_PASS") or ""

    tokenordner = None
    if tokens_b64:
        try:
            tokenordner = Path("tokens_tmp")
            tokenordner.mkdir(exist_ok=True)
            (tokenordner / "garmin_tokens.json").write_text(
                base64.b64decode(tokens_b64).decode("utf-8"), encoding="utf-8"
            )
            ergebnis["login_weg"] = "Token"
        except Exception as exc:
            hinweis(f"Token nicht verwendbar, versuche Passwort-Login: {exc}")
            tokenordner = None

    if tokenordner is None:
        if not benutzer or not passwort:
            raise RuntimeError(
                "Keine Zugangsdaten gefunden. Entweder GARMIN_USER und "
                "GARMIN_PASS oder GARMIN_TOKENS_B64 als Secret hinterlegen."
            )
        ergebnis["login_weg"] = "Benutzer/Passwort"

    garmin = Garmin(benutzer or None, passwort or None)
    mfa_status, _ = garmin.login(
        tokenstore=str(tokenordner) if tokenordner else None
    )
    if mfa_status:
        raise RuntimeError(
            "Garmin verlangt eine Zwei-Faktor-Bestaetigung (MFA). "
            "Automatischer Passwort-Login ist damit nicht moeglich. "
            "Bitte einmalig garmin_token.py lokal ausfuehren und das "
            "Ergebnis als Secret GARMIN_TOKENS_B64 hinterlegen."
        )
    print("Anmeldung erfolgreich.", flush=True)

    # ---------- Bereichsabfragen (je 1 Aufruf) ----------
    aktivitaeten = call(
        garmin.get_activities_by_date, start.isoformat(), heute.isoformat()
    ) or []
    for akt in aktivitaeten:
        akt_id = dig(akt, "activityId")
        if akt_id is None:
            continue
        historie_akt[str(akt_id)] = {
            "id": akt_id,
            "start": dig(akt, "startTimeLocal"),
            "name": dig(akt, "activityName"),
            "typ": dig(akt, "activityType", "typeKey"),
            "dauer_sek": dig(akt, "duration"),
            "distanz_m": dig(akt, "distance"),
            "kalorien": dig(akt, "calories"),
            "hf_schnitt": dig(akt, "averageHR"),
            "hf_max": dig(akt, "maxHR"),
            "trainingseffekt_aerob": dig(akt, "aerobicTrainingEffect"),
            "trainingseffekt_anaerob": dig(akt, "anaerobicTrainingEffect"),
            "trainingsbelastung": dig(akt, "activityTrainingLoad"),
            "schritte": dig(akt, "steps"),
            "intensitaetsminuten_moderat": dig(akt, "moderateIntensityMinutes"),
            "intensitaetsminuten_hoch": dig(akt, "vigorousIntensityMinutes"),
        }
    print(f"Aktivitaeten: {len(aktivitaeten)}", flush=True)

    waagen = call(garmin.get_weigh_ins, start.isoformat(), heute.isoformat())
    for tagessatz in dig(waagen, "dailyWeightSummaries", default=[]) or []:
        datum = dig(tagessatz, "summaryDate")
        messung = dig(tagessatz, "allWeightMetrics", 0, default={}) or {}
        if not messung:
            messung = dig(tagessatz, "latestWeight", default={}) or {}
        if datum:
            historie_gewicht[datum] = {
                "datum": datum,
                "gewicht_kg": gramm_zu_kg(dig(messung, "weight")),
                "koerperfett_prozent": dig(messung, "bodyFat"),
                "muskelmasse_kg": gramm_zu_kg(dig(messung, "muscleMass")),
                "wasser_prozent": dig(messung, "bodyWater"),
            }

    bodybattery = call(
        garmin.get_body_battery, start.isoformat(), heute.isoformat()
    ) or []
    bb_nach_datum = {}
    for eintrag in bodybattery:
        datum = dig(eintrag, "date") or dig(eintrag, "calendarDate")
        if datum:
            bb_nach_datum[datum] = eintrag

    # ---------- Tagesabfragen ----------
    rohbeispiel = None
    tagesliste = [start + timedelta(days=i) for i in range(tage_zurueck + 1)]

    for tag in tagesliste:
        if not budget_ok():
            hinweis(f"Budget erreicht, Abbruch bei {tag.isoformat()}")
            break
        d = tag.isoformat()

        stats = call(garmin.get_stats_and_body, d) or {}
        schlaf = call(garmin.get_sleep_data, d) or {}
        hrv = call(garmin.get_hrv_data, d) or {}
        maxmetrik = call(garmin.get_max_metrics, d)

        if isinstance(maxmetrik, list):
            maxmetrik = maxmetrik[0] if maxmetrik else {}
        maxmetrik = maxmetrik or {}

        schlaf_dto = dig(schlaf, "dailySleepDTO", default={}) or {}
        bb = bb_nach_datum.get(d, {})

        eintrag = {
            "datum": d,
            "schritte": dig(stats, "totalSteps"),
            "distanz_m": dig(stats, "totalDistanceMeters"),
            "kalorien_gesamt": dig(stats, "totalKilocalories"),
            "kalorien_aktiv": dig(stats, "activeKilocalories"),
            "kalorien_grundumsatz": dig(stats, "bmrKilocalories"),
            "ruhepuls": dig(stats, "restingHeartRate"),
            "hf_min": dig(stats, "minHeartRate"),
            "hf_max": dig(stats, "maxHeartRate"),
            "stress_schnitt": dig(stats, "averageStressLevel"),
            "stress_max": dig(stats, "maxStressLevel"),
            "intensitaetsminuten_moderat": dig(stats, "moderateIntensityMinutes"),
            "intensitaetsminuten_hoch": dig(stats, "vigorousIntensityMinutes"),
            "spo2_schnitt": dig(stats, "averageSpo2"),
            "atemfrequenz_schnitt": dig(stats, "avgWakingRespirationValue"),
            "bodybattery_hoch": dig(stats, "bodyBatteryHighestValue"),
            "bodybattery_tief": dig(stats, "bodyBatteryLowestValue"),
            "bodybattery_geladen": dig(bb, "charged"),
            "bodybattery_verbraucht": dig(bb, "drained"),
            "gewicht_kg": gramm_zu_kg(dig(stats, "weight")),
            "vo2max_laufen": dig(maxmetrik, "generic", "vo2MaxPreciseValue")
            or dig(maxmetrik, "generic", "vo2MaxValue"),
            "vo2max_rad": dig(maxmetrik, "cycling", "vo2MaxPreciseValue")
            or dig(maxmetrik, "cycling", "vo2MaxValue"),
            "fitnessalter": dig(maxmetrik, "generic", "fitnessAge"),
            "schlaf": {
                "nacht_von": ms_zu_zeit(dig(schlaf_dto, "sleepStartTimestampLocal")),
                "nacht_bis": ms_zu_zeit(dig(schlaf_dto, "sleepEndTimestampLocal")),
                "gesamt_min": sek_zu_min(dig(schlaf_dto, "sleepTimeSeconds")),
                "tief_min": sek_zu_min(dig(schlaf_dto, "deepSleepSeconds")),
                "leicht_min": sek_zu_min(dig(schlaf_dto, "lightSleepSeconds")),
                "rem_min": sek_zu_min(dig(schlaf_dto, "remSleepSeconds")),
                "wach_min": sek_zu_min(dig(schlaf_dto, "awakeSleepSeconds")),
                "score": dig(schlaf_dto, "sleepScores", "overall", "value"),
                "bewertung": dig(schlaf_dto, "sleepScores", "overall",
                                 "qualifierKey"),
                "schlafstress": dig(schlaf_dto, "avgSleepStress"),
                "unruhemomente": dig(schlaf_dto, "restlessMomentsCount"),
                "ruhepuls_nacht": dig(schlaf, "restingHeartRate"),
            },
            "hrv": {
                "letzte_nacht": dig(hrv, "hrvSummary", "lastNightAvg"),
                "woche_schnitt": dig(hrv, "hrvSummary", "weeklyAvg"),
                "status": dig(hrv, "hrvSummary", "status"),
                "basis_tief": dig(hrv, "hrvSummary", "baseline", "lowUpper"),
                "basis_hoch": dig(hrv, "hrvSummary", "baseline", "balancedUpper"),
            },
        }

        historie_tage[d] = eintrag

        if rohbeispiel is None and schlaf_dto:
            rohbeispiel = {
                "datum": d,
                "hinweis": "Rohantworten zur Feldpruefung, gekuerzt und "
                           "von Personenbezug bereinigt.",
                "get_stats_and_body": saeubern(stats),
                "get_sleep_data": saeubern(schlaf),
                "get_hrv_data": saeubern(hrv),
                "get_max_metrics": saeubern(maxmetrik),
                "get_body_battery_eintrag": saeubern(bb),
                "get_activities_by_date_eintrag": saeubern(
                    aktivitaeten[0] if aktivitaeten else {}
                ),
                "get_weigh_ins": saeubern(waagen),
            }

    if rohbeispiel and (os.getenv("RAW_SAMPLE", "ja").lower()
                        in ("ja", "true", "1", "yes")):
        RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_PATH.write_text(
            json.dumps(rohbeispiel, ensure_ascii=False, indent=1),
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
    # Exit-Code immer 0, damit der Commit-Schritt sicher laeuft.
    sys.exit(0)
