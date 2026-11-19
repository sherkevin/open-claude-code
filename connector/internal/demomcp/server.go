// Package demomcp serves the bundled, read-only MCP used for connector
// onboarding and end-to-end verification.
package demomcp

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"sync"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

const (
	SourceName = "开物内置测试服务（示例数据）"
	Marker     = "KAIWU_BUILTIN_DEMO_V1"

	ToolPing         = "kaiwu_demo_ping"
	ToolOrderSummary = "kaiwu_demo_order_summary"
	ToolInventory    = "kaiwu_demo_inventory_query"
)

// Tools is the fixed, read-only tool allowlist for the bundled demo source.
var Tools = []string{ToolInventory, ToolOrderSummary, ToolPing}

type PingInput struct {
	Nonce string `json:"nonce" jsonschema:"random nonce to echo back, at most 128 characters"`
}

type PingOutput struct {
	Demo          bool   `json:"demo"`
	Marker        string `json:"marker"`
	Nonce         string `json:"nonce"`
	ServerTimeUTC string `json:"server_time_utc"`
	InstanceID    string `json:"instance_id"`
}

type OrderSummaryOutput struct {
	Demo       bool   `json:"demo"`
	Marker     string `json:"marker"`
	OrderCount int    `json:"order_count"`
	Currency   string `json:"currency"`
	Amount     int    `json:"amount"`
	Summary    string `json:"summary"`
	Notice     string `json:"notice"`
}

type InventoryOutput struct {
	Demo        bool   `json:"demo"`
	Marker      string `json:"marker"`
	ProductCode string `json:"product_code"`
	ProductName string `json:"product_name"`
	Stock       int    `json:"stock"`
	Notice      string `json:"notice"`
}

// Server owns one loopback-only Streamable HTTP MCP listener.
type Server struct {
	instanceID string

	mu         sync.Mutex
	endpoint   string
	httpServer *http.Server
	done       chan error
	running    bool
}

func New() *Server {
	return &Server{instanceID: newInstanceID()}
}

func newInstanceID() string {
	raw := make([]byte, 16)
	instanceID := "demo-instance"
	if _, err := rand.Read(raw); err == nil {
		instanceID = "demo_" + hex.EncodeToString(raw)
	}
	return instanceID
}

func (s *Server) Start() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.running {
		return nil
	}

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return fmt.Errorf("启动内置测试服务监听失败: %w", err)
	}
	address, ok := listener.Addr().(*net.TCPAddr)
	if !ok || !address.IP.IsLoopback() {
		_ = listener.Close()
		return errors.New("内置测试服务没有监听回环地址")
	}

	server := s.mcpServer()
	handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server {
		return server
	}, &mcp.StreamableHTTPOptions{
		Stateless:           true,
		JSONResponse:        true,
		MaxRequestBodyBytes: 64 << 10,
	})
	httpServer := &http.Server{
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	done := make(chan error, 1)
	s.instanceID = newInstanceID()
	s.endpoint = fmt.Sprintf("http://127.0.0.1:%d/mcp", address.Port)
	s.httpServer = httpServer
	s.done = done
	s.running = true
	go func() {
		err := httpServer.Serve(listener)
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		done <- err
	}()
	return nil
}

func (s *Server) Stop(ctx context.Context) error {
	s.mu.Lock()
	if !s.running {
		s.mu.Unlock()
		return nil
	}
	httpServer := s.httpServer
	done := s.done
	s.running = false
	s.httpServer = nil
	s.done = nil
	s.mu.Unlock()

	if httpServer != nil {
		if err := httpServer.Shutdown(ctx); err != nil {
			_ = httpServer.Close()
			return err
		}
	}
	if done != nil {
		select {
		case err := <-done:
			return err
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func (s *Server) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running
}

func (s *Server) Endpoint() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.endpoint
}

func (s *Server) InstanceID() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.instanceID
}

func (s *Server) mcpServer() *mcp.Server {
	server := mcp.NewServer(&mcp.Implementation{
		Name:    "kaiwu-builtin-demo",
		Version: "1.3.9",
	}, &mcp.ServerOptions{
		Capabilities: &mcp.ServerCapabilities{},
	})
	mcp.AddTool(server, &mcp.Tool{
		Name:        ToolPing,
		Description: "Echo a nonce and return the bundled demo instance and current UTC time.",
	}, s.ping)
	mcp.AddTool(server, &mcp.Tool{
		Name:        ToolOrderSummary,
		Description: "Return a fixed synthetic order summary containing 12 example orders.",
	}, s.orderSummary)
	mcp.AddTool(server, &mcp.Tool{
		Name:        ToolInventory,
		Description: "Return fixed synthetic inventory for product KAIWU-DEMO-001.",
	}, s.inventory)
	return server
}

func (s *Server) ping(_ context.Context, _ *mcp.CallToolRequest, input PingInput) (*mcp.CallToolResult, PingOutput, error) {
	if input.Nonce == "" {
		return nil, PingOutput{}, errors.New("nonce 不能为空")
	}
	if len(input.Nonce) > 128 {
		return nil, PingOutput{}, errors.New("nonce 不能超过 128 个字符")
	}
	return nil, PingOutput{
		Demo:          true,
		Marker:        Marker,
		Nonce:         input.Nonce,
		ServerTimeUTC: time.Now().UTC().Format(time.RFC3339Nano),
		InstanceID:    s.InstanceID(),
	}, nil
}

func (s *Server) orderSummary(_ context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, OrderSummaryOutput, error) {
	return nil, OrderSummaryOutput{
		Demo:       true,
		Marker:     Marker,
		OrderCount: 12,
		Currency:   "CNY",
		Amount:     12800,
		Summary:    "示例订单共 12 笔，合计 12800 元。",
		Notice:     "以上为开物内置测试服务的示例数据，不是客户业务数据。",
	}, nil
}

func (s *Server) inventory(_ context.Context, _ *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, InventoryOutput, error) {
	return nil, InventoryOutput{
		Demo:        true,
		Marker:      Marker,
		ProductCode: "KAIWU-DEMO-001",
		ProductName: "示例工业传感器",
		Stock:       128,
		Notice:      "以上为开物内置测试服务的示例数据，不是客户业务数据。",
	}, nil
}
