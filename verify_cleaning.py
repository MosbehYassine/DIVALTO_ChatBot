"""
verify_cleaning_divalto.py
==========================
Rapport de vérification de la qualité du nettoyage Divalto.

Vérifie :
  1. Stats globales (total, par space)
  2. Détection de pages suspectes encore présentes
  3. Distribution de la longueur des pages
  4. Échantillon de pages courtes (à vérifier manuellement)
  5. Vérification qu'aucune page vide / placeholder ne reste

Usage :
  py verify_cleaning_divalto.py
  py verify_cleaning_divalto.py --cleaned "C:\\chemin\\vers\\cleaned_data"
"""

import re
import argparse
from pathlib import Path
from collections import defaultdict


# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DEFAULT_CLEANED = Path(r"C:\Users\Ameni Mejri\Documents\chatbot-divalto\divalto_data\cleaned_pages")

PLACEHOLDER_PATTERNS = [
    r"temporary placeholder", r"copy placeholder", r"will be deleted",
    r"à compléter", r"en cours de rédaction", r"coming soon",
]
PLACEHOLDER_RE = re.compile("|".join(PLACEHOLDER_PATTERNS), re.IGNORECASE)


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def parse_file(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    header = {}
    body_lines = []
    in_body = False
    for line in text.splitlines():
        if not in_body:
            if line.startswith("TITRE:"):   header["titre"] = line[6:].strip()
            elif line.startswith("ESPACE:"): header["espace"] = line[7:].strip()
            elif line.startswith("URL:"):    header["url"] = line[4:].strip()
            elif "=" * 20 in line:           in_body = True
        else:
            body_lines.append(line)
    return header, "\n".join(body_lines).strip()


def real_word_count(text: str) -> int:
    return sum(1 for w in text.split() if len(w) > 2 and not w.isdigit())


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def subsection(title: str):
    print(f"\n  ── {title}")
    print(f"  {'─'*50}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleaned", default=str(DEFAULT_CLEANED))
    args = parser.parse_args()

    cleaned_root = Path(args.cleaned)

    if not cleaned_root.exists():
        print(f"[ERREUR] Dossier introuvable : {cleaned_root}")
        return

    spaces = sorted([d for d in cleaned_root.iterdir() if d.is_dir()])
    if not spaces:
        print(f"[ERREUR] Aucun space trouvé dans {cleaned_root}")
        return

    # ── Collecte de toutes les pages ──
    all_pages = []
    for space_dir in spaces:
        for f in space_dir.glob("*.txt"):
            header, body = parse_file(f)
            all_pages.append({
                "path": f,
                "space": space_dir.name,
                "titre": header.get("titre", f.stem),
                "body": body,
                "word_count": real_word_count(body),
                "body_len": len(body),
            })

    section("📊 STATS GLOBALES")
    print(f"\n  Total pages nettoyées : {len(all_pages)}")
    print(f"  Nombre de spaces      : {len(spaces)}")

    # Par space
    subsection("Pages par space")
    by_space = defaultdict(list)
    for p in all_pages:
        by_space[p["space"]].append(p)
    for sp, pages in sorted(by_space.items()):
        avg_words = sum(p["word_count"] for p in pages) // len(pages)
        print(f"    {sp:<12} : {len(pages):>5} pages  |  moy. {avg_words:>4} mots/page")

    # ── Distribution longueur ──
    section("📏 DISTRIBUTION DE LONGUEUR (mots réels)")
    buckets = {
        "50–100  mots  (très court)": (50, 100),
        "100–300 mots  (court)     ": (100, 300),
        "300–700 mots  (moyen)     ": (300, 700),
        "700–2000 mots (long)      ": (700, 2000),
        "> 2000  mots  (très long) ": (2000, 99999),
    }
    for label, (lo, hi) in buckets.items():
        count = sum(1 for p in all_pages if lo <= p["word_count"] < hi)
        bar = "█" * (count * 40 // len(all_pages))
        print(f"  {label} : {count:>5}  {bar}")

    # ── Vérification des anomalies restantes ──
    section("🔍 VÉRIFICATION DES ANOMALIES RESTANTES")

    # 1. Pages vides
    empty = [p for p in all_pages if p["body_len"] < 10]
    print(f"\n  ❌ Pages vides (body < 10 chars)     : {len(empty)}")
    for p in empty[:3]:
        print(f"      → {p['space']}/{p['path'].name}")

    # 2. Placeholders
    placeholders = [p for p in all_pages if PLACEHOLDER_RE.search(p["body"])]
    print(f"  ❌ Pages placeholder encore présentes : {len(placeholders)}")
    for p in placeholders[:3]:
        print(f"      → {p['space']}/{p['path'].name}")

    # 3. Pages très courtes (50–60 mots) — à vérifier manuellement
    borderline = [p for p in all_pages if 50 <= p["word_count"] <= 60]
    print(f"\n  ⚠️  Pages borderline (50–60 mots)     : {len(borderline)}")
    print(f"     (Ces pages ont passé le filtre mais méritent un œil)")

    # 4. Doublons résiduels (même body exact dans même space)
    from collections import Counter
    import hashlib
    hash_space = [(p["space"], hashlib.md5(re.sub(r'\s+', ' ', p["body"]).strip().lower().encode()).hexdigest())
                  for p in all_pages]
    dup_counts = Counter(hash_space)
    residual_dups = {k: v for k, v in dup_counts.items() if v > 1}
    print(f"  ❌ Doublons résiduels intra-space     : {len(residual_dups)}")

    # ── Échantillon pages courtes ──
    section("🔎 ÉCHANTILLON — PAGES COURTES (50–80 mots) À VÉRIFIER")
    short_sample = sorted([p for p in all_pages if 50 <= p["word_count"] <= 80],
                          key=lambda x: x["word_count"])[:10]
    for p in short_sample:
        print(f"\n  [{p['space']}] {p['titre']} ({p['word_count']} mots)")
        print(f"  {p['body'][:180].strip()}")
        print(f"  ...")

    # ── Bilan final ──
    section("✅ BILAN")
    issues = len(empty) + len(placeholders) + len(residual_dups)
    if issues == 0:
        print("\n  🎉 Aucune anomalie détectée — le nettoyage est propre !")
    else:
        print(f"\n  ⚠️  {issues} anomalie(s) détectée(s) — voir détails ci-dessus")

    avg_words = sum(p["word_count"] for p in all_pages) // len(all_pages)
    print(f"\n  Moyenne mots/page  : {avg_words}")
    print(f"  Pages > 300 mots   : {sum(1 for p in all_pages if p['word_count'] >= 300)} "
          f"({sum(1 for p in all_pages if p['word_count'] >= 300)*100//len(all_pages)}%)")
    print(f"\n  📁 Données vérifiées : {cleaned_root}")
    print("=" * 60)


if __name__ == "__main__":
    main()