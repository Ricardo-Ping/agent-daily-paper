#!/usr/bin/env python3
"""Preload local embedding model for semantic filtering."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Download embedding model to local cache")
    parser.add_argument("--model", default="BAAI/bge-m3")
    args = parser.parse_args()

    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as exc:
        print(f"[ERROR] sentence-transformers not available: {exc}")
        return 1

    try:
        SentenceTransformer(args.model)
        print(f"[OK] Embedding model ready: {args.model}")
        return 0
    except Exception as exc:
        print(f"[ERROR] Failed to load model {args.model}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

