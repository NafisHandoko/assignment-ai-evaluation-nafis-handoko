"""
Modul Tools: fungsi-fungsi "aksi" yang dipanggil router ketika intent
BUKAN pertanyaan informasi/kebijakan, melainkan permintaan spesifik
yang butuh pengecekan data langsung (bukan retrieval semantik).

Pola ini mengikuti konsep Tool Integration di Sesi 12.3: tool adalah
fungsi Python biasa yang mengembalikan data, lalu dipilih oleh router
berdasarkan intent yang terdeteksi.
"""

from pathlib import Path

import pandas as pd

MOCK_TICKETS_PATH = Path(__file__).resolve().parent.parent / "data" / "mock_tickets.csv"

# Cache data tiket: dimuat sekali, dipakai untuk semua pemanggilan.
_tickets_df = None


def _load_tickets() -> pd.DataFrame:
    """Memuat mock_tickets.csv dengan lazy loading + cache."""
    global _tickets_df
    if _tickets_df is None:
        _tickets_df = pd.read_csv(MOCK_TICKETS_PATH)
    return _tickets_df


def get_ticket_status(ticket_id: str) -> str:
    """
    Mencari status tiket berdasarkan ticket_id pada data mock_tickets.csv.

    Jika ditemukan, mengembalikan ringkasan: subject, status, dan last_update.
    Jika tidak ditemukan, mengembalikan pesan informatif (tidak crash).
    """
    if not ticket_id:
        return "Tiket tidak ditemukan: tidak ada nomor tiket yang diberikan."

    df = _load_tickets()
    match = df[df["ticket_id"].str.upper() == ticket_id.strip().upper()]

    if match.empty:
        return (
            f"Tiket {ticket_id} tidak ditemukan di sistem. "
            "Periksa kembali nomor tiket Anda, atau hubungi tim IT Support "
            "jika Anda yakin nomor tiket sudah benar."
        )

    row = match.iloc[0]
    return (
        f"Tiket {row['ticket_id']} ({row['category']}): {row['subject']}. "
        f"Status: {row['status']}. "
        f"Pembaruan terakhir: {row['last_update']}."
    )