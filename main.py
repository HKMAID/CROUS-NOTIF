""""
    python3 main.py --test     # test spam
    python3 main.py            # lancer le  script
"""

import argparse
import json
import logging
import os
import random
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

SEARCH_URL = os.environ.get(
    "CROUS_SEARCH_URL",
    "https://trouverunlogement.lescrous.fr/tools/47/search"
    "?occupationModes=alone&occupationModes=house_sharing"
    "&bounds=1.0587473_49.483705_1.10246_49.4508448"
    "&locationName=Mont-Saint-Aignan+%2876130%29",
)

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 30))

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")          # adresse mail 
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")  # mdp
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER)  # email notif

STATE_FILE = os.environ.get("STATE_FILE", "crous_state.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
OVERLOAD_MARKER = "vous etes trop nombreux"
NO_NEW_EMAIL_EVERY = int(os.environ.get("NO_NEW_EMAIL_EVERY", 14))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("main")


def send_email(subject: str, body_html: str) -> bool:
    if not SMTP_USER or not SMTP_PASSWORD:
        log.error("SMTP_USER / SMTP_PASSWORD non définis — email non envoyé.")
        return False
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        log.info("Email envoyé : %s", subject)
        return True
    except Exception as e:
        log.error("Échec de l'envoi de l'email : %s", e)
        return False


def fetch_listings() -> dict:
    """Récupère la page de résultats et retourne {id: {title, url}}."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"}
    resp = requests.get(SEARCH_URL, headers=headers, timeout=15)
    resp.raise_for_status()

    if OVERLOAD_MARKER in resp.text.lower():
        raise RuntimeError("Page de surcharge CROUS ('trop nombreux accès'), réessai plus tard.")

    soup = BeautifulSoup(resp.text, "html.parser")
    listings = {}
    for link in soup.select("a[href*='/accommodations/']"):
        href = link.get("href")
        if not href:
            continue
        full_url = href if href.startswith("http") else f"https://trouverunlogement.lescrous.fr{href}"
        listing_id = href.rstrip("/").split("/")[-1]
        title = link.get_text(strip=True) or f"Logement {listing_id}"
        listings[listing_id] = {"title": title, "url": full_url}
    return listings


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None  # None = premier lancement


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def check_once(state: dict) -> dict:
    listings = fetch_listings()
    current_ids = set(listings.keys())
    seen_ids = set(state.get("seen_ids", []))
    new_ids = current_ids - seen_ids

    if new_ids:
        items_html = "".join(
            f"<li><a href='{listings[i]['url']}'>{listings[i]['title']}</a></li>" for i in new_ids
        )
        body = (
            f"<p>{len(new_ids)} nouveau(x) logement(s) détecté(s) :</p>"
            f"<ul>{items_html}</ul>"
            f"<p>Lien de recherche : <a href='{SEARCH_URL}'>{SEARCH_URL}</a></p>"
        )
        send_email(f"🏠 {len(new_ids)} nouveau(x) logement(s) CROUS disponible(s)", body)
        log.info("%d nouveau(x) logement(s) — email envoyé.", len(new_ids))
        state["no_new_count"] = 0 
    else:
        no_new_count = state.get("no_new_count", 0) + 1
        log.info(
            "Aucun nouveau logement (%d actuellement listé(s)). [%d/%d itérations]",
            len(current_ids), no_new_count, NO_NEW_EMAIL_EVERY,
        )
        if no_new_count >= NO_NEW_EMAIL_EVERY:
            send_email(
                "CROUS76 — rapport quotidien",
                "<p>Toujours aucun nouveau logement trouvé.</p>",
            )
            no_new_count = 0
        state["no_new_count"] = no_new_count

    state["seen_ids"] = list(current_ids)
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Envoie un email de test et quitte")
    parser.add_argument(
        "--once", action="store_true",
        help="Effectue une seule vérification puis quitte (utilisé par GitHub Actions / cron)",
    )
    args = parser.parse_args()

    if args.test:
        ok = send_email(
            "Test — CROUS76",
            "<p>Ceci est un email de test. Si tu le reçois (même dans les spams, "
            "marque-le comme 'non spam'), la config SMTP fonctionne.</p>",
        )
        raise SystemExit(0 if ok else 1)

    if "REMPLACE_MOI" in SEARCH_URL:
        log.error("SEARCH_URL n'a pas été configuré. Édite le script ou la variable "
                   "d'environnement CROUS_SEARCH_URL.")
        raise SystemExit(1)

    state = load_state()
    first_run = state is None
    if first_run:
        state = {"seen_ids": []}
        # Email de test au tout premier lancement, pour vérifier la config
        # et habituer la boîte mail à recevoir ces messages (anti-spam).
        send_email(
            "CROUS76 lancé",
            "<p>La surveillance vient de lacer. Tu recevras un email dès qu'un "
            "nouveau logement apparaîtra dans tes résultats.</p>",
        )

    if args.once:
        # Mode "une seule vérification" : utilisé par un planificateur externe
        try:
            state = check_once(state)
            save_state(state)
        except requests.RequestException as e:
            log.warning("Erreur réseau : %s", e)
            raise SystemExit(1)
        except RuntimeError as e:
            log.warning(str(e))
        raise SystemExit(0)

    log.info("Démarrage — vérification toutes les %ds. URL : %s", CHECK_INTERVAL, SEARCH_URL)

    while True:
        try:
            state = check_once(state)
            save_state(state)
        except requests.RequestException as e:
            log.warning("Erreur réseau : %s", e)
        except RuntimeError as e:
            log.warning(str(e))
        except Exception:
            log.exception("Erreur inattendue")

        time.sleep(max(5, CHECK_INTERVAL + random.uniform(-2, 2)))


if __name__ == "__main__":
    main()
