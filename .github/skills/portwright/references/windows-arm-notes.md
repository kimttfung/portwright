# Windows Arm constraints

Reference notes, consulted on 21 September 2026. These describe platform constraints, not a successful Portwright migration.

## Native boundaries

Ordinary Arm64 processes load Arm64 code, not ordinary x64 or Arm64EC DLLs. Arm64EC uses an x64-compatible ABI and can interoperate with x64 code on Windows 11 on Arm. Arm64X can contain compatible views for both. Choose a route from the application's real dependency boundaries; a native launcher does not establish that every worker is native.

Use standard tools such as `link /dump /headers` when an artifact inspection is authorized. Check the complete header interpretation, required libraries, and runtime behavior rather than classifying by filename.

Source: https://learn.microsoft.com/en-us/windows/arm/arm64ec

## MFC, ATL, and installation

For the inspected Visual Studio 2022 catalog, the Arm64 component identifiers are `Microsoft.VisualStudio.Component.VC.MFC.ARM64` and `Microsoft.VisualStudio.Component.VC.ATL.ARM64`. The MFC component declares ATL and MFC runtime redistribution dependencies. Compiler presence does not establish that these components are installed.

Inspect the selected installation, headers, target libraries, SDK, and runtime source. A different toolset or a future catalog needs a new check. Do not install unrelated workloads.

Microsoft documents that standard users cannot invoke the installer's `--quiet` or `--passive` modes programmatically, regardless of `AllowStandardUserControl`. `--norestart` is paired with one of those modes. `--wait` belongs to the bootstrapper, not `setup.exe`. Do not work around an unavailable administrator token or managed policy.

Sources:

- https://learn.microsoft.com/en-us/visualstudio/install/workload-component-id-vs-build-tools?view=vs-2022
- https://learn.microsoft.com/en-us/visualstudio/install/use-command-line-parameters-to-install-visual-studio?view=vs-2022
- Record installed tool identities in the current run rather than treating a tool's presence as a successful application build.

## Command-line toolchain environment

MSVC command-line tools depend on architecture-specific environment variables, including `PATH`, `INCLUDE`, `LIB`, and `LIBPATH`. Microsoft recommends using a Developer Command Prompt or one of the installed Visual Studio command files instead of reconstructing these variables manually.

Keep environment initialization and the native build in the same `cmd.exe` process. For a native ARM64 toolchain, use the installed ARM64 command file. If a wrapper such as node-gyp stalls while discovering Visual Studio, retry once from that explicit environment before treating discovery as blocked.

Source: https://learn.microsoft.com/cpp/build/building-on-the-command-line?view=msvc-170

## Runtime deployment

Use Microsoft's signed Visual C++ Redistributable for the target, or permitted unmodified app-local files from the matching Visual Studio redistribution directory. Microsoft's redistribution list is subject to the applicable license terms; file presence or a valid signature does not independently establish the user's redistribution entitlement.

Do not source release payloads from the build host's Windows system directories or distribute `debug_nonredist` files. Match the supported runtime version to the build toolset and inspect the actual target payload. Installer availability does not prove a staged application launches.

Sources:

- https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170
- https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution
