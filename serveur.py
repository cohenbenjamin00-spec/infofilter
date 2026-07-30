# -*- coding: utf-8 -*-
"""
InfoFilter - Petit serveur local (version Gemini).

Il fait deux choses :
  1. Il affiche la page de l'appli (interface.html) sur http://localhost:8000
  2. Il repond aux questions libres tapees sous une depeche, en cherchant la
     reponse dans les messages Telegram du jour (base messages.db), via Gemini.

Regle (fidele a la vision) : on repond a partir des messages ; si l'info n'y
est pas, on le dit clairement. On n'invente jamais.

Pour lancer :
    py serveur.py
puis ouvrir http://localhost:8000 dans le navigateur.

La cle Gemini se met simplement dans le fichier  cle-gemini.txt  (a cote de ce
fichier). Sans cle valide, la page marche mais les questions libres affichent un
message d'explication.
"""

import json
import sqlite3
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google import genai
from google.genai import types, errors

# ---------------------------------------------------------------------------
# REGLAGES
# ---------------------------------------------------------------------------
ICI = os.path.dirname(os.path.abspath(__file__))
# En local : http://localhost:8000. En ligne : l'hebergeur fixe PORT/HOST.
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
FICHIER_DB = os.path.join(ICI, "messages.db")
FICHIER_PAGE = os.path.join(ICI, "interface.html")
FICHIER_CLE = os.path.join(ICI, "cle-gemini.txt")
DOSSIER_JOURS = os.path.join(ICI, "jours")   # un fichier JSON par jour, ecrit par Claude
# Gemini sert UNIQUEMENT aux questions libres (gratuit). On essaie ces modeles
# dans l'ordre : si l'un a atteint son quota du jour, on passe au suivant.
MODELES_QUESTIONS = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.0-flash"]
PLACEHOLDER = "colle-ta-cle-gemini-ici"

SKELETON = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>InfoFilter — 29 juillet 2026</title></head>
<body>{contenu}</body></html>"""


def lire_cle():
    """Lit la cle depuis cle-gemini.txt, sinon la variable GEMINI_API_KEY."""
    if os.path.exists(FICHIER_CLE):
        val = open(FICHIER_CLE, encoding="utf-8").read().strip()
        if val and val != PLACEHOLDER:
            return val
    return os.environ.get("GEMINI_API_KEY", "").strip() or None


def charger_digests():
    """Charge les digests de chaque jour. En priorite un seul fichier
    digests.json (pratique pour l'hebergement) ; sinon le dossier 'jours'
    (un fichier 2026-07-JJ.json par jour). {} si rien."""
    fichier_unique = os.path.join(ICI, "digests.json")
    if os.path.exists(fichier_unique):
        try:
            return json.load(open(fichier_unique, encoding="utf-8"))
        except Exception:
            pass
    digests = {}
    if os.path.isdir(DOSSIER_JOURS):
        for nom in sorted(os.listdir(DOSSIER_JOURS)):
            if nom.endswith(".json"):
                date = nom[:-5]
                try:
                    data = json.load(open(os.path.join(DOSSIER_JOURS, nom), encoding="utf-8"))
                    depeches = data.get("depeches", data) if isinstance(data, dict) else data
                    if depeches:
                        digests[date] = depeches
                except Exception:
                    pass
    return digests


def repondre(cle, topic, fait, question):
    """Repond a la question libre via Gemini (offre gratuite). Essaie plusieurs
    modeles gratuits pour maximiser la disponibilite si l'un a atteint son
    quota du jour."""
    consigne = (
        "Tu es l'assistant d'InfoFilter. Un lecteur lit une depeche d'actualite "
        "et pose une question. La depeche te donne seulement le CONTEXTE (le "
        "sujet) ; tu reponds a la question en t'appuyant sur tes CONNAISSANCES "
        "GENERALES et des informations fiables, pas seulement sur le texte de la "
        "depeche.\n\n"
        "Regles :\n"
        "- Reponds en francais, de facon factuelle, en 2 a 5 phrases.\n"
        "- Utilise tes connaissances pour repondre vraiment a la question.\n"
        "- N'invente pas de faits. Si tu ignores la reponse (par exemple un "
        "detail tres recent), dis-le clairement.\n"
        "- Pas d'opinion : seulement les faits."
    )
    question_complete = (
        f"Contexte de la depeche : « {fait} » (theme : {topic}).\n\n"
        f"Question : {question}"
    )

    client = genai.Client(api_key=cle)
    dernier = None
    for modele in MODELES_QUESTIONS:
        try:
            r = client.models.generate_content(
                model=modele,
                contents=question_complete,
                config=types.GenerateContentConfig(
                    system_instruction=consigne, temperature=0.2),
            )
            return (r.text or "").strip() or "Je n'ai pas trouve de reponse."
        except errors.APIError as e:
            dernier = e
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                continue   # quota de ce modele atteint : on essaie le suivant
            raise
    # Tous les modeles gratuits ont atteint leur quota du jour.
    raise dernier


class Handler(BaseHTTPRequestHandler):
    def _envoyer(self, code, corps, ctype="application/json; charset=utf-8"):
        data = corps.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(FICHIER_PAGE, encoding="utf-8") as f:
                contenu = f.read()
            # On injecte les digests de chaque jour dans la page.
            digests = json.dumps(charger_digests(), ensure_ascii=False).replace("</", "<\\/")
            data_script = "<script>window.DIGESTS = " + digests + ";</script>\n"
            self._envoyer(200, SKELETON.format(contenu=data_script + contenu),
                          "text/html; charset=utf-8")
        else:
            self._envoyer(404, json.dumps({"erreur": "page inconnue"}))

    def do_POST(self):
        if self.path != "/api/demander":
            self._envoyer(404, json.dumps({"erreur": "route inconnue"}))
            return
        try:
            taille = int(self.headers.get("Content-Length", 0))
            donnees = json.loads(self.rfile.read(taille).decode("utf-8"))
            question = (donnees.get("question") or "").strip()
            fait = donnees.get("fait", "")
            topic = donnees.get("topic", "")
            if not question:
                self._envoyer(200, json.dumps({"reponse": "Pose une question d'abord."}))
                return

            cle = lire_cle()
            if not cle:
                self._envoyer(200, json.dumps({"reponse":
                    "La cle Gemini n'est pas encore mise. Ouvre le fichier "
                    "cle-gemini.txt, colle ta cle dedans, enregistre, puis relance "
                    "le serveur."}))
                return

            reponse = repondre(cle, topic, fait, question)
            self._envoyer(200, json.dumps({"reponse": reponse}))

        except errors.APIError as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                self._envoyer(200, json.dumps({"reponse":
                    "Le quota gratuit de questions Gemini est atteint pour "
                    "aujourd'hui. Il se remet a zero chaque jour ; reessaie plus "
                    "tard. (Les depeches, elles, restent disponibles.)"}))
            elif "API_KEY" in msg or "API key" in msg or "401" in msg or "403" in msg:
                self._envoyer(200, json.dumps({"reponse":
                    "La cle Gemini a ete refusee (cle invalide). Verifie le "
                    "contenu de cle-gemini.txt."}))
            else:
                self._envoyer(200, json.dumps({"reponse": "Erreur Gemini : " + msg}))
        except Exception as e:
            self._envoyer(200, json.dumps({"reponse":
                "Desole, une erreur est survenue : " + str(e)}))

    def log_message(self, *args):
        pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    serveur = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 60)
    print("InfoFilter tourne. Ouvre cette adresse dans ton navigateur :")
    print(f"    http://localhost:{PORT}")
    cle = "trouvee" if lire_cle() else "MANQUANTE (mets-la dans cle-gemini.txt)"
    print(f"Cle Gemini (questions) : {cle}   |   Digests : ecrits par Claude")
    print("Pour arreter : ferme cette fenetre ou appuie sur Ctrl+C.")
    print("=" * 60)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nServeur arrete.")


if __name__ == "__main__":
    main()
