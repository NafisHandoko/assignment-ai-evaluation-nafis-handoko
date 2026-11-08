"""
Uji ulang skenario kunci SETELAH perbaikan, dengan pengukuran yang sama
seperti saat pengukuran before (measure_breakdown.py).
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
OUT = BASE / "evaluation" / "after_improvement_measurements.json"

# Skenario yang diuji ulang: semua yang lewat jalur faq + 1 kontrol
RETEST = ["EVAL-01", "EVAL-02", "EVAL-03", "EVAL-04", "EVAL-05", "EVAL-10", "EVAL-11", "EVAL-12"]


def call_llm_with_usage(prompt, max_tokens=None):
    if max_tokens is None:
        max_tokens = rag.MAX_OUTPUT_TOKENS
    if rag._USE_GEMINI:
        from google.genai import types
        t0 = time.perf_counter()
        resp = rag._client.models.generate_content(
            model=rag.LLM_MODEL, contents=prompt,
            config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=max_tokens),
        )
        dt = time.perf_counter() - t0
        m = getattr(resp, "usage_metadata", None)
        return (resp.text or "").strip(), dt, {
            "prompt_tokens": getattr(m, "prompt_token_count", None),
            "completion_tokens": getattr(m, "candidates_token_count", None),
            "total_tokens": getattr(m, "total_token_count", None),
        }

    import urllib.request
    payload = {
        "model": rag.LLM_MODEL_OR,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3, "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {rag.OPENROUTER_API_KEY}",
                 "Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    d = json.loads(urllib.request.urlopen(req, timeout=180).read())
    dt = time.perf_counter() - t0
    u = d.get("usage", {}) or {}
    return (d["choices"][0]["message"].get("content") or "").strip(), dt, {
        "prompt_tokens": u.get("prompt_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "total_tokens": u.get("total_tokens"),
        "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
    }


async def main():
    rag.build_index()
    df = pd.read_csv(SCENARIOS)
    df = df.loc[:, [c for c in df.columns if not c.startswith("Unnamed")]]

    rows = []
    for sid in RETEST:
        row = df[df["scenario_id"] == sid].iloc[0]
        query = row["query"]

        t_all = time.perf_counter()

        # Jalur greeting / no-context tidak memanggil LLM sama sekali.
        if rag.is_greeting(query):
            answer = await rag.answer_from_knowledge_base(query)
            total = time.perf_counter() - t_all
            print(f"[{sid}] GREETING FAST-PATH | total={total:.3f}s | tokens=0")
            print(f"      -> {answer[:100]}\n")
            rows.append({
                "scenario_id": sid, "query": query, "path": "greeting_fastpath",
                "t_retrieval_s": 0.0, "t_llm_s": 0.0, "t_total_s": round(total, 3),
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "answer_chars": len(answer), "answer": answer,
            })
            continue

        t0 = time.perf_counter()
        results = rag.semantic_search(query, top_k=3)
        t_ret = time.perf_counter() - t0
        filtered = rag._filter_relevant(results)

        if not filtered:
            answer = await rag.answer_from_knowledge_base(query)
            total = time.perf_counter() - t_all
            print(f"[{sid}] NO-CONTEXT FAST-PATH (sim tertinggi {results[0]['similarity']:.3f}) "
                  f"| retrieval={t_ret:.2f}s total={total:.3f}s | tokens=0")
            print(f"      -> {answer[:100]}\n")
            rows.append({
                "scenario_id": sid, "query": query, "path": "no_context_fastpath",
                "t_retrieval_s": round(t_ret, 3), "t_llm_s": 0.0, "t_total_s": round(total, 3),
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "answer_chars": len(answer), "answer": answer,
            })
            continue

        prompt = rag.build_prompt(query, filtered)
        answer, t_llm, usage = await asyncio.to_thread(call_llm_with_usage, prompt)
        total = time.perf_counter() - t_all
        print(f"[{sid}] RAG | retrieval={t_ret:.2f}s llm={t_llm:.2f}s total={total:.2f}s "
              f"| tokens={usage.get('total_tokens')} (prompt={usage.get('prompt_tokens')}, "
              f"completion={usage.get('completion_tokens')}) | ctx={len(filtered)} FAQ")
        print(f"      -> ({len(answer)} chars) {answer[:120]}\n")

        rows.append({
            "scenario_id": sid, "query": query, "path": "rag",
            "t_retrieval_s": round(t_ret, 3), "t_llm_s": round(t_llm, 3),
            "t_total_s": round(total, 3),
            "context_faqs": len(filtered), "prompt_chars": len(prompt),
            **usage, "answer_chars": len(answer), "answer": answer,
        })

    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Disimpan: {OUT}")


asyncio.run(main())