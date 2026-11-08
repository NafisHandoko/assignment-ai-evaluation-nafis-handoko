"""
Router: menentukan apakah query user butuh RAG (informasi/kebijakan)
atau tool call (aksi spesifik, misal cek status tiket).

Pola ini mengikuti workflow router yang dipelajari di Sesi 12.4:
  user input -> intent detection -> jika butuh data -> retrieval (RAG)
                                  -> jika butuh aksi -> tool call

Intent detection memakai dua lapis:
  1. LLM (Gemini / OpenRouter fallback) dengan structured prompt: utama.
  2. Baseline keyword sebagai jaring pengaman: jika LLM gagal merespons
     atau mengembalikan label di luar yang dikenal, query yang menyebut
     "tiket"/nomor tiket tetap diarahkan ke ticket_status.
"""

import json
import os
import re
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from app.rag import answer_from_knowledge_base, _USE_GEMINI, _client, LLM_MODEL, LLM_MODEL_OR
from app.tools import get_ticket_status

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Intent yang dikenal
INTENT_FAQ = "faq"
INTENT_TICKET = "ticket_status"
INTENT_UNKNOWN = "unknown"

# Label sah hasil klasifikasi LLM
VALID_INTENTS = {INTENT_FAQ, INTENT_TICKET}

# Format nomor tiket pada mock_tickets.csv: TICKET-XXXX (digit 1-4).
TICKET_ID_PATTERN = re.compile(r"TICKET-\d{1,4}", re.IGNORECASE)

# Kata kunci yang menandakan permintaan cek status tiket
# (dipakai untuk baseline / jaring pengaman).
TICKET_KEYWORDS = ("tiket", "status tiket", "ticket", "ticket_id", "nomor tiket", "no tiket")

PROMPT_INTENT = """Klasifikasikan pertanyaan karyawan berikut ke dalam SATU label:

faq            - pertanyaan informasi/kebijakan umum perusahaan (cuti, password,
                 email, hardware, jaringan, software, dsb.)
ticket_status  - permintaan mengecek status tiket tertentu, biasanya menyebut
                 nomor tiket seperti TICKET-0001

Jawab hanya dengan satu kata label, tanpa penjelasan.

PERTANYAAN: {query}"""


def _call_llm_intent(query: str, max_tokens: int = 10) -> str:
    """Memanggil LLM untuk klasifikasi intent (sinkron, dipanggil via thread pool)."""
    prompt = PROMPT_INTENT.format(query=query)

    if _USE_GEMINI:
        from google.genai import types

        response = _client.models.generate_content(
            model=LLM_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=max_tokens),
        )
        return (response.text or "").strip()

    payload = {
        "model": LLM_MODEL_OR,
        "messages": [{"role": "user", "content": prompt}],
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
    d = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return (d["choices"][0]["message"].get("content") or "").strip()


def _keyword_intent(query: str) -> str:
    """Baseline sederhana berbasis keyword (jaring pengaman / fallback)."""
    q = query.lower()
    if any(kw in q for kw in TICKET_KEYWORDS) or TICKET_ID_PATTERN.search(query):
        return INTENT_TICKET
    return INTENT_FAQ


def extract_ticket_id(query: str) -> str:
    """
    Mengekstrak ticket_id dari teks query menggunakan regex.

    Format: TICKET-XXXX (case-insensitive). Jika tidak ditemukan,
    mengembalikan string kosong.
    """
    match = TICKET_ID_PATTERN.search(query)
    if not match:
        return ""
    # Normalisasi ke huruf besar agar konsisten dengan data (TICKET-0001).
    return match.group(0).upper()


async def detect_intent(query: str) -> str:
    """
    Menentukan intent dari query user:
      - "faq"           -> narah ke RAG
      - "ticket_status" -> narah ke tool call

    Prioritas: LLM dulu; jika gagal atau label tidak sah, gunakan baseline
    keyword. Jika keduanya tidak yakin, default ke faq (aman, RAG yang
    menjawab dengan knowledge base).
    """
    # Jalur cepat: sapaan singkat pasti bukan permintaan cek tiket, jadi tidak
    # perlu memanggil LLM hanya untuk mengklasifikasikannya. Berdasarkan
    # evaluasi, pemanggilan LLM untuk klasifikasi ini memakan beberapa detik
    # pada setiap request, termasuk request yang sepele.
    if _is_simple_greeting(query):
        return INTENT_FAQ

    label = None

    # Lapis 1: LLM (dijalankan di thread pool agar tidak memblokir event loop).
    try:
        import asyncio

        raw = await asyncio.to_thread(_call_llm_intent, query)
        cleaned = (raw or "").strip().lower().split()[0].strip(".,:;\"'")
        if cleaned in VALID_INTENTS:
            label = cleaned
    except Exception:
        label = None

    # Lapis 2: baseline keyword (jaring pengaman).
    if label is None:
        label = _keyword_intent(query)

    return label


def _is_simple_greeting(query: str) -> bool:
    """Sapaan/obrolan singkat yang jelas bukan permintaan cek tiket."""
    normalized = query.strip().lower().strip("!?.,")
    if not normalized:
        return False
    if TICKET_ID_PATTERN.search(query):
        return False
    if any(kw in normalized for kw in TICKET_KEYWORDS):
        return False
    words = normalized.split()
    return len(words) <= 4 and all(w.strip("!?.,") in _GREETING_WORDS for w in words)


_GREETING_WORDS = {
    "halo", "hai", "hi", "hello", "pagi", "siang", "sore", "malam",
    "selamat", "assalamualaikum", "terima", "kasih", "makasih", "thanks",
    "ok", "oke",
}


async def handle_query(query: str) -> dict:
    """
    Fungsi utama yang dipanggil oleh endpoint /ask.
    Menjalankan intent detection, lalu merutekan ke RAG atau tool call.
    """
    intent = await detect_intent(query)

    if intent == INTENT_TICKET:
        ticket_id = extract_ticket_id(query)
        data = await _get_ticket_status_safe(ticket_id, query)
        return {"status": "success", "intent": INTENT_TICKET, "data": data}

    if intent == INTENT_FAQ:
        try:
            data = await answer_from_knowledge_base(query)
            return {"status": "success", "intent": INTENT_FAQ, "data": data}
        except Exception as exc:
            return {
                "status": "error",
                "intent": INTENT_FAQ,
                "data": f"Maaf, terjadi kendala saat memproses pertanyaan Anda: {exc}",
            }

    return {
        "status": "error",
        "intent": INTENT_UNKNOWN,
        "data": "Maaf, saya tidak memahami permintaan Anda. "
                "Coba tanyakan hal seputar kebijakan perusahaan atau status tiket IT.",
    }


async def _get_ticket_status_safe(ticket_id: str, raw_query: str) -> str:
    """
    Memanggil tool get_ticket_status dengan penanganan edge case:
    - Jika tidak ada nomor tiket yang terekstrak, hasilkan pesan jelas
      (pengguna menyebut 'tiket' tapi tanpa nomor, misal: "status tiket saya").
    """
    if not ticket_id:
        return (
            "Saya mendeteksi Anda menanyakan status tiket, tetapi tidak menemukan "
            "nomor tiketnya. Format nomor tiket: TICKET-XXXX (contoh: TICKET-0001). "
            "Silakan sebutkan nomor tiket Anda."
        )
    import asyncio

    return await asyncio.to_thread(get_ticket_status, ticket_id)