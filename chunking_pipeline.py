"""
Pipeline Chunking Adaptatif — Divalto RAG
==========================================
Stratégie : Adaptive (Recursive + Fixed pour pages courtes)
Output    : JSON par espace + JSON global

Usage :
    python chunking_pipeline.py

Paramètres configurables dans la section CONFIG ci-dessous.
"""

import os, re, json, glob, time
from pathlib import Path
from collections import defaultdict

# ─── CONFIG ───────────────────────────────────────────────────────────────────

DATA_DIR      = r"C:\Users\Ameni Mejri\Documents\chatbot-divalto - Copie\divalto_data\cleaned_pages"
OUTPUT_DIR    = r"C:\Users\Ameni Mejri\Documents\chatbot-divalto\chunks_output"

# Paramètres Fixed-Size (pages courtes)
FIXED_CHUNK_WORDS   = 60
FIXED_OVERLAP_WORDS = 10

# Paramètres Recursive
RECURSIVE_CHUNK_WORDS   = 150
RECURSIVE_OVERLAP_WORDS = 30

# Seuil pages courtes → Fixed
SHORT_PAGE_THRESHOLD = 80   # mots

# Post-filtrage
MIN_CHUNK_WORDS = 15        # supprimer chunks < 15 mots
MIN_ALPHA_RATIO = 0.35      # supprimer chunks trop bruités

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def extract_content(filepath):
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()
    lines = raw.replace('\r\n', '\n').split("\n")
    meta = {}
    content_start = 0
    for i, line in enumerate(lines):
        if line.startswith("TITRE:"): meta["titre"] = line[6:].strip()
        elif line.startswith("ESPACE:"): meta["espace"] = line[7:].strip()
        elif line.startswith("URL:"): meta["url"] = line[4:].strip()
        elif line.startswith("MAJ:"): meta["maj"] = line[4:].strip()
        elif "=" * 20 in line:
            content_start = i + 1
            break
    content = "\n".join(lines[content_start:]).strip()
    meta["content"] = content
    meta["word_count"] = len(content.split())
    return meta

# ─── MÉTHODE 1 : FIXED-SIZE ───────────────────────────────────────────────────

def fixed_size_chunk(text, chunk_words=FIXED_CHUNK_WORDS, overlap=FIXED_OVERLAP_WORDS):
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = words[i:i + chunk_words]
        chunks.append(" ".join(chunk))
        i += chunk_words - overlap
    return chunks

# ─── MÉTHODE 2 : RECURSIVE ────────────────────────────────────────────────────

def recursive_chunk(text, chunk_words=RECURSIVE_CHUNK_WORDS, overlap=RECURSIVE_OVERLAP_WORDS):
    separators = ["\n\n", "\n", ". ", " "]

    def split_text(text, seps):
        if not seps:
            return [text]
        sep = seps[0]
        splits = [s for s in text.split(sep) if s.strip()]
        chunks = []
        current = ""
        for s in splits:
            candidate = (current + sep + s).strip() if current else s.strip()
            if len(candidate.split()) <= chunk_words:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                if len(s.split()) > chunk_words:
                    chunks.extend(split_text(s, seps[1:]))
                    current = ""
                else:
                    current = s.strip()
        if current:
            chunks.append(current)
        return chunks

    raw_chunks = split_text(text, separators)

    # Ajouter overlap
    final = []
    for idx, chunk in enumerate(raw_chunks):
        if idx > 0 and overlap > 0:
            prev_words = final[-1].split()[-overlap:]
            chunk = " ".join(prev_words) + " " + chunk
        final.append(chunk.strip())

    return [c for c in final if c.strip()]

# ─── POST-FILTRAGE ─────────────────────────────────────────────────────────────

def is_noise(text):
    text = text.strip()
    words = text.split()

    # Trop court
    if len(words) < MIN_CHUNK_WORDS:
        return True, "trop_court"

    # UUID Confluence
    if re.search(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}', text):
        return True, "uuid"

    # URL / iframe
    if re.search(r'https?://|atlassian|widgetconnector|\.vm\b', text):
        return True, "url"

    # Dimensions HTML
    if re.match(r'^\d+px', text):
        return True, "html_dim"

    # Codes couleur Confluence
    if re.search(r'#[A-F0-9]{6}', text) and len(words) < 20:
        return True, "color_code"

    # Tags statut Confluence
    if re.search(r'\b(DECIDED|TODO|incomplete|a faire)\b', text) and len(words) < 15:
        return True, "status_tag"

    # Ratio alpha trop bas (bruit technique)
    alpha = [w for w in words if re.match(r'^[a-zA-ZÀ-ÿ]+$', w)]
    if len(words) > 5 and len(alpha) / len(words) < MIN_ALPHA_RATIO:
        return True, "low_alpha"

    return False, None

# ─── ADAPTIVE ROUTER ──────────────────────────────────────────────────────────

def adaptive_chunk(content, word_count):
    if word_count < SHORT_PAGE_THRESHOLD:
        raw = fixed_size_chunk(content)
        method = "fixed"
    else:
        raw = recursive_chunk(content)
        method = "recursive"

    # Post-filtrage
    clean_chunks = []
    filtered = 0
    for chunk in raw:
        noisy, reason = is_noise(chunk)
        if noisy:
            filtered += 1
        else:
            clean_chunks.append(chunk)

    return clean_chunks, method, filtered

# ─── PIPELINE PRINCIPAL ───────────────────────────────────────────────────────

def run_pipeline():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    per_space_dir = os.path.join(OUTPUT_DIR, "by_space")
    os.makedirs(per_space_dir, exist_ok=True)

    all_chunks  = []
    global_stats = {
        "total_pages": 0,
        "total_chunks": 0,
        "filtered_chunks": 0,
        "method_counts": defaultdict(int),
        "space_stats": defaultdict(lambda: {"pages": 0, "chunks": 0, "method_fixed": 0, "method_recursive": 0})
    }

    spaces = sorted([d for d in os.listdir(DATA_DIR)
                     if os.path.isdir(os.path.join(DATA_DIR, d))])

    print(f"📂 {len(spaces)} espaces trouvés")
    print(f"🚀 Démarrage du pipeline...\n")

    start_total = time.time()

    for space in spaces:
        space_chunks = []
        files = glob.glob(os.path.join(DATA_DIR, space, "*.txt"))

        space_start = time.time()

        for fp in files:
            page = extract_content(fp)
            content = page.get("content", "").strip()
            if not content:
                continue

            chunks, method, filtered = adaptive_chunk(content, page["word_count"])

            global_stats["total_pages"] += 1
            global_stats["total_chunks"] += len(chunks)
            global_stats["filtered_chunks"] += filtered
            global_stats["method_counts"][method] += 1
            global_stats["space_stats"][space]["pages"] += 1
            global_stats["space_stats"][space]["chunks"] += len(chunks)
            global_stats["space_stats"][space][f"method_{method}"] += 1

            for idx, chunk_text in enumerate(chunks):
                chunk_obj = {
                    "id": f"{space}_{os.path.basename(fp).replace('.txt','')}_{idx}",
                    "espace": space,
                    "titre": page.get("titre", ""),
                    "url": page.get("url", ""),
                    "maj": page.get("maj", ""),
                    "chunk_index": idx,
                    "chunk_total": len(chunks),
                    "method": method,
                    "text": chunk_text,
                    "word_count": len(chunk_text.split()),
                    "token_approx": int(len(chunk_text.split()) * 1.3)
                }
                space_chunks.append(chunk_obj)
                all_chunks.append(chunk_obj)

        elapsed = time.time() - space_start
        print(f"  ✓ {space:<12} {len(files):>5} pages → {len(space_chunks):>6} chunks ({elapsed:.1f}s)")

        # Sauvegarder JSON par espace
        space_output = os.path.join(per_space_dir, f"{space}_chunks.json")
        with open(space_output, "w", encoding="utf-8") as f:
            json.dump(space_chunks, f, ensure_ascii=False, indent=2)

    # Sauvegarder JSON global
    global_output = os.path.join(OUTPUT_DIR, "all_chunks.json")
    with open(global_output, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)

    # Stats finales
    elapsed_total = time.time() - start_total
    total_chunks = global_stats["total_chunks"]
    token_sizes = [c["token_approx"] for c in all_chunks]
    token_sizes.sort()
    n = len(token_sizes)

    print(f"\n{'='*55}")
    print(f"✅ Pipeline terminé en {elapsed_total:.1f}s")
    print(f"\n── Stats globales ─────────────────────────────────────")
    print(f"  Pages traitées     : {global_stats['total_pages']:>6}")
    print(f"  Chunks produits    : {total_chunks:>6}")
    print(f"  Chunks filtrés     : {global_stats['filtered_chunks']:>6}")
    print(f"  Moy. tokens/chunk  : {sum(token_sizes)//n:>6}")
    print(f"  Median tokens      : {token_sizes[n//2]:>6}")
    print(f"  % dans plage 50-512: {sum(1 for t in token_sizes if 50<=t<=512)/n*100:>5.1f}%")
    print(f"\n── Méthodes utilisées ─────────────────────────────────")
    for m, c in global_stats["method_counts"].items():
        print(f"  {m:<12} {c:>6} pages ({c/global_stats['total_pages']*100:.1f}%)")
    print(f"\n── Output ─────────────────────────────────────────────")
    print(f"  Global  : {global_output}")
    print(f"  By space: {per_space_dir}/")

    # Sauvegarder stats
    stats_output = os.path.join(OUTPUT_DIR, "pipeline_stats.json")
    stats_to_save = {
        "total_pages": global_stats["total_pages"],
        "total_chunks": total_chunks,
        "filtered_chunks": global_stats["filtered_chunks"],
        "avg_tokens": sum(token_sizes)//n,
        "median_tokens": token_sizes[n//2],
        "pct_in_range": round(sum(1 for t in token_sizes if 50<=t<=512)/n*100, 1),
        "method_counts": dict(global_stats["method_counts"]),
        "space_stats": {k: dict(v) for k, v in global_stats["space_stats"].items()}
    }
    with open(stats_output, "w", encoding="utf-8") as f:
        json.dump(stats_to_save, f, ensure_ascii=False, indent=2)
    print(f"  Stats   : {stats_output}")

if __name__ == "__main__":
    run_pipeline()