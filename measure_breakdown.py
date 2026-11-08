"""
Instrumentasi: mengukur breakdown latency (retrieval vs LLM call) dan token usage.

Dijalankan in-process (import modul system) supaya tiap fase bisa diukur terpisah
dan response mentah OpenRouter (termasuk field 'usage') bisa dibaca.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

BASE = Path("/tmp/ai-evaluation-assignment")
SYSTEM = BASE / "system"
sys.path.insert(0, str(SYSTEM))
os.chdir(SYSTEM)

import pandas as pd

from app import rag

SCENARIOS = BASE / "evaluation" / "evaluation_test_scenarios.csv"
OUT = BASE / "evaluation" / "latency_token_breakdown.json"


def call_llm_with_usage(prompt: str, max_tokens: int = 600):
    """Panggil LLM via jalur yang sama, tapi kembalikan usage mentah."""
    if rag._USE_GEMINI:
        from google.genai import types
        t0 = time.perf_counter()
        resp = rag._client.models.generate_content(
            model=rag.LLM_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=max_tokens),
        )
        dt = time.perf_counter() - t0
        meta = getattr(resp, "usage_metadata", None)
        usage = {
            "prompt_tokens": getattr(meta, "prompt_token_count", None),
            "completion_tokens": getattr(meta, "candidates_token_count", None),
            "total_tokens": getattr(meta, "total_token_count", None),
        }
        return (resp.text or "").strip(), dt, usage

    payload = {
        "model": rag.LLM_MODEL_OR,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    import urllib.request
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {rag.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    t0 = time.perf_counter()
    d = json.loads(urllib.request.urlopen(req, timeout=180).read())
    dt = time.perf_counter() - t0
    content = (d["choices"][0]["message"].get("content") or "").strip()
    u = d.get("usage", {}) or {}
    usage = {
        "prompt_tokens": u.get("prompt_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "total_tokens": u.get("total_tokens"),
        "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "cost_usd": u.get("cost"),
    }
    return content, dt, usage


async def main():
    print("Membangun index...", flush=True)
    t_idx = time.perf_counter()
    rag.build_index()
    print(f"build_index: {time.perf_counter() - t_idx:.2f}s "
          f"({len(rag._documents)} dokumen, jalur={'Gemini' if rag._USE_GEMINI else 'OpenRouter'})\n", flush=True)

    df = pd.read_csv(SCENARIOS)
    df = df.loc[:, [c for c in df.columns if not c.startswith("Unnamed")]]

    rows = []
    for _, row in df.iterrows():
        sid = row["scenario_id"]
        query = row["query"]
        intent_expected = row["category"]

        # Fase 1: retrieval (embedding query + FAISS search)
        t0 = time.perf_counter()
        results = rag.semantic_search(query, top_k=3)
        t_retrieval = time.perf_counter() - t0

        # Fase 2: prompt build (murah, CPU)
        t0 = time.perf_counter()
        prompt = rag.build_prompt(query, results)
        t_prompt = time.perf_counter() - t0

        # Fase 3: LLM call
        answer, t_llm, usage = await asyncio.to_thread(call_llm_with_usage, prompt)

        total = t_retrieval + t_prompt + t_llm
        top_sim = results[0]["similarity"] if results else 0.0

        print(f"[{sid}] retrieval={t_retrieval:5.2f}s llm={t_llm:6.2f}s total={total:6.2f}s "
              f"| tokens={usage.get('total_tokens')} (prompt={usage.get('prompt_tokens')}, "
              f"completion={usage.get('completion_tokens')}) | top_sim={top_sim:.3f} "
              f"| prompt_chars={len(prompt)}")

        rows.append({
            "scenario_id": sid,
            "category": intent_expected,
            "query": query,
            "t_retrieval_s": round(t_retrieval, 3),
            "t_prompt_s": round(t_prompt, 5),
            "t_llm_s": round(t_llm, 3),
            "t_total_s": round(total, 3),
            "prompt_chars": len(prompt),
            "top_similarity": round(top_sim, 4),
            "answer_chars": len(answer),
            **usage,
        })
        print(f"      -> answer ({len(answer)} chars): {answer[:90]}\n", flush=True)

    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDisimpan: {OUT}")

    # Ringkasan
    tot_ret = sum(r["t_retrieval_s"] for r in rows)
    tot_llm = sum(r["t_llm_s"] for r in rows)
    print(f"\n=== RINGKASAN ===")
    print(f"Total retrieval: {tot_ret:.2f}s ({tot_ret/(tot_ret+tot_llm)*100:.1f}%)")
    print(f"Total LLM      : {tot_llm:.2f}s ({tot_llm/(tot_ret+tot_llm)*100:.1f}%)")
    toks = [r.get("total_tokens") or 0 for r in rows]
    print(f"Total token    : {sum(toks)} (rata-rata {sum(toks)/len(toks):.0f})")


asyncio.run(main())