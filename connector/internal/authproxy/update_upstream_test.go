package authproxy

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestUpdateUpstreamKeepsListenerAndChangesOnlyTarget(t *testing.T) {
	target := func(name string) *httptest.Server {
		return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"jsonrpc":"2.0","id":1,"result":{"target":"` + name + `"}}`))
		}))
	}
	first := target("first")
	defer first.Close()
	second := target("second")
	defer second.Close()

	proxy, baseURL := startTestProxy(t, Options{
		SourceID:    "src_demo_update",
		UpstreamURL: first.URL,
	})
	defer proxy.Close(testContext(t))
	host, beforePort, err := proxy.Endpoint()
	if err != nil {
		t.Fatal(err)
	}

	status, _, body := postMCP(t, baseURL, "", "ping", map[string]any{})
	if status != http.StatusOK || !strings.Contains(body, `"target":"first"`) {
		t.Fatalf("first upstream was not used: status=%d body=%s", status, body)
	}
	if err := proxy.UpdateUpstream(second.URL); err != nil {
		t.Fatal(err)
	}
	afterHost, afterPort, err := proxy.Endpoint()
	if err != nil || afterHost != host || afterPort != beforePort {
		t.Fatalf("listener changed after upstream update: before=%s:%d after=%s:%d err=%v", host, beforePort, afterHost, afterPort, err)
	}
	status, _, body = postMCP(t, baseURL, "", "ping", map[string]any{})
	if status != http.StatusOK || !strings.Contains(body, `"target":"second"`) {
		t.Fatalf("updated upstream was not used: status=%d body=%s", status, body)
	}
	if err := proxy.UpdateUpstream("https://user:secret@example.com/mcp"); err == nil {
		t.Fatal("credential-bearing upstream must be rejected")
	}
	if got := proxy.UpstreamURL(); got != second.URL {
		t.Fatalf("invalid update changed the active upstream: %s", got)
	}
}
