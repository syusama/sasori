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
$wheels = @(Get-ChildItem -LiteralPath $output -Filter *.whl)
if ($wheels.Count -ne 1) { throw "expected exactly one built wheel" }
python scripts/repack_wheel.py --wheel $wheels[0].FullName
if ($LASTEXITCODE -ne 0) { throw "wheel repack failed" }
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
Wheel members must use only Deflate or BZIP2 compression, carry no archive
comment, encryption, or data-descriptor flag, and place `.dist-info` physically
after payload members. It writes an application-artifact SPDX 2.3 JSON SBOM,
artifact manifest, and unsigned local provenance record:

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

The accepted W1.2 implementation snapshot is bound to
[`e3bc816`](https://github.com/syusama/sasori/commit/e3bc816c9d33febcc364e595a7480b475d181efb)
and [Hosted run 31391700342](https://github.com/syusama/sasori/actions/runs/31391700342).
Its exact wheel was 252,158 bytes under the strict 250 KiB (256,000-byte)
ceiling, leaving 3,842 bytes of nominal headroom; its manifest recorded
`sasori-release-verify` v11 and `sasori-source-tree-v8`. This is dated evidence
for that exact artifact, not a promise that later wheels remain below the
limit, and it is not a reason to raise the limit. Run `31391700342` was an
ordinary non-tag `main` run: all 20 non-tag jobs passed and the exact-tag-only
release-candidate job was skipped. It therefore produced no exact-tag bundle,
GitHub-hosted signed attestation or trusted provenance, GitHub Release, PyPI
publication, or published container image.

The W1.3 implementation candidate changes the verifier inventory because
ADR-0017 becomes an explicit release/source-tree input. Its local verifier is
therefore `sasori-release-verify` v12 with `sasori-source-tree-v9`; the strict
wheel threshold remains unchanged. After the normal locked build,
`scripts/repack_wheel.py` deterministically chooses the smaller of Deflate 9 and
BZIP2 9 for each member without changing its path, extracted bytes, permission,
timestamp, extra metadata, or `RECORD` bytes; `.dist-info` is written last and a
second repack must be byte-idempotent. The rebuilt-sdist wheel uses the same
repacker before verification and installation.

The 2026-08-11 exact-current-source local candidate built through the
digest-pinned DaoCloud Python base, Tsinghua index, and hash-locked build
wheelhouse. Its ordinary build wheel was 258,582 bytes; the canonical repack was
243,357 bytes with 37 Deflate-9 members and 32 BZIP2-9 members. A second repack
was byte-idempotent.
Verifier v12 accepted the wheel and sdist while returning the required dirty
local exit `5` and `release_eligible=false`. A clean normal-pip consumer passed,
and the locked sdist consumer produced a 243,366-byte canonical wheel, ran the
same verifier, installed it into a separate environment, and passed the saved
Catalog smoke. The exact-current-code mainland-source container fresh-volume
journey, 471-test suite with five skips, 29-case Chrome fixture, and three real
browser journeys also passed locally.

This is not W1.3 Hosted artifact evidence. The exact implementation commit must
still pass the Linux/Windows Python 3.11/3.12/3.13 source, original-wheel, and
rebuilt-sdist matrices plus browser and mainland-source container jobs. Do not
reuse the W1.2 run URL for W1.3, and do not raise the wheel limit. These local
figures are not an exact-tag bundle, TestPyPI/PyPI upload/download/install,
attestation, publication, or release claim.

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
`sasori_memory` extension), all allowlisted Workbench resources, and all three
console scripts. The installed-wheel smoke also creates a separate Memory
database, writes one immutable revision, and retrieves it through the installed
package; it does not import the source checkout. W1.3 also creates, CAS-updates,
reopens, and reads the independent saved Workflow catalog from the installed
distribution.

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

The supported distribution path is normal pip extraction and installation.
Other wheel installers require separate evidence and are not part of the
current compatibility claim. Sasori does not promise that adding the wheel
archive directly to `sys.path` will work as `zipimport`, and generic ZIP tools
that lack BZIP2-member support are outside the installation contract. The
original and rebuilt-sdist wheels must both prove BZIP2 acceptance in the full
Python 3.11/3.12/3.13 by Linux/Windows Hosted matrix. Before a formal tag, an
upload/download/install round trip through TestPyPI is also required; a local
pip success is not publication evidence.

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

The first gate executes the bundled assets against a same-origin fixture. It
freezes delayed/stale response isolation and the Studio's exact success,
controlled rejection, malformed-envelope, invalid-Unicode, keyboard/focus,
desktop, 390×844 narrow, and reduced-motion behavior. The W1.3 fixture also
covers 29 cases including stable pagination, browser canonical SHA-256 binding,
saved create, stale edit, exact `412` conflict, exact non-retryable 504 and
malformed-success GET reconciliation, three kinds of late record-switch result,
and no automatic mutation retry. The second runs three journeys while forwarding
every product request to a real local `sasori.server`: Studio
preflight plus an explicit saved revision 1 must create no run or action, then
independent Incident and typed Workflow
journeys must pass `approval_required → resume_required → explicit resume →
completed`, artifact, cold-reopen, and no-replay checks. Its test-only
action-count probe is out-of-band evidence, not a Sasori API. These checks do
not establish mobile-device/cross-browser certification and do not replace the
container restart or credentialed provider gates.

Run a no-cache Compose build through the configured mainland sources, then the
real workflow rather than health-only checks:

```text
POST /v1/workflows/preflight
  -> unchanged run/event/action ledger
  -> PUT saved Workflow revision 1 (If-None-Match: *)
  -> PUT saved Workflow revision 2 (If-Match: exact ETag)
  -> stale writer rejected with 412
  -> current and historical saved revisions readable
  -> unchanged run/event/action ledger
  -> POST /v1/runs
  -> approval_required
  -> durable approval
  -> resume_required
  -> explicit resume
  -> completed
  -> SSE reconnect from the durable cursor
  -> container restart
  -> saved head/history unchanged
  -> unchanged final and effect count
  -> second run and Workflow-catalog owners rejected
```

The repository's `Container product gate` job performs this workflow on
`ubuntu-24.04` after the source test matrix. It builds the `sasori:local`
candidate with `--no-cache --pull`, starts that service through Compose, and runs
the standard-library Incident driver in three phases and the typed Workflow
driver in four phases (`preflight`, `prepare`, `complete`, `after-restart`):

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
$workflowRunId = "workflow-$([guid]::NewGuid().ToString('N'))"
$workflowEvidence = Join-Path $env:TEMP "$workflowRunId.json"
$savedWorkflowEvidence = Join-Path $env:TEMP "saved-$workflowRunId.json"

python scripts/container_acceptance.py prepare `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $evidence --run-id $runId
python scripts/container_acceptance.py complete `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $evidence

python scripts/container_saved_workflow_acceptance.py prepare `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $savedWorkflowEvidence

python scripts/container_workflow_acceptance.py preflight `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $workflowEvidence
python scripts/container_workflow_acceptance.py prepare `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $workflowEvidence --run-id $workflowRunId
python scripts/container_workflow_acceptance.py complete `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $workflowEvidence

docker compose restart sasori
docker compose up -d --wait --wait-timeout 120 sasori
python scripts/container_acceptance.py after-restart `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $evidence
python scripts/container_workflow_acceptance.py after-restart `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $workflowEvidence
python scripts/container_saved_workflow_acceptance.py after-restart `
  --base-url $baseUrl --token-file $env:SASORI_TOKEN_FILE `
  --evidence $savedWorkflowEvidence
```

The Workflow `preflight` reconstructs the strict definition from the catalog,
checks an exact successful manifest and a deliberate Tool-schema-drift
rejection, and requires before/after run and event snapshots to be identical.
The CI action snapshots before and after this phase must also be byte-identical.
It therefore proves static contract inspection and zero mutation, not a dry
run or Tool-effect truth. Workflow `prepare` stops at `resume_required` without
the side effect; `complete` explicitly resumes and observes exactly 17 events
and one Workflow action; `after-restart` repeats read/preflight identity checks
and rejects any replay. This remains step-boundary recovery, not exactly-once
execution.

The saved Workflow driver is a different zero-execution path. `prepare` creates
revision 1 with `If-None-Match: *`, updates to revision 2 with the exact r1 ETag,
requires a stale r1 writer to return `412`, and reads both the current head and
historical revision 1. `after-restart` requires those same identities, digests,
ETags, list summary, and `current_contract` verdicts. Run/event counts remain
zero, and CI takes byte-identical external action snapshots immediately before
and after saved authoring. The driver never calls `/v1/runs`.

Incident `prepare` must stop at `resume_required` with 11 exact semantic events
and no completed `record_action`. `complete` must first prove that prepared durable
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

The CI job independently snapshots `/data/incident-actions.jsonl` across the
Incident and Workflow boundaries, verifies exact per-composition counts and
digests, and starts a second container against the same named volume. Only an
exact `ConcurrentRunError` for the run database and
`WorkflowCatalogConfigurationError` for the saved Workflow database, followed
by the probe's exit code `2`, pass that ownership check.
Before upload it scans the acceptance evidence, action snapshots, runtime log,
owner log, image SPDX, native Syft catalog, and image binding for the bearer
token. The token and raw logs are deleted. Eighteen audited JSON files are
retained for seven days: Incident evidence; Workflow completed/restarted;
saved Workflow completed/restarted plus its before/after action snapshots;
Memory prepared/restarted; artifact tamper; three Incident action snapshots;
and five executable-Workflow action snapshots spanning before-preflight,
preflight, paused, completed, and restarted. The image SPDX, native catalog,
and unsigned
binding are uploaded as a separate three-file, 30-day artifact. Cleanup
deliberately uses `docker compose down
--remove-orphans --timeout 20` without `-v` or `--volumes`.

On native Linux the token file remains host-private at mode `0640`. Its numeric
host GID is passed through `SASORI_TOKEN_GID`, and Compose adds only that
supplemental group to the non-root `10001:10001` process. This is required
because file-backed Compose secrets are bind mounts and cannot apply secret
`uid`/`gid`/`mode` remapping. Never weaken this bridge to a world-readable
token or an inspectable token environment variable.

This job exercises the deterministic `incident` composition, first-party typed
Incident Workflow, zero-execution Workflow preflight and saved authoring
catalog, installed-container
Memory smoke, artifact tamper rejection, second-owner exclusion, and the locally
built `sasori:local` candidate. It generates an unsigned image SBOM from that
tested candidate, but does not run either credentialed provider smoke,
Research/Developer external capabilities, publish an image, sign an artifact,
or create trusted provenance. A workflow definition or successful local run is
not a passing public gate: record the Hosted run URL and exact commit only after
that exact revision succeeds.

Keep the named data volume during restart/owner testing. Do not publish a public
deployment without the additional TLS, isolation, authentication, limits,
backup, monitoring, and secret-management controls in `SECURITY.md`.

## 6. Publish gate

Before uploading or tagging a release, all of the following must be true:

- Windows and Linux 3.11-3.13 source regression and installed-wheel smoke
  matrices pass;
- the delayed-response and real-server Workbench browser gates pass on the
  final revision;
- saved Workflow unit/HTTP/browser/container gates prove immutable history,
  strong-ETag CAS, stale-writer refusal, crash/restart recovery, current-Tool
  drift, unchanged runtime/action authorities, and both database owner locks;
- OpenAI and Anthropic each pass the real two-turn tool smoke without exposing
  credentials, and any claimed streaming behavior has its own conformance test;
- the no-cache domestic-source Compose workflow and installed-container Memory
  write/search/restart smoke plus saved Workflow create/update/history/restart
  smoke pass on the final revision;
- the final exact wheel remains strictly below 256,000 bytes without changing
  the verifier threshold, and both installed-wheel and rebuilt-sdist consumers
  execute the saved Catalog smoke outside the source checkout;
- the final exact original and rebuilt-sdist wheels pass pip installation on
  Python 3.11-3.13 across Linux and Windows, and the exact candidate completes a
  TestPyPI upload/download/install round trip before the formal tag;
- wheel/sdist manifest, application SBOM, image SBOM, and notices are archived,
  and the signed GitHub build-provenance attestation verifies every relied-upon
  subject against the expected workflow, exact tag ref, and tag commit;
- the actual workflow trigger tag equals `v{project.version}`, and the gated
  eight-file candidate bundle has passed source, package, wheel, sdist, and
  container jobs;
- the working tree is clean and exactly tagged; and
- the exact implementation commit and any later documentation-promotion commit
  have each passed their own Hosted non-tag gates before the exact tag is
  created; and
- `catalog/index.json` changes only after hosting, review, and provenance are
  real.

Artifact-signing formats beyond the GitHub/Sigstore provenance statement, PyPI
Trusted Publishing, rollback, and central marketplace publication are not
implemented by the local verifier. Add them only when a real hosting and
maintainer-operating contract exists.
