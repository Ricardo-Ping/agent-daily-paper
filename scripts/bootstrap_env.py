#!/usr/bin/env python3
"""One-command bootstrap for agent-daily-paper.

Creates/updates a conda env, installs dependencies, installs Argos model,
and initializes local config files from templates if needed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
    if check and proc.returncode != 0:
        msg = proc.stderr.strip() or proc.stdout.strip() or "command failed"
        raise RuntimeError(f"{' '.join(cmd)}\n{msg}")
    return proc


def find_conda() -> str:
    for name in ("conda", "mamba"):
        exe = shutil.which(name)
        if exe:
            return exe
    raise RuntimeError("Conda not found in PATH. Please install Miniconda/Anaconda first.")


def ensure_env(conda: str, env_name: str, py_ver: str, root: Path) -> None:
    out = run([conda, "env", "list", "--json"], cwd=root)
    data = json.loads(out.stdout)
    envs = data.get("envs", [])
    exists = any(Path(p).name.lower() == env_name.lower() for p in envs)
    if not exists:
        print(f"[BOOTSTRAP] Creating conda env: {env_name} (python={py_ver})")
        run([conda, "create", "-n", env_name, f"python={py_ver}", "-y"], cwd=root)
    else:
        print(f"[BOOTSTRAP] Conda env already exists: {env_name}")


def install_packages(
    conda: str,
    env_name: str,
    root: Path,
    skip_argos_model: bool,
    skip_embedding_model: bool,
) -> None:
    print("[BOOTSTRAP] Installing python packages in env...")
    run([conda, "run", "-n", env_name, "python", "-m", "pip", "install", "--upgrade", "pip"], cwd=root)
    run([conda, "run", "-n", env_name, "python", "-m", "pip", "install", "argostranslate"], cwd=root)
    run([conda, "run", "-n", env_name, "python", "-m", "pip", "install", "sentence-transformers"], cwd=root)

    if not skip_argos_model:
        print("[BOOTSTRAP] Installing Argos en->zh model...")
        run([conda, "run", "-n", env_name, "python", "scripts/install_argos_model.py"], cwd=root)
    else:
        print("[BOOTSTRAP] Skipping Argos model installation by flag.")

    if not skip_embedding_model:
        print("[BOOTSTRAP] Preloading embedding model (BAAI/bge-m3)...")
        proc = run(
            [conda, "run", "-n", env_name, "python", "scripts/install_embedding_model.py", "--model", "BAAI/bge-m3"],
            cwd=root,
            check=False,
        )
        if proc.returncode != 0:
            print("[BOOTSTRAP][WARN] Embedding model preload failed, will download on first run.")
    else:
        print("[BOOTSTRAP] Skipping embedding model preload by flag.")


def ensure_file_from_template(dst: Path, src: Path) -> None:
    if dst.exists():
        print(f"[BOOTSTRAP] Keep existing file: {dst}")
        return
    if not src.exists():
        print(f"[BOOTSTRAP][WARN] Template missing: {src}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    print(f"[BOOTSTRAP] Initialized: {dst} <- {src}")


def ensure_state_file(state_path: Path) -> None:
    if state_path.exists():
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "sent_ids": [],
                "sent_versions": {},
                "last_run_at": None,
                "last_push_date_by_sub": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[BOOTSTRAP] Initialized: {state_path}")


def ensure_subscriptions_placeholder(path: Path) -> None:
    if path.exists():
        print(f"[BOOTSTRAP] Keep existing file: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    placeholder = {
        "setup_required": True,
        "setup_message": "首次使用请先配置：研究领域（可多项）、每领域数量(5-20)、每日推送时间(HH:MM)、时区。",
        "subscriptions": [],
    }
    path.write_text(json.dumps(placeholder, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[BOOTSTRAP] Initialized placeholder config: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap conda env and first-run config")
    parser.add_argument("--env-name", default="arxiv-digest-lab")
    parser.add_argument("--python-version", default="3.10")
    parser.add_argument("--skip-argos-model", action="store_true")
    parser.add_argument("--skip-embedding-model", action="store_true")
    parser.add_argument("--run-doctor", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]

    try:
        conda = find_conda()
        ensure_env(conda, args.env_name, args.python_version, root)
        install_packages(
            conda,
            args.env_name,
            root,
            args.skip_argos_model,
            args.skip_embedding_model,
        )
    except Exception as exc:
        print(f"[BOOTSTRAP][ERROR] {exc}")
        return 1

    ensure_file_from_template(
        root / "config/agent_field_profiles.json",
        root / "config/agent_field_profiles.example.json",
    )
    ensure_subscriptions_placeholder(root / "config/subscriptions.json")
    ensure_state_file(root / "data/state.json")

    if args.run_doctor:
        print("[BOOTSTRAP] Running health check...")
        try:
            run([conda, "run", "-n", args.env_name, "python", "scripts/doctor.py"], cwd=root)
        except Exception as exc:
            print(f"[BOOTSTRAP][WARN] doctor check failed: {exc}")

    print("\n[BOOTSTRAP] Done.")
    print(f"Next: conda run -n {args.env_name} python scripts/doctor.py")
    print(
        f"Then: conda run -n {args.env_name} python scripts/instant_digest.py "
        "--fields \"数据库优化器\" --limit 20 --time-window-hours 72"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

