package web

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
	"github.com/sherkevin/open-claude-code/connector/internal/state"
)

func TestUpgradePageKeepsUserPause(t *testing.T) {
	var desired atomic.Int32
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/v1/enrollments/redeem" {
			_, _ = io.WriteString(w, `{"connector_id":"conn_ui","tenant_id":"tenant_ui","device_token":"synthetic"}`)
			return
		}
		if strings.HasSuffix(r.URL.Path, "/desired-state") {
			desired.Add(1)
		}
		_, _ = io.WriteString(w, `{}`)
	}))
	defer control.Close()
	opts := agent.Options{DataDir: t.TempDir(), CloudBase: control.URL, EdgeBase: control.URL}

	open := func() (*Server, *agent.Agent) {
		t.Helper()
		instance, err := agent.New(opts)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(instance.Close)
		ui, err := New(instance, loopbackHost)
		if err != nil {
			t.Fatal(err)
		}
		return ui, instance
	}

	ui, first := open()
	if _, err := first.Bind(context.Background(), "synthetic"); err != nil {
		t.Fatal(err)
	}
	first.StartRuntime()
	pause := upgradeIntentPageRequest(t, ui, http.MethodPost, "/api/pause")
	if pause.Code != http.StatusOK {
		t.Fatalf("POST /api/pause returned %d: %s", pause.Code, pause.Body.String())
	}
	if got := decodeUpgradeIntentStatus(t, pause); got.State != state.Paused {
		t.Fatalf("POST /api/pause returned %s", got.State)
	}
	first.Close()

	for boot := 1; boot <= 2; boot++ {
		ui, instance := open()
		instance.StartRuntime()
		pageStatus := upgradeIntentPageRequest(t, ui, http.MethodGet, "/api/status")
		status := decodeUpgradeIntentStatus(t, pageStatus)
		if status.State != state.Paused || status.StateLabel != "已暂停" ||
			status.FrpcRunning || status.TunnelConnected || !status.BuiltinDemo.Running {
			t.Fatalf("boot %d: state=%s label=%q frpc=%t connected=%t demo=%t",
				boot, status.State, status.StateLabel, status.FrpcRunning, status.TunnelConnected, status.BuiltinDemo.Running)
		}
		if boot == 2 {
			script := pageScriptFragment(t, "function renderStageSummary(){", "\nfunction keepLocalSelfTest")
			runNodeScript(t, `
const currentStatus = `+string(envelopeDataForUpgradeIntent(t, status))+`;
const $ = () => ({value:""});
const diagnosisBySource = {};
const sourceByID = () => null;
const labels = {};
function setStage(id, label){ labels[id] = label; }
`+script+`
renderStageSummary();
require("node:assert/strict").equal(labels.tunnel, "已暂停");
`)
			reply := upgradeIntentPageRequest(t, ui, http.MethodPost, "/api/self-test")
			var result struct {
				Data agent.SelfTestResult `json:"data"`
			}
			if err := json.Unmarshal(reply.Body.Bytes(), &result); err != nil {
				t.Fatal(err)
			}
			if len(result.Data.Stages) == 0 || result.Data.Stages[0].ID != "local_mcp" ||
				result.Data.Stages[0].Status != agent.SelfTestStatusPassed {
				t.Fatalf("paused connector did not pass local demo: %s", reply.Body.String())
			}
		}
		instance.Close()
	}
	if got := desired.Load(); got != 0 {
		t.Fatalf("paused connector requested a new tunnel %d times", got)
	}
}

func TestUpgradePagePausePersistenceError(t *testing.T) {
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"connector_id":"conn_ui","tenant_id":"tenant_ui","device_token":"synthetic"}`)
	}))
	defer control.Close()
	dir := t.TempDir()
	instance, err := agent.New(agent.Options{DataDir: dir, CloudBase: control.URL, EdgeBase: control.URL})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(instance.Close)
	if _, err := instance.Bind(context.Background(), "synthetic"); err != nil {
		t.Fatal(err)
	}
	ui, err := New(instance, loopbackHost)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "state.json.tmp"), 0o700); err != nil {
		t.Fatal(err)
	}
	response := upgradeIntentPageRequest(t, ui, http.MethodPost, "/api/pause")
	if response.Code != http.StatusBadRequest || !strings.Contains(response.Body.String(), "无法保存通道选择") {
		t.Fatalf("failed pause storage reported success: %d %s", response.Code, response.Body.String())
	}
}

func envelopeDataForUpgradeIntent(t *testing.T, status agent.Status) []byte {
	t.Helper()
	body, err := json.Marshal(status)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func upgradeIntentPageRequest(t *testing.T, ui *Server, method, route string) *httptest.ResponseRecorder {
	t.Helper()
	handler := ui.Handler()
	index := httptest.NewRequest(http.MethodGet, "http://"+loopbackHost+"/", nil)
	indexReply := httptest.NewRecorder()
	handler.ServeHTTP(indexReply, index)
	request := httptest.NewRequest(method, "http://"+loopbackHost+route, strings.NewReader("{}"))
	for _, cookie := range indexReply.Result().Cookies() {
		request.AddCookie(cookie)
	}
	request.Header.Set("X-Connector-Token", ui.Token)
	request.Header.Set("Origin", "http://"+loopbackHost)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	return recorder
}

func decodeUpgradeIntentStatus(t *testing.T, response *httptest.ResponseRecorder) agent.Status {
	t.Helper()
	var envelope struct {
		Data agent.Status `json:"data"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	return envelope.Data
}
