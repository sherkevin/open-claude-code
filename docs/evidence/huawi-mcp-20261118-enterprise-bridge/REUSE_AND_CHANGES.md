# Reuse list and necessary changes

## Reused without replacement

| Capability | Existing implementation |
| --- | --- |
| Windows install/service/update | `connector/packaging/windows`, `connector/cmd/connector/service_windows.go` |
| Local UI and management guard | `connector/internal/web` |
| MCP import and source identity | `connector/internal/config`, `connector/internal/agent` |
| Customer Bearer and HMAC headers | `connector/internal/huaweiauth`, `connector/internal/authproxy` |
| STCP process supervision | `connector/internal/frpc`, `connector/internal/supervisor` |
| Control-plane enrollment/signature/heartbeat/revoke | `server/app/kaiwu_control.py` |
| Platform MCP JSON parser | `server/app/mcp_import.py` |
| Platform MCP transport | official Python `mcp` SDK already used by the server |
| Tunnel | official fatedier/frp 0.61.1 binaries |

## Necessary additions or corrections

| Need | Change |
| --- | --- |
| Enterprise credential lifecycle | Integration tables, create/list/get/rotate/revoke admin endpoints and one-time K JSON |
| Complete public data plane | FastAPI Edge with K auth and session binding; dedicated frps/visitor generator and supervisor |
| Safe source routing | Database visitor route allocation and Edge host/port policy |
| Correct SSE | Incremental authproxy event forwarding and single-payload multi-line data handling |
| Customer protocol compatibility | Fixed offline HMAC vector and removal of undocumented key-length rejection |
| Windows secret protection | machine-scoped DPAPI JSON store, ACL hard failure, backup and migration test |
| Current product wording | Connector README/UI/docs and historical Plan link updated to enterprise-K scope |
| Evidence | Synthetic real-frps/visitor/connector/Edge test and T01-T13 acceptance matrix |

## Deliberately not added

- No custom tunnel, heartbeat or reconnect protocol.
- No frp core change or HTTP authentication plugin in this one-customer scope.
- No employee-by-employee identity mapping or OAuth Token Exchange dependency.
- No customer database access or rewritten business query implementation.
- No production domain, certificate or shared-service restart performed by this
  development pass.
