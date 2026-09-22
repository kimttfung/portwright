---
name: Windows native build environment
status: observed-once
---

# Windows native builds need a local path and explicit toolchain environment

## Observation

A native addon rebuild stalled in a cloud-synced checkout whose path contained spaces. Mapping the same directory to a short drive letter did not fix every addon because one tool resolved the physical path. After moving the pinned checkout outside the synced directory, node-gyp still stalled while discovering Visual Studio automatically.

A separate command failed before testing because an unquoted `cmd.exe` environment assignment retained the space before `&&`, turning `never` into `never `.

## Remediation

Use a clean short local worktree at the same pinned revision when native package tools encounter path or hydration problems. Run the target-specific Visual Studio command file and the dependent build command in the same `cmd.exe` process. Use quoted command-shell assignments:

```cmd
set "NAME=value"
```

For an ARM64 native dependency build, initialize the installed ARM64 Visual Studio environment before invoking the package or build tool.

## Applicability

Use this pattern for node-gyp, Electron native addons, MSBuild wrappers, CMake generators, or other Windows native tools that hang during toolchain discovery or warn about a spaced or cloud-backed path.

## Exclusions

A path containing spaces or a cloud-sync provider does not prove that a build will fail. Do not move a checkout after a run is bound without starting a new run or explicitly recording the replacement checkout. Do not copy Visual Studio environment variables from another machine.

## Evidence standard

Retain the stalled or malformed-command log. Bind the replacement worktree to the same revision. Record the Visual Studio command file, target architecture, corrected command, successful native build, and resulting artifact identity.
