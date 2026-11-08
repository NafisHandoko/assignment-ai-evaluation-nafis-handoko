"""
Modul RAG: mengubah knowledge base menjadi embedding, menyimpannya ke FAISS,
melakukan semantic search, lalu menghasilkan jawaban menggunakan LLM.

Alur ini serupa dengan yang sudah dibangun di Assignment 3 (Semantic Search &
LLM Application) dan Sesi 11 (Building a Simple RAG System). Di assignment ini,
alur tersebut dibungkus menjadi fungsi yang dipanggil oleh router.py.

Arsitektur dual-provider:
  - Jalur A (default)  : Gemini API langsung (gemini-embedding-001, 768 dimensi)
                         dan LLM gemini-3.6-flash.
  - Jalur B (fallback) : OpenRouter (google/gemini-embedding-001, 3072 dimensi)
                         dan google/gemini-3.6-flash, dipakai otomatis ketika
                         Gemini langsung tidak tersedia (mis. pembatasan region).

Satu jalur dipakai konsisten dari awal sampai akhir supaya ruang vektor
embedding tidak tercampur antar model. Konfigurasi lewat environment variables;
tanpa hardcode API key.
"""

import json
import os
import urllib.request
from pathlib import Path

import faiss
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# Baca .env dari root project (atau dari environment yang sudah di-set).
# load_dotenv tidak menimpa nilai yang sudah ada di environment.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

KNOWLEDGE_BASE_PATH = Path(__file__).resolve().parent.parent / "data" / "it_helpdesk_knowledge_base.csv"

# Model
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
LLM_MODEL = "gemini-3.6-flash"
LLM_MODEL_OR = "google/gemini-3.6-flash"
BATCH_SIZE = 20

# Ambang relevansi retrieval.
# Berdasarkan hasil evaluasi: query yang benar-benar terjawab KB mendapat
# similarity >= 0.78, sedangkan sapaan dan pertanyaan di luar cakupan
# berada di 0.48-0.65. Ambang 0.70 memisahkan keduanya dengan margin aman.
MIN_SIMILARITY = 0.70

# Jatah token output LLM.
# Gemini 3.x memakai sebagian jatah untuk reasoning internal sebelum menjawab.
# Dengan 600 token, jawaban terpotong (EVAL-11 hanya 102 karakter dari 596
# completion token). Dinaikkan agar reasoning dan jawaban sama-sama kebagian.
MAX_OUTPUT_TOKENS = 1500

# Sapaan/obrolan singkat yang tidak perlu retrieval sama sekali.
_GREETING_WORDS = {
    "halo", "hai", "hi", "hello", "pagi", "siang", "sore", "malam",
    "selamat pagi", "selamat siang", "selamat sore", "selamat malam",
    "assalamualaikum", "terima kasih", "makasih", "thanks", "ok", "oke",
}

# Deteksi jalur: coba Gemini langsung saat import pertama kali.
_USE_GEMINI = False
_client = None

if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types

        _client = genai.Client(api_key=GEMINI_API_KEY)
        # Probe murah: embedding satu token. Gagal -> region diblokir -> fallback.
        _client.models.embed_content(
            model=EMBED_MODEL,
            contents=["uji"],
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=EMBED_DIM,
            ),
        )
        _USE_GEMINI = True
    except Exception:
        _client = None
        _USE_GEMINI = False

if not _USE_GEMINI and not OPENROUTER_API_KEY:
    raise RuntimeError(
        "Tidak ada API key tersedia. Set GEMINI_API_KEY (atau OPENROUTER_API_KEY "
        "sebagai fallback) di file .env atau environment variable."
    )

# STATE aplikasi (diisi saat build_index dipanggil sekali di startup).
_index = None
_documents = None
_df = None


def _normalize(v: np.ndarray) -> np.ndarray:
    """Normalisasi L2 per baris agar inner product = cosine similarity."""
    panjang = np.linalg.norm(v, axis=1, keepdims=True)
    panjang[panjang == 0] = 1e-12
    return v / panjang


def _embed_texts(texts: list[str]) -> np.ndarray:
    """
    Mengubah daftar teks menjadi vektor embedding (float32, sudah dinormalisasi).

    Jalur A: Gemini API langsung, task_type RETRIEVAL_DOCUMENT, 768 dimensi.
    Jalur B: OpenRouter embeddings, model yang sama, 3072 dimensi (default penuh).
    """
    if _USE_GEMINI:
        from google.genai import types

        vecs = []
        for start in range(0, len(texts), BATCH_SIZE):
            chunk = texts[start:start + BATCH_SIZE]
            result = _client.models.embed_content(
                model=EMBED_MODEL,
                contents=chunk,
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_DOCUMENT",
                    output_dimensionality=EMBED_DIM,
                ),
            )
            vecs.extend(e.values for e in result.embeddings)
        v = np.array(vecs, dtype="float32")
    else:
        payload = {"model": "google/gemini-embedding-001", "input": texts}
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/embeddings",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        d = json.loads(urllib.request.urlopen(req, timeout=120).read())
        data = sorted(d["data"], key=lambda x: x["index"])
        v = np.array([item["embedding"] for item in data], dtype="float32")

    return _normalize(v)


def _embed_query(query: str) -> np.ndarray:
    """Embedding satu query, konsisten dengan jalur embedding dokumen."""
    if _USE_GEMINI:
        from google.genai import types

        result = _client.models.embed_content(
            model=EMBED_MODEL,
            contents=[query],
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=EMBED_DIM,
            ),
        )
        v = np.array([e.values for e in result.embeddings], dtype="float32")
    else:
        payload = {"model": "google/gemini-embedding-001", "input": [query]}
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/embeddings",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
        v = np.array([d["data"][0]["embedding"]], dtype="float32")

    return _normalize(v)


def build_index() -> None:
    """
    Membangun index FAISS dari knowledge base CSV.

    Dipanggil SATU KALI saat aplikasi startup (bukan per request) supaya
    embedding tidak dihitung ulang setiap kali ada pertanyaan masuk.
    """
    global _index, _documents, _df

    _df = pd.read_csv(KNOWLEDGE_BASE_PATH)
    # Unit retrievable: pertanyaan + jawaban digabung agar konteks utuh.
    _documents = ("Q: " + _df["question"] + " A: " + _df["answer"]).tolist()

    embeddings = _embed_texts(_documents)
    dimension = embeddings.shape[1]
    _index = faiss.IndexFlatIP(dimension)
    _index.add(embeddings)


def semantic_search(query: str, top_k: int = 3) -> list[dict]:
    """
    Mencari entri knowledge base paling relevan terhadap query.

    Mengembalikan daftar dict: {rank, faq_id, category, question, answer,
    similarity}. Skor similarity adalah cosine similarity (0-1).
    """
    if _index is None:
        build_index()

    q = _embed_query(query)
    scores, indices = _index.search(q, top_k)

    results = []
    for rank, (score, idx) in enumerate(zip(scores[0], indices[0]), 1):
        if idx == -1:
            continue
        results.append({
            "rank": rank,
            "faq_id": int(_df.iloc[idx]["faq_id"]),
            "category": _df.iloc[idx]["category"],
            "question": _df.iloc[idx]["question"],
            "answer": _df.iloc[idx]["answer"],
            "similarity": float(score),
        })
    return results


def build_prompt(query: str, search_results: list[dict]) -> str:
    """
    Menyusun prompt terstruktur (Persona/Task/Context/Format) sesuai
    pola yang dipelajari di Assignment 3 dan Sesi 10.
    """
    context = "\n\n".join(
        f"FAQ: {r['question']}\nJawaban: {r['answer']}" for r in search_results
    )
    return f"""Kamu adalah asisten IT Helpdesk perusahaan yang ramah dan membantu.

Tugas: Jawab pertanyaan karyawan HANYA berdasarkan informasi dari knowledge base di bawah.
Jika informasi tidak ditemukan di knowledge base, katakan bahwa kamu belum memiliki
informasi tersebut dan sarankan karyawan menghubungi tim IT Support.

Konteks knowledge base:
{context}

Pertanyaan karyawan: {query}

Format jawaban: jawab dalam Bahasa Indonesia yang singkat, jelas, dan natural.
Jangan menambahkan informasi di luar konteks di atas."""


def _call_llm(prompt: str, max_tokens: int = None) -> str:
    """Memanggil LLM (Gemini langsung atau fallback OpenRouter)."""
    if max_tokens is None:
        max_tokens = MAX_OUTPUT_TOKENS

    if _USE_GEMINI:
        from google.genai import types

        response = _client.models.generate_content(
            model=LLM_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=max_tokens,
            ),
        )
        return (response.text or "").strip()

    payload = {
        "model": LLM_MODEL_OR,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    d = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return (d["choices"][0]["message"].get("content") or "").strip()


def is_greeting(query: str) -> bool:
    """
    Mendeteksi sapaan/obrolan singkat yang tidak membutuhkan retrieval.

    Hasil evaluasi EVAL-12: query "halo" tetap dijalankan lewat RAG penuh,
    memakai 221 prompt token berisi FAQ yang tidak relevan (similarity 0.483)
    dan LLM tetap menjawab dengan menyebut topik yang tidak diminta.
    """
    normalized = query.strip().lower().strip("!?.,")
    if not normalized:
        return True
    # Sapaan persis, atau kalimat pendek (<= 4 kata) yang seluruhnya sapaan.
    if normalized in _GREETING_WORDS:
        return True
    words = normalized.split()
    return len(words) <= 4 and all(w.strip("!?.,") in _GREETING_WORDS for w in words)


def _filter_relevant(search_results: list[dict]) -> list[dict]:
    """
    Membuang hasil retrieval yang similarity-nya di bawah ambang relevansi.

    Tanpa filter ini, pertanyaan di luar cakupan tetap menyisipkan FAQ yang
    tidak relevan ke dalam prompt sehingga menambah token dan mengundang
    jawaban melebar ke topik yang tidak ditanyakan.
    """
    return [r for r in search_results if r["similarity"] >= MIN_SIMILARITY]


async def answer_from_knowledge_base(query: str, top_k: int = 3) -> str:
    """
    Alur lengkap RAG yang dipanggil router ketika intent = 'faq':
    semantic search -> filter relevansi -> susun prompt ber-context -> panggil LLM.

    Dua optimasi hasil evaluasi:
      1. Sapaan singkat dijawab langsung tanpa retrieval dan tanpa LLM
         (menghemat seluruh biaya embedding + token LLM).
      2. Hasil retrieval di bawah MIN_SIMILARITY dibuang supaya konteks yang
         masuk ke prompt benar-benar relevan.
    """
    if is_greeting(query):
        return (
            "Halo! Saya asisten IT Helpdesk. Saya bisa membantu pertanyaan seputar "
            "kebijakan HR dan IT perusahaan, atau mengecek status tiket IT Anda. "
            "Ada yang bisa saya bantu?"
        )

    search_results = semantic_search(query, top_k=top_k)
    search_results = _filter_relevant(search_results)

    if not search_results:
        # Tidak ada konteks yang cukup relevan: jawab tanpa memanggil LLM.
        # Ini menghindari biaya token sekaligus mencegah model mengarang.
        return (
            "Maaf, saya belum memiliki informasi mengenai hal tersebut di knowledge base. "
            "Silakan hubungi tim IT Support untuk bantuan lebih lanjut."
        )

    prompt = build_prompt(query, search_results)
    # Panggilan LLM sinkron dijalankan di thread pool agar tidak memblokir
    # event loop FastAPI (pola Sesi 13.4).
    return await _run_llm(prompt)


async def _run_llm(prompt: str) -> str:
    import asyncio
    return await asyncio.to_thread(_call_llm, prompt)