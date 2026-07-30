# -*- coding: utf-8 -*-
"""
Ajoute le digest du jour a digests.json.
- Recupere les messages du jour sur la page publique Telegram @MKINFOSWORLD.
- Fait rediger le digest par Gemini (cle dans la variable GEMINI_API_KEY).
- Met a jour digests.json.
Lance automatiquement chaque jour par GitHub Actions.
"""

import os
import sys
import re
import json
import time
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

from google import genai
from google.genai import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CANAL = "MKINFOSWORLD"
BASE = "https://t.me/s/" + CANAL
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
ICI = os.path.dirname(os.path.abspath(__file__))
FICHIER = os.path.join(ICI, "digests.json")
MODELES = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.0-flash"]

RE_MSG = re.compile(r'<div class="tgme_widget_message[^"]*" data-post="' + CANAL + r'/(\d+)"')
RE_DATE = re.compile(r'<time datetime="([^"]+)"')

CONSIGNE = (
    "Tu es l'editeur d'InfoFilter. A partir des messages Telegram bruts d'une "
    "journee (chaine d'actualite israelienne), produis le digest des evenements "
    "les plus importants du jour.\n\n"
    "Regles :\n"
    "- Regroupe en UN seul evenement les messages qui parlent du meme sujet ; en "
    "cas de contradiction, garde le plus recent.\n"
    "- Garde le FAIT, retire tout commentaire, opinion, emoji, « URGENT », "
    "« partagez ».\n"
    "- Note chaque evenement de 0 a 10 selon son importance ; garde les 5 mieux "
    "notes (moins de 5 si journee calme ; jamais de remplissage).\n"
    "- Pour chaque evenement : \"fait\" (UNE phrase factuelle de 25 mots maximum, "
    "comprehensible seule, chiffres/noms/lieux, sans commentaire), \"topic\" (2 a "
    "4 mots, ex. « Iran · Etats-Unis »), \"score\" (entier 0-10), \"questions\" "
    "(3 ou 4 questions, chacune {\"q\":\"...\",\"a\":\"...\"}, reponse fondee "
    "UNIQUEMENT sur les messages du jour, 1 a 3 phrases ; si l'info n'y est pas : "
    "« Cette information n'apparait pas dans les messages du jour. »).\n"
    "- Classe les depeches par score decroissant.\n\n"
    "Reponds UNIQUEMENT en JSON : {\"depeches\": [ {\"score\": 9, \"topic\": "
    "\"...\", \"fait\": \"...\", \"questions\": [ {\"q\": \"...\", \"a\": \"...\"} ] } ] }"
)


class ExtracteurTexte(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cap = False; self.prof = 0; self.buf = []
    def handle_starttag(self, t, a):
        classes = dict(a).get("class", "")
        if not self.cap and t == "div" and "js-message_text" in classes:
            self.cap = True; self.prof = 1; return
        if self.cap:
            if t == "br": self.buf.append("\n")
            elif t == "div": self.prof += 1
    def handle_endtag(self, t):
        if self.cap and t == "div":
            self.prof -= 1
            if self.prof == 0: self.cap = False
    def handle_data(self, d):
        if self.cap: self.buf.append(d)
    def texte(self):
        return "".join(self.buf).strip()


def telecharger(url, essais=4):
    derniere = None
    for tentative in range(1, essais + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
        except Exception as e:
            derniere = e
            time.sleep(3 * tentative)
    raise derniere


def messages_du_jour(jour):
    """Renvoie [(numero, 'HH:MM', texte), ...] pour la date 'AAAA-MM-JJ'."""
    url = BASE
    vus = set(); msgs = []; dernier = None
    for _ in range(80):
        page = telecharger(url)
        positions = [(m.start(), int(m.group(1))) for m in RE_MSG.finditer(page)]
        if not positions:
            break
        ids = [p[1] for p in positions]
        min_id = min(ids)
        plus_vieux = False
        for i, (deb, num) in enumerate(positions):
            fin = positions[i + 1][0] if i + 1 < len(positions) else len(page)
            bloc = page[deb:fin]
            md = RE_DATE.search(bloc)
            d = md.group(1) if md else ""
            if d[:10] == jour:
                if num not in vus:
                    e = ExtracteurTexte(); e.feed(bloc); t = e.texte()
                    if t:
                        msgs.append((num, d[11:16], t)); vus.add(num)
            elif d[:10] and d[:10] < jour:
                plus_vieux = True
        if plus_vieux:
            break
        if dernier is not None and min_id >= dernier:
            break
        dernier = min_id
        url = f"{BASE}?before={min_id}"
        time.sleep(1.5)
    msgs.sort()
    return msgs


def rediger_digest(cle, jour, msgs):
    contexte = "\n\n".join(f"[{n} — {h}] {t}" for n, h, t in msgs)
    client = genai.Client(api_key=cle)
    for modele in MODELES:
        try:
            r = client.models.generate_content(
                model=modele,
                contents=f"Date : {jour}\n\nMessages du jour :\n{contexte}",
                config=types.GenerateContentConfig(
                    system_instruction=CONSIGNE, temperature=0.3,
                    max_output_tokens=8192, response_mime_type="application/json"),
            )
            data = json.loads((r.text or "").strip())
            dep = data.get("depeches", [])[:5]
            return [{
                "score": int(d.get("score", 0)),
                "topic": str(d.get("topic", "")).strip(),
                "fait": str(d.get("fait", "")).strip(),
                "questions": [{"q": str(q.get("q", "")).strip(), "a": str(q.get("a", "")).strip()}
                              for q in d.get("questions", []) if q.get("q")],
            } for d in dep]
        except Exception as e:
            s = str(e)
            if "429" in s or "RESOURCE_EXHAUSTED" in s:
                continue
            raise
    raise RuntimeError("Tous les modeles Gemini gratuits ont atteint leur quota du jour.")


def main():
    cle = os.environ.get("GEMINI_API_KEY", "").strip()
    if not cle:
        print("Cle GEMINI_API_KEY manquante — rien fait.")
        return
    jour = os.environ.get("JOUR_TEST", "").strip() or \
        datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print("Jour a traiter :", jour)
    msgs = messages_du_jour(jour)
    print("Messages avec texte trouves :", len(msgs))
    if not msgs:
        print("Aucun message pour ce jour — rien a ajouter.")
        return
    dep = rediger_digest(cle, jour, msgs)
    if not dep:
        print("Digest vide — rien a ajouter.")
        return
    digests = {}
    if os.path.exists(FICHIER):
        digests = json.load(open(FICHIER, encoding="utf-8"))
    digests[jour] = dep
    json.dump(digests, open(FICHIER, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"digests.json mis a jour : {len(dep)} depeches pour {jour}.")


if __name__ == "__main__":
    main()
