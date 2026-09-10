# airlift

Build a bundle of your tools on a machine that has internet, carry it across to
machines that don't, and install everything offline with a single command.

## What it's for

A lot of machines can't just `pip install` or `apt-get` what they need. Locked-down
production servers, air-gapped networks, and hosts stuck behind a corporate proxy
all block outbound traffic — so the tools you depend on won't download on the box
where you actually need them.

The usual fix is to fetch everything ahead of time on a machine that *does* have
internet, then hand-carry it across the gap. That already works one ecosystem at a
time: pip has wheelhouses, Go and Rust can vendor their dependencies, apt can
download `.deb` files. What's missing is a single tool that does it for a *mixed*
toolkit and produces one archive you can move around and trust.

airlift is that tool. You list what you need in a small manifest, run one command
on a networked machine, and get a single `.tar.gz` that:

- contains every tool plus all of its dependencies,
- carries its own offline installer,
- is fetched for the platform you're *targeting*, not the one you built on, and
- checks its own integrity at every stop, so a half-copied file fails loudly
  instead of wasting your time later.

It's a single Python script with no third-party dependencies (~350 lines), meant
to be read and adapted rather than treated as a black box.

## How it works

The flow splits cleanly in two:

**Online half — on your build machine.** airlift reads the manifest, downloads
each tool and its dependencies into its own folder, writes an installer, hashes
everything, and packs it all into one archive.

**Offline half — on the target.** You unpack the archive and run its installer. It
re-checks the hashes, then installs each tool without touching the network: Python
tools into their own virtualenv, binaries into a shared `bin/`, source repos
unpacked and ready to run.

Everything before "pack the archive" needs internet. Everything after it doesn't.
That split is the whole point.

## Requirements

On the build machine you need Python 3.11 or newer (airlift uses the
standard-library `tomllib`). `pip` ships with Python; `git` is only needed if you
bundle `git`-type tools; a Go or Rust toolchain only if you cross-compile your own
code with a `build`-type tool.

On the target you need a POSIX shell, `tar`, and `sha256sum` for the core install.
Python tools additionally need `python3` and the `venv` module, which Kali,
Ubuntu, and most distributions already have.

## Step by step

### 1. Describe your toolkit in a manifest

Copy the example and edit it. Keep one manifest per engagement or environment,
under version control:

```bash
cp manifest.example.toml engagement.toml
```

Each tool is one `[[tools]]` block with a `type` that tells airlift where to fetch
it from:

| type | use it for | example |
|--------|-----------|---------|
| `pip`    | a Python package — pulls the whole dependency tree | impacket, netexec |
| `github` | a prebuilt release binary on GitHub | nuclei, ffuf |
| `url`    | any direct file link | a standalone static binary |
| `git`    | a tool you run from a cloned repo | linpeas, a script collection |
| `build`  | your own code, cross-compiled to a static binary | a Go/Rust tool you wrote |

For `github` and `url` tools, a few optional keys handle real-world release
layouts:

- `unpack = true` — extract a `.zip`, `.tar.gz`, or `.gz` asset so a binary
  shipped inside an archive lands as a real binary. (nuclei, for example, ships
  its binary inside a `.zip`.)
- `binaries = ["nuclei"]` — when unpacking, keep only these files and drop the
  LICENSE/README that often ride along.
- `bin_name = "gowitness"` — rename a single fetched binary to a clean command
  name, since raw assets often arrive as `gowitness-3.0-linux-amd64`.

A minimal manifest:

```toml
[bundle]
name = "engagement-kit"        # produces dist/engagement-kit.tar.gz

[target]                        # leave empty when the build host and target
                                # share arch + Python version (see Platform note)

[[tools]]
name = "impacket"
type = "pip"
requirements = ["impacket==0.12.0"]

[[tools]]
name = "linpeas"
type = "git"
url  = "https://github.com/peass-ng/PEASS-ng.git"
ref  = "master"
```

### 2. Build the bundle — needs internet

```bash
python3 airlift.py build engagement.toml
```

airlift fetches every tool and its dependencies, generates the offline installer,
checksums everything, and writes `dist/engagement-kit.tar.gz`. It prints the
archive's SHA-256 at the end — jot it down so you can confirm the file is intact
after each transfer.

### 3. Carry it across, and verify on arrival

Move the archive however the gap allows: `scp`, a shared folder, a USB stick, or
serving it over `python -m http.server`. After it lands on a machine, check it
before you trust it:

```bash
python3 airlift.py verify engagement-kit.tar.gz
```

This re-runs the checksums and fails if a transfer corrupted or truncated the file.
(If a given machine doesn't have airlift.py on it, the installer in the next step
re-checks the same hashes anyway.)

### 4. Install offline and run

On the target, with no internet:

```bash
tar xzf engagement-kit.tar.gz
cd engagement-kit
./install.sh
export PATH="$PWD/installed/bin:$PATH"
```

`install.sh` verifies the checksums first, then installs everything offline. When
it finishes:

- Python tools' commands are on your `PATH` (e.g. `impacket-secretsdump -h`),
- downloaded binaries are in `installed/bin/`,
- cloned source sits under `installed/src/` (e.g.
  `installed/src/linpeas/linPEAS/linpeas.sh`).

### 5. Reuse and refresh

Your manifests are the documented, reproducible version of your toolkit. Next time,
copy the closest one, adjust the tool list, and rebuild. To update a tool, bump its
version pin and run `build` again.

## Platform note — the thing that usually goes wrong

Most "it worked when I built it" failures come down to building for the wrong
platform. If the target is a different architecture, or has an older glibc than
your build machine, fetch for the target explicitly and prefer static binaries.

- **Static binaries dodge the problem.** Go with `CGO_ENABLED=0`, Rust with the
  `x86_64-unknown-linux-musl` target — no shared-library version roulette on an old
  host. The `build` resolver exists for exactly this.
- **Python wheels are platform-specific.** When the build host and target match,
  plain `pip` is most reliable (leave `[target]` empty). When they differ, set
  `pip_platform` / `python_version` / `pip_abi` in `[target]`. Forcing binary-only
  wheels fails if a package ships only source, though; for those, build the wheel on
  a matching base or ship a relocatable interpreter so you don't depend on the
  target's Python at all.
- **apt/.deb is the hard case.** Resolving dependencies against a different base is
  fragile. Where you can, replace an apt dependency with a static binary and skip
  the problem entirely.

## Extending it

Each source type is one `resolve_*` function in the `RESOLVERS` table plus one
branch in `install_snippet`. To add npm, `cargo vendor`, or `.deb` support, follow
the same shape: fetch everything into `tools/<name>/`, then teach the installer how
to install that folder offline.

## Signing (optional)

The checksums prove the bundle wasn't *corrupted* in transit. A GPG signature also
proves it came from *you* and wasn't *tampered with*. To sign, add `--sign` at build
time (optionally with a key id):

```bash
python3 airlift.py build engagement.toml --sign            # default key
python3 airlift.py build engagement.toml --sign KEYID       # specific key
```

This adds a `SHA256SUMS.asc` to the bundle. `verify` checks it automatically, and
`install.sh` verifies it on the target when `gpg` is present and the signer's public
key has been imported there (otherwise it warns and continues). Unsigned bundles are
unaffected — the signature step only runs when you ask for it.

## Deliberately not (yet) added

A few things sound useful but aren't worth the cost for this workflow — noted here:

- **zstd compression.** Python's stdlib can't do zstd before 3.14, so it would mean a
  third-party dependency or requiring the `zstd` binary on both ends — for little gain,
  since binaries and pinned templates don't compress much better than gzip.
- **Incremental/diff bundles.** Shipping only what changed needs the target to already
  hold a previous bundle to diff against, which fresh air-gapped hosts don't. The only
  thing it would speed up is re-downloading on the (networked) build host. The
  `AIRLIFT_CACHE` variable is reserved in case this ever earns its keep.
- **Windows installer (`install.ps1`).** Only worth it if you install tooling *onto*
  Windows hosts rather than running it from a Linux box. Straightforward to add if that
  changes.

## If you'd rather not build your own

- **Python only:** a `pip download` wheelhouse installed with
  `pip install --no-index --find-links`, or package a CLI as a single file with
  `shiv` or `pex`, or `conda-pack` a whole environment into a relocatable tarball.
- **Go only:** cross-compile static binaries, or use `go-offline-packager`.
- **Whole hosts or images:** `docker save` / `docker load` if the target runs Docker.