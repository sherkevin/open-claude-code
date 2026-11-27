package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
)

var assistTokenPattern = regexp.MustCompile(`const TOKEN = "([0-9a-f]+)";`)

// assistCommand opens one assistance session from CMD. The customer runs this
// command once; the connector then pulls commands from Kaiwu, executes them and
// uploads the output. Only the customer side ever initiates a connection.
func assistCommand(args []string) {
	flags := flag.NewFlagSet("assist", flag.ExitOnError)
	opts, listen := registerCommon(flags)
	code := flags.String("code", "", "首次使用时的企业绑定码")
	direct := flags.Bool("direct", false, "不使用已运行的本机界面，直接在当前进程建立协助")
	interactive := flags.Bool("interactive", false, "在终端交互绑定设备并明确确认协助")
	flags.Parse(args)
	opts.NativeRelay = true

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if *interactive {
		if !*direct || strings.TrimSpace(*code) != "" || flags.NArg() != 0 {
			fatal(fmt.Errorf("交互模式必须使用 --direct，且接入码只能在本机提示中输入"))
		}
		if err := runInteractiveAssist(ctx, *opts); err != nil {
			fatal(err)
		}
		return
	}

	base := localAssistBase(*listen)
	if token, err := readLocalAssistToken(ctx, base); err == nil && !*direct {
		runLocalAssist(ctx, base, token)
		return
	}
	runDirectAssist(ctx, *opts, *code)
}

func localAssistBase(listen string) string {
	address := displayAddr(listen)
	if strings.HasPrefix(address, ":") {
		address = "127.0.0.1" + address
	}
	return "http://" + address
}

func readLocalAssistToken(ctx context.Context, base string) (string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/", nil)
	if err != nil {
		return "", err
	}
	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("本机界面返回 HTTP %d", response.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return "", err
	}
	match := assistTokenPattern.FindSubmatch(body)
	if match == nil {
		return "", fmt.Errorf("本机界面没有返回会话令牌")
	}
	return string(match[1]), nil
}

func runLocalAssist(ctx context.Context, base, token string) {
	client := &http.Client{Timeout: 7 * time.Second}
	var offer cloud.AssistOffer
	if err := localAssistPost(ctx, client, base, token, "start", struct{}{}, &offer); err != nil {
		fatal(fmt.Errorf("本机连接器未开始协助: %w", err))
	}
	printAssistStart(offer)
	status := func(ctx context.Context) (cloud.AssistStatus, error) {
		var data cloud.AssistStatus
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/assist/offer", nil)
		if err != nil {
			return data, err
		}
		request.Header.Set("X-Connector-Token", token)
		response, err := client.Do(request)
		if err != nil {
			return data, err
		}
		defer response.Body.Close()
		var payload struct {
			OK    bool               `json:"ok"`
			Data  cloud.AssistStatus `json:"data"`
			Error *struct {
				Message string `json:"message"`
			} `json:"error"`
		}
		if err := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&payload); err != nil {
			return data, err
		}
		if !payload.OK {
			if payload.Error != nil && payload.Error.Message != "" {
				return data, fmt.Errorf("%s", payload.Error.Message)
			}
			return data, fmt.Errorf("本机连接器返回了错误")
		}
		return payload.Data, nil
	}
	stop := func(ctx context.Context) error {
		return localAssistPost(ctx, client, base, token, "stop", map[string]string{"session_id": offer.ID}, nil)
	}
	watchAssist(ctx, status, stop)
}

func localAssistPost(ctx context.Context, client *http.Client, base, token, action string, body any, out any) error {
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/api/assist/"+action, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Connector-Token", token)
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	var payload struct {
		OK    bool            `json:"ok"`
		Data  json.RawMessage `json:"data"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&payload); err != nil {
		return err
	}
	if !payload.OK {
		if payload.Error != nil && payload.Error.Message != "" {
			return fmt.Errorf("%s", payload.Error.Message)
		}
		return fmt.Errorf("本机连接器返回了错误")
	}
	if out != nil && len(payload.Data) > 0 {
		return json.Unmarshal(payload.Data, out)
	}
	return nil
}

func runDirectAssist(ctx context.Context, opts agent.Options, code string) {
	instance, err := agent.New(opts)
	if err != nil {
		fatal(err)
	}
	defer instance.Close()
	if !instance.Status().Bound {
		if strings.TrimSpace(code) == "" {
			fatal(fmt.Errorf("本机尚未绑定企业。请让开物提供一次性绑定码，并用 --code 传入"))
		}
		if _, err := instance.Bind(ctx, code); err != nil {
			fatal(err)
		}
	}
	// Read the control plane's consent capability before opening so a direct
	// CLI session negotiates the same fixed week policy as the local page. A
	// failed read keeps the legacy policy; the start itself still requires the
	// explicit local action below.
	_, _ = instance.AssistStatus(ctx)
	offer, err := instance.OpenAssist(ctx)
	if err != nil {
		fatal(err)
	}
	printAssistStart(*offer)
	status := func(ctx context.Context) (cloud.AssistStatus, error) {
		return instance.AssistStatus(ctx)
	}
	stop := func(ctx context.Context) error {
		return instance.StopAssist(ctx, offer.ID)
	}
	watchAssist(ctx, status, stop)
}

func printAssistStart(offer cloud.AssistOffer) {
	fmt.Printf("\n已建立主动连接，协助编号 %s。\n", offer.ID)
	fmt.Println("开物工程师下发的命令会显示在这里并自动执行，输出会自动回传。")
	fmt.Println("按 Ctrl+C 或关闭此窗口即可停止协助。")
}

func watchAssist(ctx context.Context, status func(context.Context) (cloud.AssistStatus, error),
	stop func(context.Context) error) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	lastID, lastState := "", ""
	for {
		select {
		case <-ctx.Done():
			fmt.Println("\n正在停止协助...")
			stopCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := stop(stopCtx); err != nil {
				fmt.Fprintln(os.Stderr, "停止通知未送达:", err)
			}
			return
		case <-ticker.C:
			current, err := status(ctx)
			if err != nil {
				if ctx.Err() != nil {
					continue
				}
				fmt.Fprintln(os.Stderr, "读取协助状态失败:", err)
				continue
			}
			if !current.Enabled || current.Offer == nil {
				fmt.Println("协助已结束。")
				return
			}
			command := current.LastCommand
			if command == nil {
				continue
			}
			if command.ID != lastID {
				lastID, lastState = command.ID, ""
				fmt.Printf("\n[%s] 命令: %s\n", time.Now().Format("15:04:05"), command.Command)
			}
			if command.State == lastState {
				continue
			}
			lastState = command.State
			switch command.State {
			case "running":
				fmt.Println("执行中...")
			case "succeeded":
				fmt.Println("执行完成，输出已自动回传。")
			case "timeout":
				fmt.Println("执行超时，结果已自动回传。")
			case "canceled":
				fmt.Println("命令已取消。")
			default:
				fmt.Println("执行失败，结果已自动回传。")
			}
			if command.Output != "" {
				fmt.Println("----- 输出 -----")
				fmt.Println(command.Output)
				if command.Truncated {
					fmt.Println("（输出超过 64 KiB，已截断）")
				}
				fmt.Println("----------------")
			}
		}
	}
}
