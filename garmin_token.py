#!/usr/bin/env python3
"""
garmin_token.py  ·  NUR EINMAL LOKAL AUSFUEHREN - nicht in GitHub Actions.

Wird gebraucht, wenn auf dem Garmin-Konto die Zwei-Faktor-Bestaetigung (MFA)
aktiv ist. Das Skript meldet sich einmal an, fragt den MFA-Code ab und gibt
am Ende eine lange Base64-Zeichenkette aus. Diese Zeichenkette kommt als
GitHub-Secret GARMIN_TOKENS_B64 ins Repository - danach laeuft der naechtliche
Export ohne Passwort und ohne MFA-Abfrage.

Vorbereitung (Windows, einmalig):
    1. Python von python.org installieren (Haken bei "Add to PATH")
    2. Eingabeaufforderung oeffnen
    3. pip install garminconnect==0.3.9
    4. python garmin_token.py

Wichtig: Die ausgegebene Zeichenkette ist ein Zugangsschluessel zum
Garmin-Konto. Nicht in einen Chat kopieren, nicht ins Repository schreiben,
nur ins Secret-Feld bei GitHub einfuegen.
"""

import base64
import getpass

from garminconnect import Garmin


def main():
    email = input("Garmin-Connect-E-Mail: ").strip()
    passwort = getpass.getpass("Passwort (Eingabe bleibt unsichtbar): ")

    garmin = Garmin(
        email,
        passwort,
        prompt_mfa=lambda: input(
            "MFA-Code (aus E-Mail oder Authenticator-App): "
        ).strip(),
    )
    garmin.login()

    tokens = garmin.client.dumps()
    kodiert = base64.b64encode(tokens.encode("utf-8")).decode("ascii")

    print("\nAnmeldung erfolgreich.")
    print("Angemeldet als:", garmin.get_full_name())
    print("\n--- Ab hier kopieren (eine einzige lange Zeile) ---\n")
    print(kodiert)
    print("\n--- Bis hier kopieren ---\n")
    print("Bei GitHub einfuegen unter:")
    print("Settings -> Secrets and variables -> Actions -> New repository secret")
    print("Name: GARMIN_TOKENS_B64")


if __name__ == "__main__":
    main()
