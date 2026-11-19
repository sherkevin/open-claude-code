package cloud

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestSelfTestUsesFixedCloudEndpoint(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/connectors/conn_test/self-test" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"ok":true,"trace_id":"kwtr_aaaaaaaaaaaaaaaaaaaaaaaa","source_id":"src_11111111111111111111111111111111","stages":[{"id":"edge_connect","status":"passed","message":"ok"},{"id":"mcp_initialize","status":"passed","message":"ok"},{"id":"tools_list","status":"passed","message":"ok"},{"id":"tools_call","status":"passed","message":"ok"},{"id":"probe_validation","status":"passed","message":"ok"}],"probe":{"demo":true,"marker":"KAIWU_BUILTIN_DEMO_V1","nonce":"nonce-1","server_time_utc":"2026-11-19T00:00:00Z","instance_id":"demo_11111111111111111111111111111111"}}}`))
	}))
	defer server.Close()

	client, err := New(server.URL, "device-token")
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.SelfTest(context.Background(), "conn_test", "src_11111111111111111111111111111111", "nonce-1")
	if err != nil {
		t.Fatal(err)
	}
	if !result.OK || result.TraceID == "" || result.SourceID != "src_11111111111111111111111111111111" ||
		result.Probe.Nonce != "nonce-1" || len(result.Stages) != 5 || result.HTTPStatus != http.StatusOK {
		t.Fatalf("unexpected self-test result: %+v", result)
	}
}

func TestSelfTestPreservesLayeredFailureEnvelope(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"data":{"ok":false,"trace_id":"kwtr_bbbbbbbbbbbbbbbbbbbbbbbb","source_id":"src_11111111111111111111111111111111","stages":[{"id":"edge_connect","status":"failed","message":"Edge unavailable"},{"id":"mcp_initialize","status":"skipped","message":"not run"}],"probe":{}}}`))
	}))
	defer server.Close()

	client, err := New(server.URL, "device-token")
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.SelfTest(context.Background(), "conn_test", "src_11111111111111111111111111111111", "nonce-2")
	if err != nil {
		t.Fatal(err)
	}
	if result.OK || result.HTTPStatus != http.StatusBadGateway || len(result.Stages) != 2 ||
		result.Stages[0].Status != "failed" || result.TraceID == "" {
		t.Fatalf("layered failure was lost: %+v", result)
	}
}

func TestSelfTestReturnsBoundedAPIErrorWhenEndpointUnsupported(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"error":{"code":"HTTP_404","message":"not found"}}`))
	}))
	defer server.Close()

	client, err := New(server.URL, "device-token")
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.SelfTest(context.Background(), "conn_test", "src_11111111111111111111111111111111", "nonce-3")
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Code != "HTTP_404" {
		t.Fatalf("unexpected self-test error: %v", err)
	}
}
