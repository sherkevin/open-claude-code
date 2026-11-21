# Third-party components

| Component | Version | License | Distribution |
| --- | --- | --- | --- |
| frp / frpc | 0.61.1 | Apache License 2.0 | `frp/` |
| Model Context Protocol Go SDK | 1.8.0 | Apache-2.0 / MIT transition; documentation contributions CC-BY-4.0 | `mcp-go-sdk/LICENSE` |
| jsonschema-go | 0.4.3 | MIT | `go-deps/github.com/google/jsonschema-go@v0.4.3/` |
| segmentio/asm | 1.1.3 | MIT | `go-deps/github.com/segmentio/asm@v1.1.3/` |
| segmentio/encoding | 0.5.4 | MIT | `go-deps/github.com/segmentio/encoding@v0.5.4/` |
| uritemplate/v3 | 3.0.2 | BSD 3-Clause | `go-deps/github.com/yosida95/uritemplate/v3@v3.0.2/` |
| golang.org/x/oauth2 | 0.35.0 | BSD 3-Clause | `go-deps/golang.org/x/oauth2@v0.35.0/` |
| golang.org/x/sync | 0.20.0 | BSD 3-Clause | `go-deps/golang.org/x/sync@v0.20.0/` |
| golang.org/x/time | 0.15.0 | BSD 3-Clause | `go-deps/golang.org/x/time@v0.15.0/` |
| golang.org/x/sys | 0.41.0 | BSD 3-Clause | `go-deps/golang.org/x/sys@v0.41.0/` |
| Go standard library | 1.27.4 | BSD 3-Clause | Copied by the build from the pinned Go toolchain into package `compliance/go-LICENSE.txt` and `compliance/go-PATENTS.txt` |

The Go module inventory and the exact binary build metadata are generated for
each release under the package's `compliance/` directory.

The SDK license text is preserved verbatim. Version 1.8.0 is in the project's
MIT-to-Apache-2.0 transition, so the file includes Apache-2.0, MIT and
CC-BY-4.0 documentation terms. It must not be summarized as MIT-only.

The transitive Go module license and patent files are copied verbatim under
`compliance/go-deps/` for both Linux and Windows packages.

`cyclonedx-gomod` cannot infer the standard-library license from the toolchain.
The release build explicitly sets the `std` component to BSD-3-Clause, includes
the pinned Go `LICENSE` and `PATENTS`, and marks the application's own license
as `NOASSERTION` rather than inferring it from unrelated repository files.
