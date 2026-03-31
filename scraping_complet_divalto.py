"""
============================================================
  SCRAPING RAPIDE - Portail Divalto (Confluence API)
  ⚡ ThreadPoolExecutor → pages en parallèle
  ⚡ Session HTTP réutilisée → connexions persistantes
  ⚡ Retry automatique avec backoff
  ✅ Tous les espaces + toutes les pages
  ✅ Texte nettoyé (.txt) + pièces jointes
  ✅ Index CSV global
============================================================

Prérequis:
    pip install requests python-dotenv tqdm beautifulsoup4 pandas lxml

.env:
    CONFLUENCE_EMAIL=...
    CONFLUENCE_API_TOKEN=...
    CONFLUENCE_BASE_URL=https://divalto.atlassian.net
"""

import os
import re
import json
import time
import threading
import requests
import pandas as pd
from tqdm import tqdm
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

EMAIL    = os.getenv("CONFLUENCE_EMAIL")     or os.getenv("ATLASSIAN_EMAIL")
TOKEN    = os.getenv("CONFLUENCE_API_TOKEN") or os.getenv("ATLASSIAN_TOKEN")
BASE_URL = os.getenv("CONFLUENCE_BASE_URL")  or os.getenv("ATLASSIAN_BASE_URL", "https://divalto.atlassian.net")

AUTH     = HTTPBasicAuth(EMAIL, TOKEN)
HEADERS  = {"Accept": "application/json"}
API_BASE = f"{BASE_URL}/wiki/rest/api"

# ⚡ Nombre de threads parallèles
# 5 = bon équilibre vitesse/stabilité
# Monte à 10 si tu veux encore plus vite (risque rate limit)
MAX_WORKERS = 5

DELAY = 0.1  # réduit vs 0.3 car on gère le retry automatiquement

# ─── DOSSIERS ─────────────────────────────────────────────────────────────────

BASE_DIR  = "divalto_data"
PAGES_DIR = f"{BASE_DIR}/pages"
ATT_DIR   = f"{BASE_DIR}/attachments"
RAW_DIR   = f"{BASE_DIR}/raw_json"

for d in [PAGES_DIR, ATT_DIR, RAW_DIR]:
    os.makedirs(d, exist_ok=True)

# Thread-safe index CSV
index_rows = []
index_lock = threading.Lock()  # évite les conflits d'écriture entre threads

# ─── SESSION HTTP OPTIMISÉE ───────────────────────────────────────────────────

def make_session() -> requests.Session:
    """
    ⚡ Session HTTP réutilisable avec retry automatique.
    Au lieu d'ouvrir une nouvelle connexion à chaque requête,
    on réutilise la même → beaucoup plus rapide.
    """
    session = requests.Session()
    session.auth = AUTH
    session.headers.update(HEADERS)

    # Retry automatique sur erreurs réseau et rate limit
    retry = Retry(
        total=5,                # max 5 tentatives
        backoff_factor=0.5,     # attente: 0.5, 1, 2, 4, 8 secondes
        status_forcelist=[429, 500, 502, 503, 504],  # codes à retry
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_WORKERS,   # connexions parallèles
        pool_maxsize=MAX_WORKERS * 2
    )
    session.mount("https://", adapter)
    return session

# Session globale partagée (thread-safe pour GET)
SESSION = make_session()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def clean_html(html: str) -> str:
    """Convertit le HTML Confluence en texte propre pour le RAG."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


def safe_filename(name: str, max_len: int = 60) -> str:
    """Nom de fichier sécurisé pour Windows/Linux."""
    name = name.strip().replace("\n", " ")
    name = re.sub(r'[<>:"/\\|?*]+', "_", name)
    return name[:max_len]


def api_get(endpoint: str, params: dict = None):
    """Appel API avec session optimisée et retry automatique."""
    url = f"{API_BASE}/{endpoint}"
    try:
        r = SESSION.get(url, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 401:
            print("❌ Authentification refusée — vérifie ton token!")
            return None
        elif r.status_code == 403:
            return None  # accès refusé silencieux
        else:
            return None
    except Exception as e:
        print(f"❌ Exception : {e}")
        return None


# ─── 1. TOUS LES ESPACES ──────────────────────────────────────────────────────

def get_all_spaces() -> list:
    """Récupère tous les espaces Confluence disponibles."""
    print("\n" + "="*60)
    print("  ÉTAPE 1 : Récupération de tous les espaces")
    print("="*60)

    all_spaces, start, limit = [], 0, 50

    while True:
        data = api_get("space", params={
            "limit": limit, "start": start,
            "expand": "description.plain"
        })
        if not data:
            break

        results = data.get("results", [])
        all_spaces.extend(results)
        print(f"  📦 {len(all_spaces)} espaces récupérés...")

        if data.get("_links", {}).get("next"):
            start += limit
        else:
            break

    print(f"\n  ✅ Total : {len(all_spaces)} espaces\n")
    for s in all_spaces:
        print(f"  📂 [{s.get('key')}] {s.get('name')}")

    with open(f"{RAW_DIR}/espaces.json", "w", encoding="utf-8") as f:
        json.dump(all_spaces, f, ensure_ascii=False, indent=2)

    return all_spaces


# ─── 2. TOUTES LES PAGES D'UN ESPACE ─────────────────────────────────────────

def get_all_pages(space_key: str) -> list:
    """Récupère toutes les pages d'un espace avec pagination."""
    all_pages, start, limit = [], 0, 50

    while True:
        data = api_get("content", params={
            "spaceKey": space_key,
            "type":     "page",
            "expand":   "body.storage,version,ancestors",
            "limit":    limit,
            "start":    start
        })
        if not data:
            break

        results = data.get("results", [])
        all_pages.extend(results)

        if data.get("_links", {}).get("next"):
            start += limit
        else:
            break

    return all_pages


# ─── 3. PIÈCES JOINTES ────────────────────────────────────────────────────────

def get_attachments(page_id: str) -> list:
    """Récupère toutes les pièces jointes d'une page."""
    results = []
    url = f"{API_BASE}/content/{page_id}/child/attachment?limit=50"

    while url:
        try:
            r = SESSION.get(url, timeout=60)
            if r.status_code != 200:
                break
            data = r.json()
            results.extend(data.get("results", []))
            next_link = data.get("_links", {}).get("next")
            url = f"{BASE_URL}/wiki{next_link}" if next_link else None
        except Exception:
            break

    return results


def download_attachment(att: dict, page_id: str, space_key: str):
    """Télécharge une pièce jointe."""
    if "_links" not in att or "download" not in att["_links"]:
        return None

    att_url    = f"{BASE_URL}/wiki{att['_links']['download']}"
    filename   = safe_filename(att.get("title", "attachment.bin"))
    media_type = att.get("metadata", {}).get("mediaType", "")

    page_dir = f"{ATT_DIR}/{space_key}/{page_id}"
    os.makedirs(page_dir, exist_ok=True)
    filepath = f"{page_dir}/{filename}"

    try:
        r = SESSION.get(att_url, timeout=120, stream=True)
        if r.status_code == 200:
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return {
                "filename":   filename,
                "filepath":   filepath,
                "media_type": media_type,
                "size":       os.path.getsize(filepath)
            }
    except Exception as e:
        print(f"      ⚠️  Erreur : {e}")

    return None


# ─── 4. TRAITER UNE PAGE (exécuté en parallèle) ───────────────────────────────

def process_page(page: dict, space_key: str) -> dict:
    """
    ⚡ Cette fonction est appelée en parallèle par ThreadPoolExecutor.
    Chaque thread traite une page indépendamment.
    """
    page_id      = page.get("id", "")
    page_title   = page.get("title", "")
    html         = page.get("body", {}).get("storage", {}).get("value", "")
    web_url      = f"{BASE_URL}/wiki{page.get('_links', {}).get('webui', '')}"
    last_updated = page.get("version", {}).get("when", "")

    clean_text = clean_html(html)

    space_dir = f"{PAGES_DIR}/{space_key}"
    os.makedirs(space_dir, exist_ok=True)
    safe_title = safe_filename(page_title)

    # Sauvegarde .txt
    txt_path = f"{space_dir}/{page_id}_{safe_title}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"TITRE: {page_title}\n")
        f.write(f"URL: {web_url}\n")
        f.write(f"ESPACE: {space_key}\n")
        f.write(f"MAJ: {last_updated}\n")
        f.write("="*60 + "\n\n")
        f.write(clean_text)

    # Pièces jointes
    attachments    = get_attachments(page_id)
    att_downloaded = 0
    att_infos      = []

    for att in attachments:
        att_info = download_attachment(att, page_id, space_key)
        if att_info:
            att_infos.append(att_info)
            att_downloaded += 1

    # ⚡ index_lock → évite que 2 threads écrivent en même temps
    with index_lock:
        index_rows.append({
            "page_id":                page_id,
            "title":                  page_title,
            "space":                  space_key,
            "url":                    web_url,
            "text_size":              len(clean_text),
            "attachments_total":      len(attachments),
            "attachments_downloaded": att_downloaded,
            "last_updated":           last_updated,
            "txt_file":               txt_path,
        })

    return {
        "id":           page_id,
        "title":        page_title,
        "url":          web_url,
        "space":        space_key,
        "last_updated": last_updated,
        "attachments":  att_infos
    }


# ─── 5. TRAITER UN ESPACE EN PARALLÈLE ───────────────────────────────────────

def process_space(space: dict) -> dict:
    """
    ⚡ Traite toutes les pages d'un espace en parallèle
    avec ThreadPoolExecutor.
    """
    space_key  = space.get("key", "")
    space_name = space.get("name", "")

    print(f"\n{'='*60}")
    print(f"  📂 [{space_key}] {space_name}")
    print(f"{'='*60}")

    pages = get_all_pages(space_key)
    print(f"  ✅ {len(pages)} page(s) — traitement avec {MAX_WORKERS} threads...")

    space_data = {
        "space_key":   space_key,
        "space_name":  space_name,
        "total_pages": len(pages),
        "pages":       []
    }

    # ⚡ ThreadPoolExecutor → MAX_WORKERS pages en même temps
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

        # Soumettre toutes les pages
        futures = {
            executor.submit(process_page, page, space_key): page
            for page in pages
        }

        # Récupérer les résultats avec barre de progression
        with tqdm(total=len(pages), desc=f"  ⚡ [{space_key}]", unit="page") as pbar:
            for future in as_completed(futures):
                try:
                    page_info = future.result()
                    space_data["pages"].append({
                        "id":                page_info["id"],
                        "title":             page_info["title"],
                        "url":               page_info["url"],
                        "attachments_count": len(page_info["attachments"])
                    })
                except Exception as e:
                    print(f"\n  ⚠️  Erreur page : {e}")
                finally:
                    pbar.update(1)

    with open(f"{RAW_DIR}/space_{space_key}.json", "w", encoding="utf-8") as f:
        json.dump(space_data, f, ensure_ascii=False, indent=2)

    print(f"  💾 [{space_key}] terminé — {len(space_data['pages'])} pages")
    return space_data


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if not EMAIL or not TOKEN:
        raise ValueError("❌ EMAIL/TOKEN manquants dans le .env !")

    start_time = time.time()

    print("\n" + "="*60)
    print("  ⚡ SCRAPING RAPIDE - Portail Divalto")
    print("="*60)
    print(f"  🔧 Threads parallèles : {MAX_WORKERS}")
    print(f"  📁 Output : {os.path.abspath(BASE_DIR)}")

    # Étape 1 : tous les espaces
    spaces = get_all_spaces()
    if not spaces:
        print("❌ Aucun espace trouvé!")
        return

    # Étape 2 : traiter chaque espace
    print(f"\n\n{'='*60}")
    print(f"  ÉTAPE 2 : Scraping de {len(spaces)} espaces")
    print(f"{'='*60}")

    all_results       = []
    total_pages       = 0
    total_attachments = 0

    for space in spaces:
        result = process_space(space)
        all_results.append(result)
        total_pages       += result["total_pages"]
        total_attachments += sum(p["attachments_count"] for p in result["pages"])

    # Summary
    summary = {
        "total_spaces":      len(spaces),
        "total_pages":       total_pages,
        "total_attachments": total_attachments,
        "spaces":            all_results
    }
    with open(f"{RAW_DIR}/summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # CSV
    if index_rows:
        pd.DataFrame(index_rows).to_csv(
            f"{BASE_DIR}/index.csv", index=False, encoding="utf-8"
        )

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    print("\n\n" + "="*60)
    print("  ✅ SCRAPING TERMINÉ !")
    print("="*60)
    print(f"  ⏱️  Temps total      : {minutes}m {seconds}s")
    print(f"  📂 Espaces          : {len(spaces)}")
    print(f"  📄 Pages            : {total_pages}")
    print(f"  📎 Pièces jointes   : {total_attachments}")
    print(f"  ⚡ Threads utilisés : {MAX_WORKERS}")
    print(f"\n  Structure finale :")
    print(f"  {BASE_DIR}/")
    print(f"  ├── index.csv")
    print(f"  ├── pages/        ← .txt par espace")
    print(f"  ├── attachments/  ← fichiers téléchargés")
    print(f"  └── raw_json/     ← summary + espaces")
    print("\n  🚀 Prochaine étape : chunking + embeddings RAG !")
    print("="*60)


if __name__ == "__main__":
    main()