# ADR-0004: Local Git plugin boundary

Status: accepted for the first-party `0.1.0.dev0` slice.

## Decision

`com.sasori.git` is trusted installed Python code that exposes six ordinary
Harness tools. It does not own a loop, event stream, approval store, recovery
protocol, shell, remote transport, or credential store.

- `git_status`, `git_diff`, `git_log`, and `git_show` are read-only.
- `git_stage` and `git_commit` are side-effecting revision `1` tools. They use
  the existing fingerprint-bound approval and `effect_unknown` recovery rules.
- The plugin invokes one resolved local Git executable with fixed argv,
  `shell=False`, no stdin, bounded output, a deadline, a minimal environment,
  disabled credential helpers, disabled hooks, disabled signing, and disabled
  external/textconv diff drivers.
- There are no remote, push, pull, fetch, clone, branch, reset, checkout,
  merge, rebase, amend, signing, hook, arbitrary ref, or arbitrary argv tools.

The manifest requests workspace read/write and the local Git host process.
Those values disclose intent. `trusted_process` still has full OS-user process
privileges; it is not a filesystem, process, network, or credential sandbox.

## Approval snapshot

`git_status` returns a versioned SHA-256 snapshot over:

1. the full current HEAD object ID or the unborn marker;
2. the symbolic branch name or detached marker;
3. `git ls-files --stage -z` index metadata;
4. porcelain status records; and
5. raw content hashes for changed and untracked regular files, or link text for
   symlinks.

`git_stage` and `git_commit` require this snapshot as an ordinary argument, so
it is included in the existing Harness approval fingerprint. They recompute it
before starting a mutating Git command. A mismatch is a known no-mutation
`stale_snapshot` result, not an exception and not a claimed success.

The snapshot prevents an old approval from silently accepting different file
content with the same porcelain status. It is not an external repository lock.
Another local actor can still change the repository between the final check and
Git's own mutation. The plugin therefore makes no exactly-once or adversarial
local-concurrency claim.

## File and credential boundary

Model paths must be explicit relative POSIX file paths. Absolute, drive, UNC,
ADS, parent, empty-segment, `.git`, directory, escaping-link, missing-untracked,
duplicate, and oversized paths are rejected. Every path is passed after `--`
under Git's literal-pathspec mode.

The first slice refuses common credential/key paths for diff, show, and stage,
and refuses commit when such a path is already staged. This is a conservative
filename policy, not content-based secret detection. A secret embedded in an
ordinary source filename can still be shown to the configured model.

Stage refuses paths with a Git clean/process filter. Mutations also refuse an
active merge, rebase, cherry-pick, revert, or unmerged index. This keeps Git
hooks, signing, filters, conflict workflows, and remote credential machinery
outside the reviewed boundary.

## Output and recovery

Git stdout is written to a temporary file while the parent enforces a byte cap
and total command deadline. Stderr, the inherited environment, Git config, and
remote URLs are never returned to the model. Output that is too large fails
closed.

Preflight outcomes such as stale snapshot, sensitive path, unsupported filter,
unsupported repository state, invalid arguments, and no staged change are
explicit normal tool data and never start a Git mutation. Once `git add` or
`git commit` starts, a timeout, cancellation, nonzero exit, or unverifiable
result follows the existing side-effecting `effect_unknown` path. Resume never
automatically repeats an ambiguous mutation; the operator must use the existing
manual effect-resolution API.

## Deferred

Remote operations, arbitrary Git commands, directory/glob stage, ignored files,
LFS/filter execution, sandboxing, repository-wide secret scanning, and
cross-process worktree locking are deferred until a concrete use case supplies
an acceptance test and a stronger authority boundary.
