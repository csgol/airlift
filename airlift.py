#!/usr/bin/env python3
"""
airlift — build self-contained, offline-installable tool bundles for
air-gapped / proxied targets.

Runs on a BUILD host that HAS internet. Reads a TOML manifest describing the
tools you need, fetches everything (including transitive deps) for a chosen
TARGET platform, checksums it, and packs it into a single archive with an
offline install.sh.

Move the archive across your hops. On the target, unpack and run ./install.sh —
no network needed.

Requires Python 3.11+ (stdlib tomllib). No third-party deps on the build host.
The generated install.sh needs only POSIX sh, tar and sha256sum on the target,
plus python3 + venv for pip-type tools.

Usage:
    ./airlift.py build   manifest.toml [--out PATH] [--keep]
    ./airlift.py verify  bundle.tar.gz
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:                       # Python < 3.11
    try:
        import tomli as tomllib                    # type: ignore
    except ModuleNotFoundError:
        sys.exit("airlift needs Python 3.11+ (or `pip install tomli` on older Pythons).")

__version__ = "0.1.0"

# Reserved for future incremental/diff bundles.
CACHE = Path(os.environ.get("AIRLIFT_CACHE", Path.home() / ".cache" / "airlift"))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[airlift] {msg}", file=sys.stderr)


def run(cmd: list[str], **kw) -> None:
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, headers: dict | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "airlift"})
    log(f"GET {url}")
    with urllib.request.urlopen(req) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    try:
        tar.extractall(dest, filter="data")       # Python 3.12+: blocks path traversal
    except TypeError:
        tar.extractall(dest)                       # older Python; sources are pinned/trusted


def _flatten(src: Path, out: Path, keep: set[str]) -> None:
    """Move regular files from an extracted tree up into <out>, then drop the tree.
    If <keep> is non-empty, keep only files whose basename is listed."""
    for p in src.rglob("*"):
        if p.is_file() and (not keep or p.name in keep):
            shutil.move(str(p), str(out / p.name))
    shutil.rmtree(src, ignore_errors=True)


def _rename_single(tool: dict, out: Path) -> None:
    """If tool.bin_name is set and exactly one file remains, give it that name.
    Handy for raw release assets like 'gowitness-3.0-linux-amd64' -> 'gowitness'."""
    new = tool.get("bin_name")
    if not new:
        return
    files = [p for p in out.iterdir() if p.is_file()]
    if len(files) == 1:
        files[0].rename(out / new)
    else:
        log(f"{tool.get('name','?')}: bin_name set but {len(files)} files present; not renaming")


def _unpack_archives(tool: dict, out: Path) -> None:
    """If tool.unpack is set, extract archive assets in <out> and remove them,
    leaving the raw binaries behind. Optional tool.binaries filters what to keep."""
    if not tool.get("unpack"):
        return
    keep = set(tool.get("binaries", []))
    for archive in list(out.iterdir()):
        low = archive.name.lower()
        if low.endswith(".zip"):
            tmp = out / (archive.name + ".x")
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
            archive.unlink()
            _flatten(tmp, out, keep)
        elif low.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz")):
            tmp = out / (archive.name + ".x")
            with tarfile.open(archive) as t:
                _safe_extract(t, tmp)
            archive.unlink()
            _flatten(tmp, out, keep)
        elif low.endswith(".gz"):                  # single-file gzip: tool_linux_amd64.gz
            target = out / archive.name[:-3]
            with gzip.open(archive, "rb") as f, target.open("wb") as g:
                shutil.copyfileobj(f, g)
            archive.unlink()


# --------------------------------------------------------------------------- #
# resolvers — one per source type. Each writes artifacts into <out> (=tools/<name>)
# --------------------------------------------------------------------------- #
def resolve_pip(tool: dict, out: Path, target: dict) -> None:
    """Fetch a wheelhouse: package(s) + all transitive deps as wheels/sdists."""
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "--dest", str(out)]

    # Cross-platform knobs. Omit from the manifest when build host == target
    # arch/python (the common case) for maximum compatibility.
    plat = target.get("pip_platform")
    pyver = target.get("python_version")
    abi = target.get("pip_abi")
    if plat or pyver or abi:
        cmd += ["--only-binary=:all:"]
        if plat:
            cmd += ["--platform", plat]
        if pyver:
            cmd += ["--python-version", pyver]
        if abi:
            cmd += ["--abi", abi]

    for req in tool.get("requirements", []):
        cmd.append(req)
    for req_file in tool.get("requirement_files", []):
        cmd += ["-r", req_file]
    run(cmd)


def resolve_github(tool: dict, out: Path, target: dict) -> None:
    """Download release asset(s) whose name contains asset_match, then unpack."""
    out.mkdir(parents=True, exist_ok=True)
    repo = tool["repo"]                            # "owner/name"
    tag = tool.get("tag", "latest")
    match = tool.get("asset_match", "")
    api = (f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
           if tag != "latest"
           else f"https://api.github.com/repos/{repo}/releases/latest")
    api_hdr = {"User-Agent": "airlift", "Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        api_hdr["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(api, headers=api_hdr)
    with urllib.request.urlopen(req) as r:
        rel = json.load(r)
    assets = [a for a in rel.get("assets", []) if match in a["name"]]
    if not assets:
        raise SystemExit(f"{repo}: no release asset matching '{match}' on tag '{tag}'")
    for a in assets:
        download(a["browser_download_url"], out / a["name"])
    _unpack_archives(tool, out)
    _rename_single(tool, out)


def resolve_url(tool: dict, out: Path, target: dict) -> None:
    """Download a raw file (standalone binary, tarball, ...), then unpack."""
    out.mkdir(parents=True, exist_ok=True)
    url = tool["url"]
    name = tool.get("filename") or url.rsplit("/", 1)[-1]
    download(url, out / name)
    _unpack_archives(tool, out)
    _rename_single(tool, out)


def resolve_git(tool: dict, out: Path, target: dict) -> None:
    """Shallow-clone a repo and store it as a tarball (for run-from-source tools)."""
    out.mkdir(parents=True, exist_ok=True)
    url = tool["url"]
    ref = tool.get("ref")
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "repo"
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
        cmd += [url, str(clone)]
        run(cmd)
        shutil.rmtree(clone / ".git", ignore_errors=True)
        tarball = out / (tool["name"] + ".tar.gz")
        with tarfile.open(tarball, "w:gz") as t:
            t.add(clone, arcname=tool["name"])


def resolve_build(tool: dict, out: Path, target: dict) -> None:
    """Escape hatch: run a build command (e.g. cross-compile a static binary).

    The command runs with $AIRLIFT_OUT set to this tool's output dir; whatever it
    drops there gets bundled. Example:
        command = 'CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o "$AIRLIFT_OUT/tool" .'
    """
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, AIRLIFT_OUT=str(out))
    run(["sh", "-c", tool["command"]], env=env, cwd=tool.get("cwd"))


RESOLVERS = {
    "pip": resolve_pip,
    "github": resolve_github,
    "url": resolve_url,
    "git": resolve_git,
    "build": resolve_build,
}


# --------------------------------------------------------------------------- #
# install.sh generator — runs offline on the target
# --------------------------------------------------------------------------- #
INSTALL_HEADER = r"""#!/bin/sh
# Generated by airlift. Installs everything offline. No network required.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${AIRLIFT_PREFIX:-$HERE/installed}"
mkdir -p "$PREFIX/bin"

echo "[install] verifying checksums..."
( cd "$HERE" && sha256sum -c SHA256SUMS ) || {
    echo "[install] CHECKSUM MISMATCH — archive may be corrupt/truncated from a hop" >&2
    exit 1
}
echo "[install] checksums OK"

if [ -f "$HERE/SHA256SUMS.asc" ]; then
    if command -v gpg >/dev/null 2>&1; then
        if gpg --verify "$HERE/SHA256SUMS.asc" "$HERE/SHA256SUMS" 2>/dev/null; then
            echo "[install] GPG signature OK"
        else
            echo "[install] WARNING: GPG signature not verified (missing public key or bad signature)" >&2
            echo "[install] import the signer's key and re-run, or verify manually before trusting" >&2
        fi
    else
        echo "[install] signature present but gpg not installed — skipping signature check" >&2
    fi
fi
"""

INSTALL_FOOTER = r"""
echo "[install] done. Tools under: $PREFIX"
echo "[install] add to PATH:  export PATH=\"$PREFIX/bin:\$PATH\""
"""


def install_snippet(tool: dict) -> str:
    name = tool["name"]
    typ = tool["type"]
    d = f'"$HERE/tools/{name}"'
    if typ == "pip":
        venv = tool.get("venv", name)
        reqs = " ".join(tool.get("requirements", []))
        target_pkgs = reqs if reqs else f"{d}/*.whl"
        return (
            f'echo "[install] {name}: venv + offline pip install"\n'
            f'python3 -m venv "$PREFIX/venvs/{venv}"\n'
            f'"$PREFIX/venvs/{venv}/bin/pip" install --no-index --find-links {d} {target_pkgs}\n'
            f'for f in "$PREFIX/venvs/{venv}/bin"/*; do\n'
            f'  b="$(basename "$f")"\n'
            f'  case "$b" in python*|pip*|activate*|*.py) continue;; esac\n'
            f'  ln -sf "$f" "$PREFIX/bin/$b"\n'
            f'done\n'
        )
    if typ in ("github", "url", "build"):
        return (
            f'echo "[install] {name}: copying binaries"\n'
            f'for f in {d}/*; do\n'
            f'  [ -e "$f" ] || continue\n'
            f'  cp "$f" "$PREFIX/bin/"\n'
            f'  chmod +x "$PREFIX/bin/$(basename "$f")" 2>/dev/null || true\n'
            f'done\n'
        )
    if typ == "git":
        return (
            f'echo "[install] {name}: unpacking source"\n'
            f'mkdir -p "$PREFIX/src"\n'
            f'tar -xzf {d}/{name}.tar.gz -C "$PREFIX/src"\n'
        )
    return f'echo "[install] {name}: unknown type {typ}, skipping"\n'


def write_install(stage: Path, tools: list[dict]) -> None:
    parts = [INSTALL_HEADER]
    for t in tools:
        parts.append("\n" + install_snippet(t))
    parts.append(INSTALL_FOOTER)
    p = stage / "install.sh"
    p.write_text("".join(parts))
    p.chmod(0o755)


# --------------------------------------------------------------------------- #
# checksums + packing
# --------------------------------------------------------------------------- #
def write_checksums(stage: Path) -> None:
    lines = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(f"{sha256(path)}  {path.relative_to(stage)}")
    (stage / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def sign_checksums(stage: Path, keyid: str) -> None:
    """GPG-sign SHA256SUMS -> SHA256SUMS.asc (armored detached signature).
    keyid is optional; empty string uses gpg's default signing key."""
    cmd = ["gpg", "--armor", "--detach-sign", "--output", str(stage / "SHA256SUMS.asc")]
    if keyid:
        cmd[1:1] = ["--local-user", keyid]
    cmd.append(str(stage / "SHA256SUMS"))
    run(cmd)


def pack(stage: Path, out_archive: Path, arcname: str) -> None:
    out_archive.parent.mkdir(parents=True, exist_ok=True)
    log(f"packing -> {out_archive}")
    with tarfile.open(out_archive, "w:gz") as t:
        t.add(stage, arcname=arcname)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_build(args) -> None:
    with open(args.manifest, "rb") as f:
        m = tomllib.load(f)

    name = m.get("bundle", {}).get("name", "bundle")
    target = m.get("target", {})
    tools = m.get("tools", [])
    if not tools:
        raise SystemExit("manifest has no [[tools]] entries")

    stage_parent = Path(tempfile.mkdtemp(prefix="airlift-"))
    stage = stage_parent / name
    (stage / "tools").mkdir(parents=True)

    try:
        for t in tools:
            typ = t.get("type")
            if typ not in RESOLVERS:
                raise SystemExit(f"{t.get('name','?')}: unknown type '{typ}'")
            log(f"resolving {t['name']} ({typ})")
            RESOLVERS[typ](t, stage / "tools" / t["name"], target)

        write_install(stage, tools)
        (stage / "MANIFEST.json").write_text(json.dumps(
            {"name": name, "target": target,
             "tools": [{"name": t["name"], "type": t["type"]} for t in tools]},
            indent=2))
        write_checksums(stage)
        if args.sign is not None:
            sign_checksums(stage, args.sign)

        out_archive = Path(args.out or f"dist/{name}.tar.gz")
        pack(stage, out_archive, arcname=name)
        log(f"archive sha256: {sha256(out_archive)}")

        if args.keep:
            keep = Path("dist") / f"{name}-staged"
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(stage, keep)
            log(f"staged tree kept at {keep}")
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
    log("build complete")


def cmd_verify(args) -> None:
    """Re-verify an archive's internal SHA256SUMS. Run this after each hop."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with tarfile.open(args.archive) as t:
                _safe_extract(t, Path(tmp))
        except (tarfile.TarError, OSError, EOFError) as e:
            raise SystemExit(f"verify FAILED: archive is unreadable/corrupt ({e})")
        roots = [p for p in Path(tmp).iterdir() if (p / "SHA256SUMS").exists()]
        if not roots:
            raise SystemExit("verify FAILED: no SHA256SUMS found in archive")
        try:
            run(["sha256sum", "-c", "SHA256SUMS"], cwd=roots[0])
        except subprocess.CalledProcessError:
            raise SystemExit("verify FAILED: checksum mismatch — do not trust this archive")
        asc = roots[0] / "SHA256SUMS.asc"
        if asc.exists():
            if shutil.which("gpg"):
                try:
                    run(["gpg", "--verify", str(asc), str(roots[0] / "SHA256SUMS")])
                    log("signature OK")
                except subprocess.CalledProcessError:
                    raise SystemExit("verify FAILED: bad GPG signature")
            else:
                log("signature present but gpg not installed — signature check skipped")
    log("verify OK")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="airlift", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"airlift {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="fetch deps and pack an offline archive")
    b.add_argument("manifest")
    b.add_argument("--out", help="output archive path (default dist/<name>.tar.gz)")
    b.add_argument("--keep", action="store_true", help="also keep the staged tree")
    b.add_argument("--sign", nargs="?", const="", metavar="KEYID",
                   help="GPG-sign SHA256SUMS (optional key id; default key if omitted)")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="re-check archive checksums after a hop")
    v.add_argument("archive")
    v.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
