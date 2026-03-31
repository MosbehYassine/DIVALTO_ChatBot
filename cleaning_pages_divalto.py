"""
cleaning_pages_divalto.py
=========================
Nettoyage des pages Confluence Divalto scrapées.

Stratégie :
  1. Suppression des pages vides (body vide ou header seul)
  2. Suppression des pages placeholder
  3. Suppression des pages < 50 mots réels (navigation, liens seuls...)
  4. Suppression des pages "atomisées" SW (chaque mot sur une ligne, ratio mots courts > 60%)
  5. Suppression des pages navigation anchors (Top_of_, #anchor, liens internes seuls)
  6. Déduplication exacte (hash du body) au sein de chaque space
  7. Conservation de toutes les versions UDI/UDW (chaque space est indépendante)

Structure attendue en entrée :
  C:\\Users\\Ameni Mejri\\Documents\\chatbot-divalto\\data\\
      UDI113\\ ...
      UDI112\\ ...
      PAI\\    ...
      UDW63\\ ...
      VP\\    ...
      ...

Structure de sortie :
  C:\\Users\\Ameni Mejri\\Documents\\chatbot-divalto\\cleaned_data\\
      UDI113\\ ...
      PAI\\    ...
      ...

Usage :
  py cleaning_pages_divalto.py
  -- ou avec chemins personnalisés --
  py cleaning_pages_divalto.py --input "C:\\chemin\\vers\\data" --output "C:\\chemin\\vers\\cleaned_data"
"""

import os
import re
import hashlib
import argparse
import shutil
from pathlib import Path
from collections import defaultdict


# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
MIN_REAL_WORDS = 50          # seuil mots réels (hors chiffres / mots ≤ 2 chars)
HEADER_SEPARATOR = "=" * 20  # séparateur header / body dans les .txt

PLACEHOLDER_PATTERNS = [
    r"temporary placeholder",
    r"copy placeholder",
    r"will be deleted",
    r"à compléter",
    r"en cours de rédaction",
    r"coming soon",
    r"todo",
]
PLACEHOLDER_RE = re.compile("|".join(PLACEHOLDER_PATTERNS), re.IGNORECASE)

# Ratio max de "tokens courts" (≤ 3 chars) avant de considérer la page atomisée (SW-style)
ATOMIZED_SHORT_RATIO = 0.60   # 60% des tokens sont courts → page atomisée

# Ratio max de lignes "anchor" avant de considérer la page navigation-only
ANCHOR_LINE_RATIO = 0.50      # 50% des lignes non-vides sont des anchors → navigation
ANCHOR_RE = re.compile(r"^(Top_of_|#[A-Za-z]|\[.*?\]\(#)", re.IGNORECASE)


# ──────────────────────────────────────────────
# PARSING
# ──────────────────────────────────────────────
def parse_file(path: Path) -> tuple[dict, str]:
    """Retourne (header_dict, body_str)."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    header = {}
    body_lines = []
    in_body = False

    for line in text.splitlines():
        if not in_body:
            if line.startswith("TITRE:"):
                header["titre"] = line[6:].strip()
            elif line.startswith("URL:"):
                header["url"] = line[4:].strip()
            elif line.startswith("ESPACE:"):
                header["espace"] = line[7:].strip()
            elif line.startswith("MAJ:"):
                header["maj"] = line[4:].strip()
            elif HEADER_SEPARATOR in line:
                in_body = True
        else:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()
    return header, body


# ──────────────────────────────────────────────
# FILTRES
# ──────────────────────────────────────────────
def count_real_words(text: str) -> int:
    """Mots de plus de 2 caractères, non numériques."""
    words = text.split()
    return sum(1 for w in words if len(w) > 2 and not w.isdigit())


def is_atomized(body: str) -> bool:
    """
    Détecte les pages SW-style ET navigation-anchors.
    Signe commun : la majorité des lignes contiennent 1 ou 2 mots seulement
    (chaque token sur sa propre ligne, ou anchors Top_of_).

    Ex SW :
      SW\n,\n186\nActivité\npartielle\nCalcul\ndu\nnb...
    Ex Nav :
      Top_of_Option__Resterencreation\nOption " Rester en création "\nVoir\n...
    """
    non_empty = [l.strip() for l in body.splitlines() if l.strip()]
    if len(non_empty) < 10:
        return False
    # Protection : pages avec beaucoup de contenu réel
    real_words = sum(1 for w in body.split() if len(w) > 3 and not w.isdigit())
    if real_words > 200:
        return False
    # Ratio de lignes courtes (≤ 2 mots)
    short_lines = sum(1 for l in non_empty if len(l.split()) <= 2)
    return (short_lines / len(non_empty)) > 0.60


def is_navigation_anchors(body: str) -> bool:
    """
    Détecte les pages qui contiennent des anchors Top_of_ mélangés
    avec du contenu atomisé (Options + Top_of_ + Voir + ...).
    """
    non_empty = [l.strip() for l in body.splitlines() if l.strip()]
    if not non_empty:
        return False
    real_words = sum(1 for w in body.split() if len(w) > 3 and not w.isdigit())
    if real_words > 200:
        return False
    top_of_lines = sum(1 for l in non_empty if l.startswith('Top_of_'))
    return top_of_lines >= 2


def body_hash(body: str) -> str:
    normalized = re.sub(r"\s+", " ", body).strip().lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def should_remove(body: str) -> tuple[bool, str]:
    """
    Retourne (True, raison) si la page doit être supprimée.
    """
    # 1. Corps vide
    if not body or len(body.strip()) < 10:
        return True, "empty_body"

    # 2. Placeholder
    if PLACEHOLDER_RE.search(body):
        return True, "placeholder"

    # 3. Trop peu de mots réels
    if count_real_words(body) < MIN_REAL_WORDS:
        return True, f"too_short ({count_real_words(body)} real words)"

    # 4. Page atomisée SW-style (tokens éparpillés ligne par ligne)
    if is_atomized(body):
        return True, "atomized_sw"

    # 5. Page navigation anchors (Top_of_, #anchor...)
    if is_navigation_anchors(body):
        return True, "navigation_anchors"

    return False, ""


# ──────────────────────────────────────────────
# TRAITEMENT D'UN SPACE
# ──────────────────────────────────────────────
def process_space(space_dir: Path, output_space_dir: Path) -> dict:
    """
    Nettoie toutes les pages d'un space.
    Retourne un dict de stats.
    """
    stats = defaultdict(int)
    seen_hashes = set()   # déduplication intra-space

    output_space_dir.mkdir(parents=True, exist_ok=True)

    for txt_file in sorted(space_dir.glob("*.txt")):
        stats["total"] += 1
        header, body = parse_file(txt_file)

        # --- filtres qualité ---
        remove, reason = should_remove(body)
        if remove:
            stats[f"removed_{reason.split(' ')[0]}"] += 1
            stats["removed_total"] += 1
            continue

        # --- déduplication exacte intra-space ---
        h = body_hash(body)
        if h in seen_hashes:
            stats["removed_duplicate"] += 1
            stats["removed_total"] += 1
            continue
        seen_hashes.add(h)

        # --- page conservée ---
        shutil.copy2(txt_file, output_space_dir / txt_file.name)
        stats["kept"] += 1

    return dict(stats)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    # Chemins par défaut adaptés à ton projet
    default_input  = Path(r"C:\Users\Ameni Mejri\Documents\chatbot-divalto\divalto_data\pages")
    default_output = Path(r"C:\Users\Ameni Mejri\Documents\chatbot-divalto\divalto_data\cleaned_pages")

    parser = argparse.ArgumentParser(description="Nettoyage pages Divalto Confluence")
    parser.add_argument("--input",  default=str(default_input),
                        help=f"Dossier contenant les spaces (défaut: {default_input})")
    parser.add_argument("--output", default=str(default_output),
                        help=f"Dossier de sortie nettoyé (défaut: {default_output})")
    args = parser.parse_args()

    input_root  = Path(args.input)
    output_root = Path(args.output)

    if not input_root.exists():
        print(f"[ERREUR] Dossier introuvable : {input_root}")
        return

    # Récupère directement tous les sous-dossiers (= spaces) dans data/
    spaces = sorted([d for d in input_root.iterdir() if d.is_dir()])

    if not spaces:
        print(f"[ERREUR] Aucun space trouvé dans {input_root}")
        return

    grand_total = grand_kept = grand_removed = 0
    removal_reasons = defaultdict(int)

    print("=" * 60)
    print("  Nettoyage pages Divalto Confluence")
    print("=" * 60)
    print(f"  Input  : {input_root}")
    print(f"  Output : {output_root}")
    print("=" * 60)

    for space_dir in spaces:
        space_name  = space_dir.name
        output_space = output_root / space_name

        stats = process_space(space_dir, output_space)

        total   = stats.get("total", 0)
        kept    = stats.get("kept", 0)
        removed = stats.get("removed_total", 0)

        grand_total   += total
        grand_kept    += kept
        grand_removed += removed

        for k, v in stats.items():
            if k.startswith("removed_") and k != "removed_total":
                removal_reasons[k] += v

        pct = f"{kept/total*100:.1f}%" if total else "N/A"
        print(f"  {space_name:<12} | total: {total:>5} | kept: {kept:>5} ({pct}) | removed: {removed:>4}")

    # ── Résumé final ──
    print("\n" + "=" * 60)
    print("  RÉSUMÉ FINAL")
    print("=" * 60)
    print(f"  Total pages analysées : {grand_total:>6}")
    print(f"  Pages conservées      : {grand_kept:>6}  ({grand_kept/grand_total*100:.1f}%)")
    print(f"  Pages supprimées      : {grand_removed:>6}  ({grand_removed/grand_total*100:.1f}%)")
    print()
    print("  Détail des suppressions :")
    reason_labels = {
        "removed_empty_body":         "Corps vide",
        "removed_placeholder":        "Placeholder",
        "removed_too_short":          "Trop court (< 50 mots réels)",
        "removed_atomized_sw":        "Atomisé SW (tokens éparpillés)",
        "removed_navigation_anchors": "Navigation anchors (Top_of_...)",
        "removed_duplicate":          "Doublon exact (intra-space)",
    }
    for key, label in reason_labels.items():
        count = removal_reasons.get(key, 0)
        if count:
            print(f"    • {label:<35} : {count}")
    print()
    print(f"  ✅ Pages nettoyées sauvegardées dans : {output_root}")
    print("=" * 60)


if __name__ == "__main__":
    main()