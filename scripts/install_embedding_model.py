#!/usr/bin/env python3
"""Preload local embedding model for semantic filtering."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Download embedding model to local cache")
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--kind", default="embedding", choices=["embedding", "reranker"])
    args = parser.parse_args()

    try:
        from sentence_transformers import SentenceTransformer, CrossEncoder  # type: ignore
    except Exception as exc:
        print(f"[ERROR] sentence-transformers not available: {exc}")
        return 1

    try:
        if args.kind == "embedding":
            SentenceTransformer(args.model)
            print(f"[OK] Embedding model ready: {args.model}")
        else:
            CrossEncoder(args.model)
            print(f"[OK] Reranker model ready: {args.model}")
        return 0
    except Exception as exc:
        print(f"[ERROR] Failed to load model {args.model}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
