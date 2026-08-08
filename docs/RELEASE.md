# Release process

This process distinguishes a locally verified candidate from a formal release.
A dirty or untracked local working tree can produce useful hashes and a
dirty-local record, but its `HEAD` is not the source identity of the artifacts.

## 1. Freeze the source

A formal release starts from a reviewed, clean checkout with no untracked files
and the exact tag `v{project.version}`. A different tag at `HEAD` does not make
the candidate eligible. Record the matching tag and `git rev-parse HEAD`; never
copy a revision into provenance from a human-provided string. Run the release
process from that checkout, not from a shared dirty worktree.

The curated `catalog/index.json` remains empty until artifacts are hosted, their
final hashes are known, review is approved, and a durable provenance URL exists.

## 2. Build through locked mainland sources

The reproducible input contract is the digest-pinned DaoCloud Python image,
Tsinghua's configurable Python index, and `requirements-build.txt` installed
with `--require-hashes`. Build in a temporary copy so stale local `*.egg-info`,
caches, tests, and agent metadata cannot enter the artifacts:

```powershell
$source = (Resolve-Path .).Path
$output = Join-Path $source "dist"
New-Item -ItemType Directory -Force -Path $output | Out-Null
$image = "docker.m.daocloud.io/library/python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b"

docker run --rm --init `
  --mount "type=bind,source=$source,target=/source,readonly" `
  --mount "type=bind,source=$output,target=/out" `
  --entrypoint sh $image -ceu '
    export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
    export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
    mkdir -p /tmp/sasori
    cp /source/pyproject.toml /source/MANIFEST.in /source/README.md \
       /source/LICENSE /source/SECURITY.md /source/THIRD_PARTY_NOTICES.md \
       /source/requirements-build.txt /tmp/sasori/
    cp -a /source/docs /source/licenses /source/src /tmp/sasori/
    find /tmp/sasori -type d \( -name "*.egg-info" -o -name __pycache__ \) \
      -prune -exec rm -rf -- {} +
    cd /tmp/sasori
    python -m pip install --require-hashes -r requirements-build.txt
    python -m pip wheel --no-build-isolation --no-deps --wheel-dir /out .
    python - <<PY
from setuptools.build_meta import build_sdist
print(build_sdist(chr(47)+chr(111)+chr(117)+chr(116)))
PY
  '
if ($LASTEXITCODE -ne 0) { throw "artifact build failed" }
```

The mirror is transport only: dependency versions, hashes, base-image digest,
and license obligations stay unchanged. If a future image uses `apt-get`, it
must switch Debian sources to a tested mainland mirror such as Aliyun before
the update and lock the installed versions/closure.

## 3. Verify artifacts and local records

The verifier uses only the standard library. It checks safe archive paths,
member limits, wheel `RECORD`, metadata/license, zero runtime dependencies,
console/plugin entry points, exact source bytes, Workbench package data, sdist
rebuild inputs, the 250 KiB wheel threshold, build locks, and Docker defaults.
It writes an application-artifact SPDX 2.3 JSON SBOM, artifact manifest, and
unsigned local provenance record:

```powershell
$version = python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw "project version lookup failed" }
$wheel = Join-Path (Resolve-Path .).Path "dist\sasori-$version-py3-none-any.whl"
$sdist = Join-Path (Resolve-Path .).Path "dist\sasori-$version.tar.gz"
python scripts/release_verify.py `
  --wheel $wheel `
  --sdist $sdist `
  --source-root . `
  --output dist/release-metadata
```

Exit `0` requires a clean exact-tag checkout. For diagnostics only, a
successfully verified input that is not release eligible may use
`--allow-dirty-local`; it writes records and exits `5`. The option does not
downgrade a clean exact-tag input, which still exits `0`. Non-release records
say `release_eligible=false`, bind artifacts only to the current working tree,
and keep `HEAD` as a non-authoritative baseline. They are not signed SLSA/in-toto
provenance or a trusted-builder attestation.

The generated SPDX file covers the wheel/sdist and locked Python build input.
It is not a container SBOM. Generate and archive a separate SPDX/CycloneDX SBOM
from the actual final image digest before publishing that image.

## 4. Install and test the built wheel

Run two distinct gates on Python 3.11, 3.12, and 3.13: the source regression
suite, and the installed-wheel smoke from a directory that cannot import the
checkout's `src/`. The smoke verifies distribution metadata, zero runtime
dependencies, five import packages, six Workbench resources, and all three
console scripts.

On Windows, the following uses the Python launcher to select each interpreter:

```powershell
$source = (Resolve-Path .).Path
$version = python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw "project version lookup failed" }
$wheel = Join-Path $source "dist\sasori-$version-py3-none-any.whl"
$smoke = Join-Path $source "scripts\installed_wheel_smoke.py"
foreach ($pythonVersion in @("3.11", "3.12", "3.13")) {
  $venv = Join-Path $source ("dist\verify-py" + $pythonVersion.Replace(".", ""))
  py "-$pythonVersion" -m venv $venv
  $python = Join-Path $venv "Scripts\python.exe"
  & $python -m pip install --no-deps $wheel
  Push-Location ([IO.Path]::GetTempPath())
  try { & $python $smoke } finally { Pop-Location }
  & $python -m unittest discover -s (Join-Path $source "tests") -v
  if ($LASTEXITCODE -ne 0) { throw "Python $pythonVersion acceptance failed" }
}
```

For Linux/amd64, use these digest-pinned DaoCloud images. These full Python
images already contain Git 2.47.3, so the matrix does not run `apt-get` or add
an unlocked package closure. It proves the POSIX executable-bit regression is
not skipped before running the full suite:

```powershell
$source = (Resolve-Path .).Path
$version = python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0) { throw "project version lookup failed" }
$images = @(
  "docker.m.daocloud.io/library/python:3.11@sha256:d0199e2a90bf7a206a485b115323a75bc946f30b463d704c5435a454aca084dd",
  "docker.m.daocloud.io/library/python:3.12@sha256:7ad6d21a25a94b2c00e685e82c2fd298de814353d9ee0e3f7f2cd4fca063df60",
  "docker.m.daocloud.io/library/python:3.13@sha256:36f5673ec01bd1001d7cbb8f12215101aa4ee5d70ddbbb72e01b2930d7c12f19"
)
foreach ($image in $images) {
  docker run --rm --init --platform linux/amd64 `
    --mount "type=bind,source=$source,target=/source,readonly" `
    --env "SASORI_VERSION=$version" `
    --entrypoint sh $image -ceu '
      test "$(git --version)" = "git version 2.47.3"
      python -m venv /tmp/sasori-verify
      /tmp/sasori-verify/bin/python -m pip install --no-deps \
        "/source/dist/sasori-${SASORI_VERSION}-py3-none-any.whl"
      cd /tmp
      /tmp/sasori-verify/bin/python /source/scripts/installed_wheel_smoke.py
      cd /source
      /tmp/sasori-verify/bin/python -m unittest discover \
        -s tests -p "test_git_plugin.py" -v
      /tmp/sasori-verify/bin/python -m unittest discover -s tests -v
    '
  if ($LASTEXITCODE -ne 0) { throw "Linux acceptance failed for $image" }
}
```

The Linux Git version and image digests are one tested matrix snapshot; update
them deliberately together. The output must
show `test_executable_bit_change_invalidates_approved_snapshot ... ok`, never
`skipped`. The Windows junction tests must run without administrator symlink
rights.

Real-provider smoke tests supplement, never replace, deterministic provider
conformance. With credentials kept in the normal environment, run:

```powershell
python scripts/provider_smoke.py --provider openai --model YOUR_OPENAI_MODEL
python scripts/provider_smoke.py --provider anthropic --model YOUR_ANTHROPIC_MODEL
```

## 5. Container and product acceptance

Run a no-cache Compose build through the configured mainland sources, then the
real workflow rather than health-only checks:

```text
POST /v1/runs
  -> approval_required
  -> durable approval
  -> resume_required
  -> explicit resume
  -> completed
  -> SSE reconnect from the durable cursor
  -> container restart
  -> unchanged final and effect count
  -> second database owner rejected
```

Keep the named data volume during restart/owner testing. Do not publish a public
deployment without the additional TLS, isolation, authentication, limits,
backup, monitoring, and secret-management controls in `SECURITY.md`.

## 6. Publish gate

Before uploading or tagging a release, all of the following must be true:

- Windows and Linux 3.11-3.13 source regression and installed-wheel smoke
  matrices pass;
- OpenAI and Anthropic each pass the real two-turn tool smoke without exposing
  credentials, and any claimed streaming behavior has its own conformance test;
- the no-cache domestic-source Compose workflow passes on the final revision;
- wheel/sdist manifest, application SBOM, image SBOM, notices, and trusted build
  provenance are archived and their subjects match the published digests;
- the working tree is clean and exactly tagged; and
- `catalog/index.json` changes only after hosting, review, and provenance are
  real.

Signing, Trusted Publishing, rollback, and central marketplace publication are
not implemented by the local verifier. Add them only when a real hosting and
maintainer-operating contract exists.
