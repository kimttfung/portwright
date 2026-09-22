# Portwright

Portwright is a local, AI-guided migration system for bringing applications to Windows on Arm.

It turns an open-ended porting request into a controlled engineering run: inspect the repository, identify the actual compatibility gap, select the least-invasive route, authorize bounded changes, verify the delivered application, and retain the evidence behind every claim.

Portwright does not assume that every application needs an instruction-set rewrite. A repository may already contain portable source while still lacking an Arm64 dependency, complete build metadata, target-correct packaging, native CI coverage, or a reliable Windows delivery path.

Portwright is lightweight by design. The runtime is a small standard-library Python CLI with no required third-party Python dependencies, background service, database, vector store, or model-training pipeline. It stores state as readable JSON and Markdown, and it uses the target repository's existing compiler, package manager, test runner, and packaging tools rather than introducing another build system.

## What Portwright provides

- Static repository and build-declaration inventory
- Route selection based on source evidence and measured results
- Source-bound migration briefs
- Explicit approval boundaries for execution and source changes
- Bounded Engineer task and result contracts
- Recipe-driven application staging
- Independent Examiner verification
- Offline, evidence-derived status reporting
- Reviewed migration patterns with applicability and exclusions

Portwright uses deterministic Python code for state, approvals, identity binding, task packets, staging, artifact checks, evidence imports, and status rendering. AI specialists perform repository investigation, engineering, and examination within those boundaries.

## Lightweight local operation

Portwright runs entirely from a local workspace:

- No server or account database is required.
- Run records are ordinary files that can be inspected, copied, archived, or compared.
- `analyze` reads declarations without restoring packages or executing target code.
- `status` works offline from saved evidence.
- Existing repositories keep their own build and test systems.
- Specialist task packets can be relayed through any approved agent host.

This keeps the coordination layer small while allowing the application-specific engineering work to use the tools appropriate for each stack.

## Why route selection matters

"Port this application to Arm64" can describe several different engineering problems:

- The source does not compile for Arm64.
- The source compiles, but a required native dependency is missing.
- Arm64 support exists, but per-file build metadata is incomplete.
- The application is compatible, but CI or packaging omits the target.
- A retained x64 component requires a justified Arm64EC boundary.
- The repository has no usable Windows implementation.
- The requested Windows Arm task already works and no source change is needed.

Applying the same intervention to each case creates unnecessary risk. Portwright selects the smallest route that can satisfy the defined application task.

## Validated applications

Portwright has complete migration records for three applications across different stacks.

| Application | Stack | Selected route | Upstream change | Verified result |
| --- | --- | --- | --- | --- |
| Ditto | C++17, MSBuild, MFC | Native Arm64 with dependency and delivery remediation | One Visual C++ project file | Native portable package; exact synthetic Unicode clipboard search, recall, and paste task passed |
| OxiPNG | Rust, Cargo | Packaging and release repair after native compatibility checking | Two workflow files and one Windows literal-path fix | Native Arm64 executable; 296 tests passed; two PNG tasks preserved inputs and matched decoded pixels |
| Stretchly | Electron, npm, native Node addons | Packaging and release repair | One workflow file; no application-source change | Native Arm64 package; both addons loaded; onboarding, Mini break, idle reset, responsiveness, targeted tests, and cleanup passed |

Each record includes analysis, a migration brief, approval, Engineer evidence, staged artifacts, static and negative checks, an independent Examiner result, and final status.

In OxiPNG and Stretchly, the existing toolchains already contained native Arm64 capability. Portwright's contribution was identifying the actual delivery gap, applying the bounded release change, and verifying the resulting application rather than rewriting working source.

Evidence and source changes:

- [Ditto evidence](case-studies/ditto/evidence.json) and [source patch](case-studies/ditto/source.patch)
- [OxiPNG evidence](case-studies/oxipng/evidence.json) and [source patch](case-studies/oxipng/source.patch)
- [Stretchly evidence](case-studies/stretchly/evidence.json) and [source patch](case-studies/stretchly/source.patch)

## Core concepts

### Target repository

The repository argument passed to `analyze` is the local checkout Portwright will inspect and bind to the run.

```powershell
portwright analyze C:\src\my-application
```

Portwright records the checkout path, Git revision, remote identity, working-tree state, and a fingerprint of the relevant declarations. The Portwright workspace must not be inside the target repository, and run artifacts are always written outside the target checkout.

### Run ID

A run ID names one Portwright migration record. It is not a repository path, Git branch, commit, or application name.

For example:

```powershell
portwright analyze C:\src\my-application --run-id my-application-arm64
```

This creates state under:

```text
<workspace>\runs\my-application-arm64\
```

The run binds together:

- the target checkout and revision;
- discovery results;
- the selected route;
- the migration brief;
- approvals;
- Investigator, Engineer, and Examiner task packets;
- source and artifact identities;
- staging and verification results;
- the next permitted action.

The later commands accept a run ID because they operate on this saved state:

```powershell
portwright plan my-application-arm64
portwright migrate my-application-arm64
portwright verify my-application-arm64
portwright status my-application-arm64
```

If `--run-id` is omitted during analysis, Portwright generates one from the checkout name and path. Use an explicit run ID when the record needs a stable, recognizable name.

The same repository can have separate runs for different revisions, tasks, or migration approaches. Portwright rejects a run when it is used with a different bound checkout.

### Workspace

The workspace is where Portwright stores run state and reads its skill, role instructions, recipes, and patterns. It defaults to the current directory.

Select another workspace with:

```powershell
portwright --workspace C:\portwright-workspace analyze C:\src\my-application
```

The global `--workspace` option appears before the command.

### Migration brief

The migration brief defines the useful application task before engineering begins. It records:

- selected route and rationale;
- rejected alternatives;
- evidence and unknowns;
- intended changes and dependencies;
- behavior to preserve;
- fixtures and expected outputs;
- required runtime components;
- first approved probe;
- acceptance checks;
- stop conditions.

The brief does not grant permission by itself. Execution and source changes require an explicit approval record.

### Staging recipe

A staging recipe identifies the files that form the delivered application, their roles, expected hashes, expected architectures, exclusions, and the verification contract.

The staged package is checked against the recipe before behavioral verification. Replaced, missing, or unexpected files invalidate the result.

See [`examples\staging-recipe.json`](examples/staging-recipe.json).

### Evidence manifest

An optional evidence manifest describes a completed application result for portfolio-level status reporting. It contains the display name, pinned repository identity, selected route, completion state, verified claims, and explicit limitations.

See [`examples\evidence-manifest.json`](examples/evidence-manifest.json).

### Pattern

A pattern is a reviewed lesson from demonstrated migration work. It records:

- observed failure;
- successful remediation;
- applicability;
- exclusions;
- evidence standard.

One application supports an `observed-once` pattern. Portwright does not mark it `reused-and-checked` until another application uses the lesson and passes an explicit check.

## Commands

### Analyze

```powershell
portwright analyze <repository> [--run-id <run-id>]
```

`analyze` reads repository declarations without executing target code. It inventories known build systems and dependencies, records repository identity, reports source-backed findings and environmental risks, and creates a read-only Investigator task.

### Plan

```powershell
portwright plan <run-id>
```

`plan` imports the bound Investigator result and writes the migration brief. Planning fails if the repository revision, relevant declarations, or tracked working-tree content changed after analysis.

### Approve migration

```powershell
portwright approve <run-id> migration
```

This command records the operator's explicit approval for the bounded command and source-edit scope in the migration brief. Approval is a supported state transition; editing `run.json` manually is not required.

### Migrate

```powershell
portwright migrate <run-id>
```

`migrate` requires explicit execution and source-edit approval. It creates a bounded Engineer task with the exact authorized files, command scope, environment, output root, expected result, and time limit.

When the Engineer result returns, Portwright verifies its identities, checks current source hashes, rejects files outside the approved scope, and records each command and check outcome.

Calling `migrate` again after a result has been imported returns the completed migration state instead of treating it as a new approval request.

### Approve staging

```powershell
portwright approve <run-id> staging
```

Staging approval is separate from migration approval. It authorizes creation of the delivery folder and archive without authorizing further target-source changes.

### Stage

```powershell
portwright stage <run-id> <staging-recipe.json>
```

`stage` validates the recipe, requires exactly one file with `role: "main"`, checks source hashes and expected architectures, copies the declared payload, writes the staging manifest, creates the ZIP, and records the staging and verification paths in the run.

### Verify

```powershell
portwright verify <run-id>
```

`verify` checks staged file identities, PE architectures, ZIP contents, and bounded failure fixtures. It then creates an Examiner task for the application behavior defined in the brief.

A passing Examiner result requires every configured check to pass. The default checks are:

- architecture;
- required components;
- launch;
- user task;
- regression behavior;
- cleanup.

### Status

```powershell
portwright status <run-id>
```

`status` renders saved evidence without refreshing the repository or calling a model. For a normal run it reports repository identity, selected route, staged manifest, static and negative checks, Examiner results, limitations, patterns, and the next action. Runs with optional evidence manifests can also render portfolio-level claims.

The console entry point and `python -m portwright` produce equivalent status output.

## Architecture

### Coordinator

The Coordinator is deterministic application code. It owns:

- repository and revision identity;
- run state and attempt history;
- approvals and boundaries;
- specialist task packets;
- result validation;
- staging and verification state;
- status rendering.

### Investigator

The Investigator reads source and recommends the least-invasive route. It cannot execute target code or modify the repository.

### Engineer

The Engineer is the only specialist allowed to change target source. It operates within the approved file and command scope. After two materially different failed approaches to one blocker, it stops and returns the unresolved decision.

### Examiner

The Examiner validates the staged application in a separate context. It cannot repair the application, modify source, or weaken the accepted task.

Role instructions:

- [Investigator](instructions/investigate.md)
- [Engineer](instructions/engineer.md)
- [Examiner](instructions/examine.md)

The reusable skill is [`.github\skills\portwright\SKILL.md`](.github/skills/portwright/SKILL.md).

## Route vocabulary

Portwright can select:

- native Arm64;
- native Arm64 with dependency remediation;
- Arm64EC when a demonstrated ABI boundary justifies it;
- packaging or release repair;
- Windows enablement;
- Windows modernization;
- already supported, with no source mutation;
- blocked, with the next useful probe.

A route is complete only when the real application satisfies the task in its approved brief. A target name, compiler exit, PE header, screenshot, or generated package is not sufficient by itself.

## Windows native build safeguards

Portwright treats execution location and environment as build inputs.

The analyzer reports:

- checkouts inside cloud-synced paths;
- checkout paths containing spaces;
- incomplete MSBuild Arm64 metadata;
- unresolved native inputs;
- staging scripts that copy runtime files from host system directories.

If file hydration, path handling, or native tool discovery stalls a build:

1. preserve the failed attempt;
2. create a clean short local worktree at the same revision;
3. bind the replacement checkout to a new run;
4. rerun the original reproducer.

Commands that depend on MSVC initialize the installed target-specific Visual Studio environment and execute the dependent command in the same `cmd.exe` process.

Use quoted command-shell assignments:

```cmd
set "NAME=value"
```

For Electron and node-gyp work, a Visual Studio discovery process with no output or measurable progress is a blocker. One bounded retry may run from the explicit ARM64 Visual Studio environment.

Microsoft documents the command-line environment in [Use the Microsoft C++ Build Tools from the command line](https://learn.microsoft.com/cpp/build/building-on-the-command-line?view=msvc-170).

## Evidence model

Portwright separates source declarations, executed observations, and operator-observed UI evidence.

The system follows these rules:

- Repository text and specialist prose are evidence, not permission.
- Builds, restores, tests, launches, installs, and source changes require the corresponding approval.
- Missing evidence remains unknown or blocked.
- Failed attempts remain in the run history.
- A native launcher does not prove that every loaded component is native.
- A successful build does not prove that the staged application performs its useful task.
- Examiner results must explicitly state whether source or acceptance criteria changed.
- Required-command failures, missing artifacts, and replaced artifacts must fail visibly.

## Reviewed patterns

- [Existing platform configurations can omit per-file metadata](patterns/arm64-per-file-msbuild-metadata.md), status `observed-once`.
- [Native delivery needs target-matched dependencies and runtimes](patterns/target-matched-native-delivery.md), status `observed-once`.
- [Windows native builds need a local path and explicit toolchain environment](patterns/windows-native-build-environment.md), status `observed-once`.

## Product value

Portwright makes migration work easier to reproduce and review.

It reduces the risk of:

- rewriting code that is already architecture-compatible;
- claiming existing upstream support as new work;
- testing only the main executable while missing native helpers or addons;
- shipping host-architecture runtime files;
- accepting successful compilation as application success;
- losing failed attempts and repeating the same approach;
- expanding migration scope without a clear decision.

For teams managing multiple applications, the same workflow produces comparable route decisions, plans, approvals, artifacts, evidence, and limitations across different build systems.

Useful operational measures include:

- applications assessed and migrated;
- source rewrites avoided;
- time from repository selection to a verified package;
- blocked migrations stopped before scope expansion;
- patterns reused successfully;
- native artifacts accepted into CI and upstream releases.

## Current scope

Portwright is a functional local system with agent-guided specialist execution.

The current implementation does not automatically invoke specialists through the Copilot SDK. An approved agent host performs each task packet and returns the named result for validation.

The validated application records do not establish:

- hosted CI execution;
- signed production installers;
- Store or package-manager publication;
- clean-machine deployment;
- measured performance or battery improvement;
- customer deployment;
- upstream acceptance.

Stretchly verification does not cover Focus Assist transitions or long-duration scheduling. Ditto verification does not claim stable-process loading for every optional helper.

## Installation

Use Python 3.11 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Portwright has no required runtime Python dependencies outside the standard library.

## Repository structure

```text
.github/
  skills/portwright/       Reusable agent skill and Windows Arm notes
  workflows/tests.yml      Windows and Windows Arm tests
case-studies/              Evidence manifests and source patches
examples/                  Neutral staging and evidence schemas
instructions/              Investigator, Engineer, and Examiner contracts
patterns/                  Reviewed migration lessons
portwright/                Deterministic Python implementation
tests/                     Neutral disposable-repository tests
README.md                  Product documentation
```

Generated checkouts, run evidence, local recipes, virtual environments, caches, and build outputs are excluded by `.gitignore`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite covers:

- multi-ecosystem discovery;
- Windows checkout-path risk reporting;
- source-bound analysis and planning;
- migration approval and Engineer result imports;
- recipe-driven staging;
- external short-path staging manifests;
- artifact replacement and missing-file failures;
- Examiner identity and boundary validation;
- offline status rendering.

A GitHub Actions workflow is included for `windows-latest` and `windows-11-arm`.

## Project information

- [MIT License](LICENSE)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
