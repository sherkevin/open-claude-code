# Third-party components

| Component | Version | License | Distribution |
| --- | --- | --- | --- |
| frp / frpc | 0.61.1 | Apache License 2.0 | `frp/` |
| Go standard library | 1.27.4 | BSD 3-Clause | Copied by the build from the pinned Go toolchain into package `compliance/go-LICENSE.txt` and `compliance/go-PATENTS.txt` |

The Go module inventory and the exact binary build metadata are generated for
each release under the package's `compliance/` directory.

`cyclonedx-gomod` cannot infer the standard-library license from the toolchain.
The release build explicitly sets the `std` component to BSD-3-Clause, includes
the pinned Go `LICENSE` and `PATENTS`, and marks the application's own license
as `NOASSERTION` rather than inferring it from unrelated repository files.
