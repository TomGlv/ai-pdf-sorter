"""
Trieur de PDF manuscrits (feuilles de concours : doublettes, triplettes...).

Chaque PDF est lu par Claude (lecture de l'écriture manuscrite), puis copié dans :

    <sortie>/<année>/<type>/<fichier>.pdf
    <sortie>/<année>/À vérifier/<fichier>.pdf      (type introuvable ou incertain)
    <sortie>/Année inconnue/<type>/<fichier>.pdf

Un récapitulatif `recensement.csv` liste chaque fichier avec ce qui a été lu.
Les résultats sont gardés dans `.cache_lecture.json` : relancer le programme
ne repaie pas la lecture des fichiers déjà traités.

Usage :
    python trier_pdfs.py "C:\\dossier\\scans" "C:\\dossier\\trie"
    python trier_pdfs.py SOURCE SORTIE --deplacer     (déplace au lieu de copier)
    python trier_pdfs.py SOURCE SORTIE --essai        (affiche sans rien copier)
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

MODELE = "claude-opus-5-5"
TAILLE_MAX_PDF = 30 * 1024 * 1024  # limite de l'API : 32 Mo par requête

TYPES = {
    1: "Tête-à-tête",
    2: "Doublette",
    3: "Triplette",
    4: "Quadruplette",
    5: "Quintette",
    6: "Sextette",
}
A_VERIFIER = "À vérifier"
ANNEE_INCONNUE = "Année inconnue"


# ── Ce que Claude doit renvoyer ──────────────────────────────────────────────
class Lecture(BaseModel):
    transcription: str = Field(description="Texte manuscrit lu, tel quel (abréviations comprises).")
    nb_joueurs: Optional[int] = Field(
        description="Nombre de joueurs par équipe : 1 tête-à-tête, 2 doublette, 3 triplette, "
        "4 quadrette/quadruplette, 5... null si aucune mention."
    )
    indice: Optional[str] = Field(description="Mot exact qui indique le type (ex. « dbl », « x3 », « Triplette »).")
    plusieurs_types: bool = Field(description="true si le document mélange plusieurs types différents.")
    date: Optional[str] = Field(description="Date principale du document au format AAAA-MM-JJ (ou AAAA-MM, AAAA).")
    annee: Optional[int] = Field(description="Année du document (4 chiffres), null si introuvable.")
    confiance: Literal["haute", "moyenne", "basse"]


CONSIGNE = """Tu lis un scan de document manuscrit (souvent une feuille de concours de pétanque).
Transcris le texte, puis indique le type d'équipe mentionné et la date.

Le type est souvent abrégé. Exemples :
- doublette : « doublette », « doub », « dbl », « db », « x2 », « X2 », « 2x2 », « 2 joueurs »
- triplette : « triplette », « trip », « tpl », « tri », « x3 », « X3 », « 3x3 »
- quadrette / quadruplette : « quadrette », « quadruplette », « quad », « x4 », « X4 »
- tête-à-tête : « tête-à-tête », « tat », « TàT », « x1 », « simple »
Ne déduis pas le type d'un simple nombre isolé (score, numéro de terrain...).

Pour la date, une année à 2 chiffres (« 17 ») veut dire 2017.
Si une information est absente ou illisible, renvoie null et baisse la confiance."""


# ── Détection locale des abréviations, en secours de la lecture de Claude ───
MOTIFS = [
    (2, r"\bdoub\w*|\bdbl\b|\bdb\b|\b[xX]\s?2\b|\b2\s?[xX]\s?2\b"),
    (3, r"\btripl\w*|\btrip\b|\btpl\b|\b[xX]\s?3\b|\b3\s?[xX]\s?3\b"),
    (4, r"\bquad\w*|\b[xX]\s?4\b|\b4\s?[xX]\s?4\b"),
    (5, r"\bquint\w*|\b[xX]\s?5\b"),
    (1, r"t[êe]te[\s-]?[àa][\s-]?t[êe]te|\btat\b|\bt[àa]t\b"),
]


def types_dans_le_texte(texte: str) -> set[int]:
    return {n for n, motif in MOTIFS if re.search(motif, texte, re.IGNORECASE)}


# ── Lecture d'un PDF ─────────────────────────────────────────────────────────
def contenu_pdf(chemin: Path) -> list[dict]:
    """Le PDF entier s'il tient dans une requête, sinon ses 5 premières pages en images."""
    donnees = chemin.read_bytes()
    if len(donnees) <= TAILLE_MAX_PDF:
        return [{
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(donnees).decode()},
        }]
    import pymupdf

    blocs = []
    with pymupdf.open(chemin) as doc:
        for page in list(doc)[:5]:
            png = page.get_pixmap(dpi=150).tobytes("png")
            blocs.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                      "data": base64.standard_b64encode(png).decode()}})
    return blocs


def lire(client: anthropic.Anthropic, chemin: Path) -> Lecture | None:
    reponse = client.messages.parse(
        model=MODELE,
        max_tokens=16000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": [*contenu_pdf(chemin), {"type": "text", "text": CONSIGNE}]}],
        output_format=Lecture,
    )
    if reponse.stop_reason == "refusal":
        return None
    return reponse.parsed_output


# ── Classement ───────────────────────────────────────────────────────────────
def destination(lecture: Lecture | None) -> tuple[str, str, str]:
    """(dossier année, dossier type, remarque)"""
    if lecture is None:
        return ANNEE_INCONNUE, A_VERIFIER, "lecture impossible"

    annee = str(lecture.annee) if lecture.annee and 1950 <= lecture.annee <= 2100 else ANNEE_INCONNUE
    trouves = types_dans_le_texte(lecture.transcription)
    nb = lecture.nb_joueurs if lecture.nb_joueurs in TYPES else None

    if lecture.plusieurs_types or len(trouves) > 1:
        return annee, A_VERIFIER, "plusieurs types dans le document"
    if nb is None and len(trouves) == 1:
        nb = trouves.pop()  # Claude n'a rien conclu mais l'abréviation est dans le texte
    if nb is None:
        return annee, A_VERIFIER, "type non trouvé"
    if lecture.confiance == "basse":
        return annee, A_VERIFIER, f"lecture incertaine (probablement {TYPES[nb]})"
    if trouves and nb not in trouves:
        return annee, A_VERIFIER, f"désaccord : {TYPES[nb]} lu, mais le texte évoque {TYPES[trouves.pop()]}"
    return annee, TYPES[nb], ""


def chemin_libre(cible: Path) -> Path:
    """Évite d'écraser un fichier du même nom déjà rangé."""
    n = 2
    base = cible
    while cible.exists():
        cible = base.with_name(f"{base.stem} ({n}){base.suffix}")
        n += 1
    return cible


def empreinte(chemin: Path) -> str:
    return hashlib.sha256(chemin.read_bytes()).hexdigest()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # accents corrects dans la console Windows
    p = argparse.ArgumentParser(description="Trie des PDF manuscrits par année et par type d'équipe.")
    p.add_argument("source", type=Path, help="Dossier contenant les PDF (sous-dossiers compris)")
    p.add_argument("sortie", type=Path, help="Dossier où ranger les PDF triés")
    p.add_argument("--deplacer", action="store_true", help="Déplacer les fichiers au lieu de les copier")
    p.add_argument("--essai", action="store_true", help="Lire et afficher le classement sans rien copier")
    args = p.parse_args()

    if not args.source.is_dir():
        sys.exit(f"Dossier introuvable : {args.source}")
    sortie = args.sortie.resolve()
    pdfs = sorted(f for f in args.source.rglob("*") if f.suffix.lower() == ".pdf"
                  and sortie not in f.resolve().parents)
    if not pdfs:
        sys.exit("Aucun PDF trouvé.")

    sortie.mkdir(parents=True, exist_ok=True)
    fichier_cache = sortie / ".cache_lecture.json"
    cache = json.loads(fichier_cache.read_text("utf-8")) if fichier_cache.exists() else {}
    client = anthropic.Anthropic()  # clé lue dans ANTHROPIC_API_KEY
    lignes = []

    print(f"{len(pdfs)} PDF à traiter.\n")
    for i, pdf in enumerate(pdfs, 1):
        cle = empreinte(pdf)
        if cle in cache:
            lecture = Lecture.model_validate(cache[cle]) if cache[cle] else None
        else:
            try:
                lecture = lire(client, pdf)
            except anthropic.APIConnectionError as e:
                print(f"[{i}/{len(pdfs)}] {pdf.name} : connexion impossible ({e}), fichier ignoré")
                continue
            except anthropic.APIStatusError as e:
                print(f"[{i}/{len(pdfs)}] {pdf.name} : erreur API {e.status_code}, fichier ignoré")
                if e.status_code in (401, 403):
                    sys.exit("Clé API invalide : vérifie ANTHROPIC_API_KEY.")
                continue
            cache[cle] = lecture.model_dump() if lecture else None
            fichier_cache.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")

        annee, type_, remarque = destination(lecture)
        cible = sortie / annee / type_ / pdf.name
        print(f"[{i}/{len(pdfs)}] {pdf.name} -> {annee}/{type_}" + (f"  ({remarque})" if remarque else ""))

        if not args.essai:
            cible = chemin_libre(cible)
            cible.parent.mkdir(parents=True, exist_ok=True)
            (shutil.move if args.deplacer else shutil.copy2)(pdf, cible)

        lignes.append({
            "fichier": pdf.name,
            "rangé dans": str(cible.relative_to(sortie)),
            "année": annee,
            "type": type_,
            "date lue": lecture.date if lecture else "",
            "mot repéré": lecture.indice if lecture else "",
            "confiance": lecture.confiance if lecture else "",
            "remarque": remarque,
            "transcription": (lecture.transcription if lecture else "").replace("\n", " / ")[:500],
        })

    if lignes:
        # Classé par année puis par date ; « utf-8-sig » + « ; » pour qu'Excel l'ouvre correctement
        lignes.sort(key=lambda l: (l["année"], l["date lue"] or "", l["fichier"]))
        with open(sortie / "recensement.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(lignes[0]), delimiter=";")
            w.writeheader()
            w.writerows(lignes)

    a_verifier = sum(l["type"] == A_VERIFIER for l in lignes)
    print(f"\nTerminé : {len(lignes)} fichiers classés, dont {a_verifier} à vérifier.")
    print(f"Récapitulatif : {sortie / 'recensement.csv'}")


if __name__ == "__main__":
    main()
