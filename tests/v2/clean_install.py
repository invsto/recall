"""Clean-install check (spec 9.1 "Clean install", M6 `clean_install`): a fresh virtual environment to the
first save and lookup, timed against the 5-minute target.

    python tests/v2/clean_install.py [--python PYTHON] [--keep]

Steps, all under a new private temporary directory (the repository is never written to):
1. copy the repository's tracked and new files to a scratch source tree;
2. create a new virtual environment (pip comes from the interpreter's bundled ensurepip) and upgrade pip
   (the package index is needed for this and for step 3; without it these steps fail and say so);
3. build a wheel from the scratch tree with `pip wheel --no-deps` (pip's build isolation fetches
   setuptools);
4. install that wheel with `--no-index --no-deps` (no network);
5. from a directory outside the repository run `python -m recall_v2 init`, `principal add`, one `save` and
   one `lookup` against the installed copy, and check the envelopes.

Prints one line per step and `TOTAL <seconds>s`; exits 0 only when every step passed within 300 seconds.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[2]
TARGET_SECONDS = 300


def _run(argv, *, cwd=None, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=TARGET_SECONDS)


def _copy_sources(dest: Path) -> int:
    listed = _run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=REPO)
    if listed.returncode != 0:
        raise RuntimeError("git ls-files failed")
    count = 0
    for name in filter(None, listed.stdout.split("\0")):
        source = REPO / name
        if not source.is_file() or "__pycache__" in source.parts:
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", default=None, help="base interpreter for the new venv (default: this one's base)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--no-pip-upgrade", action="store_true", help="use the venv's bundled pip as is")
    args = parser.parse_args(argv)
    base = args.python or str(Path(sys.base_prefix) / "bin" / f"python{sys.version_info[0]}.{sys.version_info[1]}")

    root = Path(tempfile.mkdtemp(prefix="recall-clean-install-"))
    os.chmod(root, 0o700)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "VIRTUAL_ENV", "PIP_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    started = time.monotonic()
    ok = True

    def step(name: str, proc: subprocess.CompletedProcess) -> bool:
        line = f"{name}: {'ok' if proc.returncode == 0 else 'FAIL'} ({time.monotonic() - started:.1f}s)"
        print(line, flush=True)
        if proc.returncode != 0:
            print((proc.stdout + proc.stderr).strip()[-2000:], flush=True)
        return proc.returncode == 0

    try:
        src, wheels, work = root / "src", root / "wheels", root / "work"
        print(f"sources: {_copy_sources(src)} files copied", flush=True)
        venv = root / "venv"
        python = str(venv / "bin" / "python")
        ok = step("venv", _run([base, "-m", "venv", str(venv)], env=env))
        if ok and not args.no_pip_upgrade:
            # The pip bundled with some interpreters (25.0.x) cannot evaluate the extras markers that current
            # setuptools writes; a clean machine would upgrade pip first. Needs the package index.
            ok = step("pip upgrade", _run([python, "-m", "pip", "install", "--upgrade", "pip"], env=env))
            # Newer pip verifies TLS through the OS trust store; keep the certifi bundle the old pip used,
            # which works behind an intercepting proxy as well as on an open network.
            env["PIP_USE_DEPRECATED"] = "legacy-certs"
        ok = ok and step("wheel", _run([python, "-m", "pip", "wheel", str(src), "--no-deps", "-w", str(wheels)],
                                       env=env))
        built = sorted(wheels.glob("recall-*.whl")) if ok else []
        if ok:
            print(f"built: {built[0].name if built else 'none'}", flush=True)
            ok = bool(built)
        ok = ok and step("install", _run([python, "-m", "pip", "install", "--no-index", "--no-deps",
                                          str(built[0])], env=env))
        if ok:
            work.mkdir(mode=0o700)
            store = str(work / "store" / "store.sqlite3")
            where = _run([python, "-c", "import recall_v2; print(recall_v2.__file__)"], cwd=work, env=env)
            print(f"imported from: {where.stdout.strip()}", flush=True)
            ok = str(venv) in where.stdout

        def cli(*command):
            proc = _run([python, "-m", "recall_v2", *command, "--store", store], cwd=work, env=env)
            ok_step = step(" ".join(command[:2]), proc)
            envelope = json.loads(proc.stdout) if proc.stdout.strip() else {}
            return ok_step and envelope.get("ok") is True, envelope

        if ok:
            ok, _ = cli("init")
        if ok:
            ok, _ = cli("principal", "add", "agent", "--token-out", str(work / "agent.token"))
        if ok:
            ok, saved = cli("save", "--token-file", str(work / "agent.token"), "--kind", "note", "--request-id",
                            "first", "--key", "hello", "--title", "Hello", "--body", "first memory from a clean install")
            ok = ok and saved["results"][0]["state"] == "accepted"
        if ok:
            ok, found = cli("lookup", "--token-file", str(work / "agent.token"), "--query", "clean install")
            ok = ok and found["count"] == 1 and found["results"][0]["key"] == "hello"
            print(f"lookup count: {found.get('count')}", flush=True)
    finally:
        elapsed = time.monotonic() - started
        if args.keep:
            print(f"kept: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    ok = ok and elapsed < TARGET_SECONDS
    print(f"TOTAL {elapsed:.1f}s target <{TARGET_SECONDS}s: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
