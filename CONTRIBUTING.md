# Contributing

Contributions should preserve Portwright's route-first and evidence-driven design.

Before submitting a change:

1. Keep reusable code and agent instructions application-neutral.
2. Do not weaken approval, source-scope, artifact-identity, or Examiner boundaries.
3. Add or update focused tests for behavior changes.
4. Run:

   ```powershell
   python -m unittest discover -s tests -v
   ```

5. Describe the problem, the smallest justified change, and the evidence used to validate it.

Application-specific commands, patches, and claims belong in case studies or local run records. A reusable pattern should state its applicability, exclusions, and evidence standard.
