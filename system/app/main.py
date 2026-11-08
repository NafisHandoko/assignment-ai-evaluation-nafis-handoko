"""
Entry point aplikasi FastAPI: IT Helpdesk Assistant.

Alur endpoint utama:
  1. Menerima pertanyaan/permintaan dari user
  2. Meneruskan ke router (router.py) untuk menentukan intent
  3. Router memanggil RAG (rag.py) atau tool (tools.py) sesuai intent
  4. Mengembalikan response terstruktur ke client

Error handling (pola Sesi 13.4):
  - Router menangkap kegagalan LLM / intent tidak dikenal dan mengembalikan
    status "error" dengan pesan yang informatif.
  - Endpoint ini menangkap error tak terduga agar API tetap merespons
    dengan HTTP 500 yang rapi, bukan crash.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rag import build_index
from app.router import handle_query

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("helpdesk")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Membangun index FAISS satu kali saat aplikasi startup (bukan per request),
    supaya embedding knowledge base tidak dihitung ulang setiap ada pertanyaan.
    """
    logger.info("Membangun index FAISS dari knowledge base...")
    build_index()
    logger.info("Index siap. Jumlah entri di-load.")
    yield


app = FastAPI(title="IT Helpdesk Assistant API", lifespan=lifespan)


class AskRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Pertanyaan atau permintaan user")


class AskResponse(BaseModel):
    status: str
    intent: str
    data: str


@app.get("/health")
async def health():
    """Endpoint sederhana untuk memastikan service berjalan."""
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest):
    """
    Endpoint utama. Menerima pertanyaan bebas dari user (bisa berupa
    pertanyaan informasi/kebijakan ATAU permintaan cek status tiket),
    lalu meneruskannya ke router untuk diproses.
    """
    try:
        result = await handle_query(request.query)
        return AskResponse(**result)
    except Exception as exc:
        # Jaring pengaman terakhir: error tak terduga tidak boleh membuat
        # API crash: dikembalikan sebagai status error yang jelas.
        logger.exception("Error tak terduga saat memproses query")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "intent": "unknown",
                "data": f"Terjadi kesalahan internal: {exc}",
            },
        )