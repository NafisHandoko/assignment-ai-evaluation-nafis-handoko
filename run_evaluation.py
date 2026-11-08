"""
Menjalankan 12 skenario evaluasi terhadap sistem (end-to-end via HTTP /ask).

Merekam untuk setiap skenario:
  - response aktual
  - latency end-to-end (detik)
  - intent yang terdeteksi
  - status response

Output: evaluation/evaluation_raw_results.json (bahan untuk evaluation_results.csv)
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import requests

BASE = Path("/tmp/ai-evaluation-assignment")
SYSTEM = BASE / "system"
SCENARIOS = BASE / "evaluation" / "evaluation_test_scenarios.csv"
OUT = BASE / "evaluation" / "evaluation_raw_results.json"

API = "http://127.0.0.1:8100"


def wait_ready(timeout=120):
    """Tunggu server siap (health endpoint merespons)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(f"{API}/health", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main():
    df = pd.read_csv(SCENARIOS)
    # Buang kolom kosong hasil trailing comma di CSV
    df = df.loc[:, [c for c in df.columns if not c.startswith("Unnamed")]]
    print(f"Total skenario: {len(df)}")
    print(f"Kolom: {list(df.columns)}\n")

    # Jalankan server
    print("Menjalankan server...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8100"],
        cwd=str(SYSTEM),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        if not wait_ready():
            print("SERVER GAGAL SIAP")
            print(proc.stdout.read() if proc.stdout else "")
            return 1

        print("Server siap.\n")
        results = []

        for _, row in df.iterrows():
            sid = row["scenario_id"]
            kategori = row["category"]
            query = row["query"]

            t0 = time.perf_counter()
            try:
                resp = requests.post(
                    f"{API}/ask",
                    json={"query": query},
                    timeout=180,
                )
                latency = time.perf_counter() - t0
                body = resp.json()
                status = body.get("status", "?")
                intent = body.get("intent", "?")
                data = body.get("data", "")
                http_code = resp.status_code
            except Exception as exc:
                latency = time.perf_counter() - t0
                status = "exception"
                intent = "-"
                data = f"EXCEPTION: {exc}"
                http_code = 0

            print(f"[{sid}] {latency:6.2f}s | {status:8s} | {intent:14s} | {query[:50]}")
            print(f"        -> {data[:110]}")

            results.append({
                "scenario_id": sid,
                "category": kategori,
                "query": query,
                "evaluation_focus": row.get("evaluation_focus", ""),
                "expected_behavior_note": row.get("expected_behavior_note", ""),
                "actual_response": data,
                "latency_seconds": round(latency, 3),
                "status": status,
                "intent": intent,
                "http_code": http_code,
            })

        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nHasil mentah disimpan: {OUT}")

        # Ringkasan latency
        lats = [r["latency_seconds"] for r in results]
        print(f"\nLatency: min={min(lats):.2f}s max={max(lats):.2f}s "
              f"rata-rata={sum(lats)/len(lats):.2f}s")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    return 0


if __name__ == "__main__":
    sys.exit(main())