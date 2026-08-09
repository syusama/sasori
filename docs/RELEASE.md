# Release process

This process distinguishes a locally verified candidate from a formal release.
A dirty or untracked local working tree can produce useful hashes and a
dirty-local record, but its `HEAD` is not the source identity of the artifacts.

## 1. Freeze the source

A formal release starts from a reviewed, clean checkout with no untracked files
and the exact tag `v{project.version}`. A different tag at `HEAD` does not make
the candidate eligible. In CI, the actual workflow trigger tag must also equal
`v{project.version}` exactly. If the expected tag and a different triggering tag
both point at the same commit, the different trigger still fails before release
metadata or a bundle is uploaded. Record the matching tag, triggering tag, and
`git rev-parse HEAD`; never copy a revision into provenance from a
human-provided string. Run the release process from that checkout, not from a
shared dirty worktree.

The curated `catalog/index.json` remains empty until artifacts are hosted, their
final hashes are known, review is approved, and a durable provenance URL exists.

## 2. Build through locked mainland sources

The reproducible input contract is the digest-pinned DaoCloud Python image,
Tsinghua's configurable Python index, and `requirements-build.txt` enforced
with `--require-hashes`. The index is used once to populate a portable build
wheelhouse; the package build and downstream source consumers install from
that same wheelhouse with `--no-index`. Build in a temporary copy so stale
local `*.egg-info`, caches, tests, and agent metadata cannot enter the
artifacts:

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
       /source/README_zh.md \
       /source/LICENSE /source/SECURITY.md /source/THIRD_PARTY_NOTICES.md \
       /source/requirements-build.txt /tmp/sasori/
    cp -a /source/docs /source/licenses /source/src /tmp/sasori/
    find /tmp/sasori -type d \( -name "*.egg-info" -o -name __pycache__ \) \
      -prune -exec rm -rf -- {} +
    cd /tmp/sasori
    mkdir -p /out/build-wheelhouse
    python -m pip download \
      --require-hashes \
      --only-binary=:all: \
      --no-deps \
      --dest /out/build-wheelhouse \
      -r requirements-build.txt
    set -- /out/build-wheelhouse/*.whl
    test "$#" -eq 1
    test -f "$1"
    test "${1%-py3-none-any.whl}" != "$1"
    python -m pip --isolated --no-cache-dir install \
      --no-index \
      --find-links /out/build-wheelhouse \
      --only-binary=:all: \
      --require-hashes \
      -r requirements-build.txt
    python -m pip --isolated --no-cache-dir wheel \
      --no-index --no-build-isolation --no-deps --wheel-dir /out .
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

CI uploads the one-wheel build wheelhouse as an unsigned, one-day artifact for
the six rebuilt-sdist jobs in that same workflow run. It is transport, not an
integrity authority: `requirements-build.txt` and pip's repeated
`--require-hashes` checks remain authoritative. The wheelhouse is not included
in the Sasori wheel, sdist, exact-tag candidate, release bundle, application
SBOM, or container SBOM. The shared wheelhouse is valid only while every build
dependency is a `py3-none-any` wheel; platform-specific build dependencies
require separate OS/Python wheelhouses.

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

The tagged GitHub workflow additionally supplies its actual ref name through an
explicit argument:

```powershell
python scripts/release_verify.py `
  --wheel $wheel `
  --sdist $sdist `
  --source-root . `
  --output dist/release-metadata `
  --trigger-tag $env:GITHUB_REF_NAME
```

`--trigger-tag` is not inferred from ambient environment state. When supplied,
it must equal the dynamic project tag exactly and is written into the local
provenance record. The workflow passes it through a quoted environment value so
the tag is data, never shell source.

Exit `0` requires a clean exact-tag checkout. For diagnostics only, a
successfully verified input that is not release eligible may use
`--allow-dirty-local`; it writes records and exits `5`. The option does not
downgrade a clean exact-tag input, which still exits `0`. Non-release records
say `release_eligible=false`, bind artifacts only to the current working tree,
and keep `HEAD` as a non-authoritative baseline. They are not signed SLSA/in-toto
provenance or a trusted-builder attestation.

The generated application SPDX file covers the wheel/sdist and locked Python
build input. It is separate from the container SBOM. After the deterministic
Compose workflow and restart/ownership acceptance pass, the container job
downloads the fixed Syft `1.50.0` Linux archive and verifies its hard-coded
SHA-256 before execution. One scan of that same, still-unchanged `sasori:local`
image writes both SPDX 2.3 and the native Syft catalog.

The job snapshots `docker image inspect` immediately after the build and
byte-compares a second inspection after the scan. `scripts/image_sbom_verify.py`
then binds the daemon image ID, optional daemon descriptor, repo digests,
platform, RootFS layer identities, the accepted Compose container's `.Image`
identity, Git revision, embedded config digest,
Syft-normalized manifest digest, image tag, and exact package/file subjects. It
decodes and hashes the embedded manifest and config rather than trusting the
image name, matches the config `rootfs.diff_ids` to the daemon inspection, and
requires one SPDX document `DESCRIBES` edge to the sole `CONTAINER` root. That
root's checksum and OCI purl must identify the same Syft-normalized manifest;
the daemon descriptor is recorded separately because Docker Desktop can expose
an OCI index ID rather than a config or platform-manifest digest. The running
container's `.Image` must equal that stable daemon ID; on Docker Desktop it can
therefore also be an index ID rather than a config digest.
The three files are uploaded separately as
`sasori-image-sbom-{git-sha}` for 30 days:

```text
sasori-image-{run-id}-{attempt}.spdx.json
sasori-image-{run-id}-{attempt}.syft.json
sasori-image-{run-id}-{attempt}.binding.json
```

The binding remains `signed=false` with the claim `unsigned CI image inventory
binding; not trusted provenance or a signature`. This is component inventory
evidence from the tested local candidate, not a published image, signature,
trusted-builder attestation, or proof that a registry digest is durable.

For an exact tagged workflow, `scripts/release_bundle.py` assembles a new empty
candidate directory with exactly eight regular files and no additional payload:

```text
sasori-{version}-py3-none-any.whl
sasori-{version}.tar.gz
artifact-manifest.json
sasori-{version}.spdx.json
provenance.local.json
LICENSE
THIRD_PARTY_NOTICES.md
licenses/CPYTHON-3.12-LICENSE.txt
```

The bundle verifier rejects missing or extra files, symlinks, digest or subject
mismatches, modified notices, a mismatched trigger tag, and any provenance that
claims signing or trusted attestation. It checks wheel/sdist hashes against the
manifest, then checks the same subjects in SPDX and local provenance. The local
provenance remains `signed=false` with the claim `local verification record; not
a trusted-build attestation`. A later GitHub-hosted attestation is external to
this file and never rewrites that local claim.

## 4. Install and test both distribution paths

Run three distinct gates on Python 3.11, 3.12, and 3.13 across Linux and
Windows: the source regression suite, the installed-wheel smoke, and a source
archive consumer rebuild. Both consumer paths run from a directory that cannot
import the checkout's `src/`. The smoke verifies distribution metadata, zero
runtime dependencies, eight import packages (including the optional
`sasori_memory` extension), eight Workbench resources, and all three console
scripts. The installed-wheel smoke also creates a separate Memory database,
writes one immutable revision, and retrieves it through the installed package;
it does not import the source checkout.

The source-archive gate rejects a missing, nested, symlinked, non-wheel, or
multi-file build wheelhouse before launching a subprocess. It clears inherited
Python-index hints, sets `PIP_NO_INDEX=1`, and installs
`requirements-build.txt` into a dedicated build environment with
`--isolated --no-cache-dir --no-index --find-links ... --require-hashes`. It
rebuilds the verified sdist with
`--isolated --no-cache-dir --no-index --no-build-isolation --no-deps`, installs
the resulting wheel into a second clean environment, and only then runs the
installed-wheel smoke. The rebuilt wheel and original sdist must also pass
`release_verify.py`; exit `5` is the expected
verified-but-unversioned result on branch builds, while an exact release-tag
build may return `0`. Exit codes `1` through `4` always fail the consumer gate.
Directly installing the sdist with implicit build isolation is not equivalent
evidence because it can resolve undeclared or unlocked build inputs.

On an exact tag, the final `Exact-tag release candidate bundle` job waits
explicitly for all five gate families: source tests, package verification,
installed-wheel smoke, rebuilt-sdist smoke, and the container product gate. It
downloads the short-lived internal candidate, reverifies the exact inventory
and all subjects from the tagged checkout, and then invokes the commit-pinned
official `actions/attest` action. Only this tag-only job receives
`id-token: write` and `attestations: write`. The action receives an explicit
newline-delimited list of the eight verified files, not a directory or broad
glob, and creates one signed SLSA build-provenance statement containing eight
name-and-SHA-256 subjects. Attestation failure prevents the longer-lived bundle
upload.

The Sigstore bundle remains external to the candidate directory, so the release
artifact still contains exactly the eight files listed above. It does not turn
`provenance.local.json` or the image-SBOM binding into a signed object. A workflow
definition on a branch is not attestation evidence: claim GitHub-hosted build
provenance only after the exact-tag job succeeds and verification binds the
downloaded subject to this repository, workflow, tag ref, and tag commit. For
example, after downloading and extracting the candidate, verify all eight
subjects rather than checking only the wheel:

```powershell
$version = "<VERSION>"
$tagCommit = "FULL_40_CHARACTER_TAG_COMMIT"
$subjects = @(
  ".\sasori-$version-py3-none-any.whl",
  ".\sasori-$version.tar.gz",
  ".\artifact-manifest.json",
  ".\sasori-$version.spdx.json",
  ".\provenance.local.json",
  ".\LICENSE",
  ".\THIRD_PARTY_NOTICES.md",
  ".\licenses\CPYTHON-3.12-LICENSE.txt"
)
foreach ($subject in $subjects) {
  gh attestation verify $subject `
    --repo syusama/sasori `
    --signer-workflow syusama/sasori/.github/workflows/ci.yml `
    --source-ref "refs/tags/v$version" `
    --source-digest $tagCommit `
    --predicate-type "https://slsa.dev/provenance/v1" `
    --deny-self-hosted-runners
  if ($LASTEXITCODE -ne 0) { throw "attestation verification failed: $subject" }
}
```

A successful attestation verifies the signed provenance statement and subject
digest; it is still not a GitHub Release, PyPI publication, registry image,
deployment approval, SLSA level claim, isolated reusable builder, or proof that
arbitrary predicate fields are independently trustworthy.

GitHub artifact attestations are available for public repositories on current
GitHub.com plans. Private or internal repositories require GitHub Enterprise
Cloud, and GitHub Enterprise Server is unsupported. If the repository
visibility, plan, Actions policy, or OIDC policy no longer permits attestations,
this mandatory job must fail closed; do not add `continue-on-error` and upload
an unattested candidate.

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

Before the container gate, run both real-browser Workbench checks with an
installed Chrome, Chromium, or Edge binary:

```powershell
python tests/workbench_browser_acceptance.py --require-browser
python tests/workbench_browser_journey.py --require-browser
```

The first gate freezes delayed-response and same-run view isolation against a
same-origin HTTP fixture. The second loads the same production assets but
forwards every product request to a real local `sasori.server` and deterministic
Incident application. It must prove `approval_required → resume_required →
explicit resume → completed`, the external action count `0 → 0 → 1`, the exact
17-event timeline, cold page reload/history reopen, final output, and visible
trusted-process permission disclosure. Its test-only action-count probe is
out-of-band evidence, not a Sasori API. These checks do not replace the
container restart or credentialed provider gates.

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

The repository's `Container product gate` job performs this workflow on
`ubuntu-24.04` after the source test matrix. It builds the `sasori:local`
candidate with `--no-cache --pull`, starts that service through Compose, and runs
the standard-library acceptance driver in three phases:

```powershell
$env:SASORI_TOKEN_FILE = "C:\secure-local-path\sasori-token"
$env:SASORI_PORT = "18888"
if ($IsLinux) {
  chmod 0640 -- $env:SASORI_TOKEN_FILE
  $env:SASORI_TOKEN_GID = (stat -c "%g" -- $env:SASORI_TOKEN_FILE).Trim()
}
docker compose config --quiet
docker compose build --no-cache --pull sasori
docker compose up -d --wait --wait-timeout 120 sasori

$baseUrl = "http://127.0.0.1:$env:SASORI_PORT"
$runId = "container-acceptance-$([guid]::NewGuid().ToString('N'))"
$evidence = Join-Path $env:TEMP "$runId.json"

python scripts/container_acceptance.py prepare `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $evidence --run-id $runId
python scripts/container_acceptance.py complete `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $evidence
docker compose restart sasori
docker compose up -d --wait --wait-timeout 120 sasori
python scripts/container_acceptance.py after-restart `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $evidence
```

`prepare` must stop at `resume_required` with 11 exact semantic events and no
completed `record_action`. `complete` must first prove that prepared durable
state is unchanged, explicitly resume once, then observe 17 exact events, one
exact action, the expected final, and the exact SSE sequence 11-17 tail.
`after-restart` performs only GET/SSE reads and requires the projection, event
and SSE hashes, final, cursor, and effect count to remain unchanged.

When the candidate includes `sasori_memory`, the same installed container image
also runs a deterministic extension smoke against a separate file on the named
data volume: bind one uppercase-valid run ID, append one immutable Memory
revision with an opaque provider-style call ID, retrieve it, restart the
container, and retrieve the same `memory_id`/revision again. CI pipes
`scripts/container_memory_acceptance.py` into the installed container in
`prepare` and `after-restart` phases, retains both strict JSON results on the
host, and cross-checks their identity, revision, collection revision, and active
generation. This proves package inventory and SQLite durability through the
built image; it is not a credentialed provider, personal-Memory,
factual-quality, or multi-tenant test.
Do not enable first-party Research/Developer Memory in this Incident workflow
or introduce a second Loop merely to perform the extension smoke.

The CI job independently snapshots `/data/incident-actions.jsonl` as
`0 -> 1 -> 1`, verifies its single JSON record exactly, and starts a second
container against the same named volume. Only an exact `ConcurrentRunError`
with exit code `2` passes that ownership probe. Before upload it scans the
acceptance evidence, three action snapshots, runtime log, owner log, image SPDX,
native Syft catalog, and image binding for the bearer token. The token and raw
logs are deleted. The seven audited acceptance JSON files (Incident, Memory
prepared, Memory restarted, artifact tamper, and three action snapshots) are
retained for seven days, while the image SPDX, native catalog, and unsigned
binding are uploaded as a separate 30-day artifact. Cleanup deliberately uses `docker compose down
--remove-orphans --timeout 20` without `-v` or `--volumes`.

On native Linux the token file remains host-private at mode `0640`. Its numeric
host GID is passed through `SASORI_TOKEN_GID`, and Compose adds only that
supplemental group to the non-root `10001:10001` process. This is required
because file-backed Compose secrets are bind mounts and cannot apply secret
`uid`/`gid`/`mode` remapping. Never weaken this bridge to a world-readable
token or an inspectable token environment variable.

This job exercises only the deterministic `incident` composition and the
locally built `sasori:local` candidate. It generates an unsigned image SBOM from
that tested candidate, but does not run either credentialed provider smoke,
publish an image, sign an artifact, or create trusted provenance. The presence
of the workflow definition is not a passing public gate: record the hosted run
URL and exact commit only after that run succeeds.

Keep the named data volume during restart/owner testing. Do not publish a public
deployment without the additional TLS, isolation, authentication, limits,
backup, monitoring, and secret-management controls in `SECURITY.md`.

## 6. Publish gate

Before uploading or tagging a release, all of the following must be true:

- Windows and Linux 3.11-3.13 source regression and installed-wheel smoke
  matrices pass;
- the delayed-response and real-server Workbench browser gates pass on the
  final revision;
- OpenAI and Anthropic each pass the real two-turn tool smoke without exposing
  credentials, and any claimed streaming behavior has its own conformance test;
- the no-cache domestic-source Compose workflow and installed-container Memory
  write/search/restart smoke pass on the final revision;
- wheel/sdist manifest, application SBOM, image SBOM, and notices are archived,
  and the signed GitHub build-provenance attestation verifies every relied-upon
  subject against the expected workflow, exact tag ref, and tag commit;
- the actual workflow trigger tag equals `v{project.version}`, and the gated
  eight-file candidate bundle has passed source, package, wheel, sdist, and
  container jobs;
- the working tree is clean and exactly tagged; and
- `catalog/index.json` changes only after hosting, review, and provenance are
  real.

Artifact-signing formats beyond the GitHub/Sigstore provenance statement, PyPI
Trusted Publishing, rollback, and central marketplace publication are not
implemented by the local verifier. Add them only when a real hosting and
maintainer-operating contract exists.
