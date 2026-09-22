---
name: Target-matched native delivery
status: observed-once
---

# Native delivery needs target-matched dependencies and runtimes

## Observation

A project declared Arm64 but referenced a native static library that was absent for the target. Its portable staging script also selected only x86 or x64 outputs and copied runtime files from host architecture directories.

## Remediation

Pin the matching dependency source, build it for the target architecture and compatible CRT, and connect it through target-specific properties. Stage the actual target outputs and the matching redistributable files. Keep operating-system components out of the package unless the platform's deployment rules require them.

## Applicability

Use this check when linking or packaging fails because a native input, helper, addin, worker, or runtime is missing or selected from another architecture. Verify provenance, machine type, ABI or CRT compatibility, and runtime behavior.

## Exclusions

Do not rebuild generated libraries that already have a documented target build step without first using that step. Do not copy arbitrary files from system directories. A valid PE header alone does not prove that a component loads in the intended process.

## Evidence standard

Record source identity, build command, output hash and architecture for rebuilt dependencies. The staged manifest must bind every required component to a hash and role. Independent verification must check the package contents, launch from the staged location, complete the declared task, and clean up its processes and fixtures.
