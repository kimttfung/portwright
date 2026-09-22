---
name: Arm64 per-file MSBuild metadata
status: observed-once
---

# Existing platform configurations can omit per-file metadata

## Observation

A Visual C++ solution declared Debug and Release Arm64 configurations, but its per-file precompiled-header rules covered only Win32 and x64. The Arm64 build therefore reached compilation and failed with missing-PCH and C1010 errors.

## Remediation

Extend the existing per-file `PrecompiledHeader` conditions to the Arm64 configurations. Preserve the project's current `Create` and `NotUsing` split instead of disabling PCH globally.

## Applicability

Use this check when a Visual C++ project already declares Arm64 and fails around PCH creation or use. Inspect both the PCH creator translation unit and every existing per-file exception.

## Exclusions

Do not infer that every Arm64 build failure is a PCH problem. This pattern does not cover projects that generate metadata, use CMake or another build system as the source of truth, or intentionally avoid PCH.

## Evidence standard

The original Arm64 compiler failure must disappear after the bounded metadata change, and the build must advance to the next independent result. One application supports `observed-once`; another checked application is required before marking reuse.
