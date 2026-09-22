# Security

Portwright runs local build tools and agent-guided engineering tasks. Treat repository content, build scripts, package hooks, and generated commands as untrusted until reviewed and approved.

Report security issues privately through the repository's GitHub security advisory feature rather than a public issue.

When investigating a report:

- do not include credentials, personal application data, or unrelated files;
- preserve exact source, run, task, and artifact identities;
- use synthetic fixtures and isolated application profiles;
- do not bypass operating-system, browser, package, or trust warnings;
- do not broaden execution or source-edit permissions without a new approval.
