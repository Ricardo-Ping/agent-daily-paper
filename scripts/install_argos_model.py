#!/usr/bin/env python3
"""Install Argos Translate language model pair (configurable).

Important:
- Merely having source/target languages listed in installed languages does NOT
  guarantee a translation package is actually available between them.
- We verify that `src.get_translation(dst)` returns a usable translator.
"""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Argos Translate language model")
    parser.add_argument("--from-code", default="en")
    parser.add_argument("--to-code", default="zh")
    args = parser.parse_args()

    try:
        from argostranslate import package
        from argostranslate import translate as argos_translate
    except Exception as exc:
        print(f"[ERROR] argostranslate not available: {exc}")
        return 1

    langs = argos_translate.get_installed_languages()

    def _pick_lang(code: str):
        code = str(code or "").strip()
        if not code:
            return None
        candidates = [x for x in langs if str(getattr(x, "code", "")).lower() == code.lower()]
        if candidates:
            return candidates[0]
        if code.lower() == "zh":
            candidates = [x for x in langs if str(getattr(x, "code", "")).lower().startswith("zh")]
            if candidates:
                return candidates[0]
        return None

    src = _pick_lang(args.from_code)
    dst = _pick_lang(args.to_code)
    if src and dst:
        try:
            tr = src.get_translation(dst)
            if tr is not None and hasattr(tr, "translate"):
                print(f"[OK] Argos model already installed: {args.from_code}->{getattr(dst, 'code', args.to_code)}")
                return 0
        except Exception:
            pass

    package.update_package_index()
    candidates = [
        p for p in package.get_available_packages()
        if str(p.from_code).lower() == str(args.from_code).lower()
        and (
            str(p.to_code).lower() == str(args.to_code).lower()
            or (str(args.to_code).lower() == "zh" and str(p.to_code).lower().startswith("zh"))
        )
    ]
    if not candidates:
        print(f"[ERROR] No package found for {args.from_code}->{args.to_code}")
        return 1

    pkg = candidates[0]
    download_path = pkg.download()
    package.install_from_path(download_path)
    # Re-check translator availability after install.
    langs = argos_translate.get_installed_languages()
    src = next((x for x in langs if str(getattr(x, "code", "")).lower() == str(args.from_code).lower()), None)
    if src is None and str(args.from_code).lower() == "zh":
        src = next((x for x in langs if str(getattr(x, "code", "")).lower().startswith("zh")), None)
    dst = next((x for x in langs if str(getattr(x, "code", "")).lower() == str(args.to_code).lower()), None)
    if dst is None and str(args.to_code).lower() == "zh":
        dst = next((x for x in langs if str(getattr(x, "code", "")).lower().startswith("zh")), None)
    if not src or not dst:
        print(f"[ERROR] Installed package but language objects not found for {args.from_code}->{args.to_code}")
        return 1
    try:
        tr = src.get_translation(dst)
    except Exception as exc:
        print(f"[ERROR] Installed package but cannot create translator: {exc}")
        return 1
    if tr is None or not hasattr(tr, "translate"):
        print(f"[ERROR] Installed package but translator is unavailable for {args.from_code}->{args.to_code}")
        return 1
    print(f"[OK] Installed Argos model and verified translator: {pkg.from_code}->{pkg.to_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
