//go:build windows

package main

import (
	"context"
	"log"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/windows/svc"
)

const windowsServiceName = "KaiwuConnector"

type connectorService struct{}

// runServiceIfNeeded 在 Windows 服务控制管理器启动本进程时接管生命周期。
// 普通命令行启动时返回 false，继续走原有 CLI。
func runServiceIfNeeded() bool {
	isService, err := svc.IsWindowsService()
	if err != nil || !isService {
		return false
	}
	if err := svc.Run(windowsServiceName, &connectorService{}); err != nil {
		log.Printf("Windows service exited with error: %v", err)
	}
	return true
}

func (s *connectorService) Execute(args []string, requests <-chan svc.ChangeRequest, changes chan<- svc.Status) (bool, uint32) {
	changes <- svc.Status{State: svc.StartPending}

	if len(args) > 0 && (args[0] == "serve" || args[0] == "run") {
		args = args[1:]
	}

	// 在创建服务主体前把日志落到数据目录，便于服务启动失败时排查。
	dataDir := serviceDataDir(args)
	if dataDir != "" {
		_ = os.MkdirAll(dataDir, 0o755)
		logPath := filepath.Join(dataDir, "service.log")
		if file, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
			log.SetOutput(file)
			defer file.Close()
		}
	}
	log.Printf("service start: %s", strings.Join(args, " "))

	server, instance, _, _, err := newServer(args)
	if err != nil {
		log.Printf("service configuration failed: %v", err)
		return false, 1
	}
	defer instance.Close()

	listener, err := net.Listen("tcp", server.Addr)
	if err != nil {
		log.Printf("service listen failed on %s: %v", server.Addr, err)
		instance.Close()
		return false, 1
	}
	go func() {
		if serveErr := server.Serve(listener); serveErr != nil {
			log.Printf("service HTTP server stopped: %v", serveErr)
		}
	}()

	instance.StartRuntime()

	changes <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	for request := range requests {
		switch request.Cmd {
		case svc.Interrogate:
			changes <- request.CurrentStatus
		case svc.Stop, svc.Shutdown:
			changes <- svc.Status{State: svc.StopPending}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = server.Shutdown(ctx)
			instance.CloseContext(ctx)
			cancel()
			return false, 0
		}
	}
	return false, 0
}

func serviceDataDir(args []string) string {
	for index, arg := range args {
		if arg == "--data-dir" && index+1 < len(args) {
			return args[index+1]
		}
		if strings.HasPrefix(arg, "--data-dir=") {
			return strings.TrimPrefix(arg, "--data-dir=")
		}
	}
	return defaultDataDir()
}
