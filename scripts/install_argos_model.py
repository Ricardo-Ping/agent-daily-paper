#!/usr/bin/env python3
"""Install Argos Translate en->zh model."""

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
    has_from = any(x.code == args.from_code for x in langs)
    has_to = any(x.code in (args.to_code, "zh_CN") for x in langs)
    if has_from and has_to:
        try:
            src = next(x for x in langs if x.code == args.from_code)
            dst = next(x for x in langs if x.code in (args.to_code, "zh_CN"))
            src.get_translation(dst)
            print(f"[OK] Argos model already installed: {args.from_code}->{dst.code}")
            return 0
        except Exception:
            pass

    package.update_package_index()
    candidates = [
        p for p in package.get_available_packages()
        if p.from_code == args.from_code and p.to_code in (args.to_code, "zh_CN")
    ]
    if not candidates:
        print(f"[ERROR] No package found for {args.from_code}->{args.to_code}")
        return 1

    pkg = candidates[0]
    download_path = pkg.download()
    package.install_from_path(download_path)
    print(f"[OK] Installed Argos model: {pkg.from_code}->{pkg.to_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

