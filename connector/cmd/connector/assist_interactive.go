// Interactive terminal entry for the existing attended assistance protocol.
// It never opens a grant until the customer confirms on the local terminal.
package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
	"github.com/sherkevin/open-claude-code/connector/internal/cloud"
)

func assistPrompt(ctx context.Context, reader *bufio.Reader, out io.Writer, prompt string) (string, error) {
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	fmt.Fprint(out, prompt)
	type answer struct {
		text string
		err  error
	}
	done := make(chan answer, 1)
	go func() {
		line, err := reader.ReadString('\n')
		done <- answer{strings.TrimSpace(line), err}
	}()
	select {
	case <-ctx.Done():
		return "", ctx.Err()
	case result := <-done:
		// A closed or redirected input is not confirmation.
		if result.err != nil {
			return "", result.err
		}
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return result.text, nil
	}
}

func runInteractiveAssist(ctx context.Context, opts agent.Options) error {
	if os.Geteuid() == 0 {
		return fmt.Errorf("请使用客户批准的普通账号，不要使用 root 或 sudo")
	}
	in, err := os.Stdin.Stat()
	if err != nil || in.Mode()&os.ModeCharDevice == 0 {
		return fmt.Errorf("请在可交互终端中运行，禁止自动确认")
	}
	out, err := os.Stdout.Stat()
	if err != nil || out.Mode()&os.ModeCharDevice == 0 {
		return fmt.Errorf("请保持终端可见，协助命令和输出会显示在这里")
	}
	reader := bufio.NewReader(os.Stdin)
	fmt.Println("开物技术协助：不安装系统服务，不自动开启协助。")
	fmt.Println("仅在企业管理员批准后使用；协助命令以当前普通账号权限执行，可能读取或修改该账号可访问的文件。")
	fmt.Println("随时按 Ctrl+C 停止；仅支持明确授权的本次排查。")
	instance, err := newAssistCLIAgent(opts)
	if err != nil {
		return fmt.Errorf("初始化本机连接器失败")
	}
	defer instance.Close()
	status := instance.Status()
	if !status.Bound {
		code, err := assistPrompt(ctx, reader, os.Stdout, "请输入开物提供的一次性企业绑定码: ")
		if err != nil {
			return err
		}
		if code == "" {
			return fmt.Errorf("未输入绑定码")
		}
		bindCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		status, err = instance.Bind(bindCtx, code)
		cancel()
		if err != nil {
			return fmt.Errorf("企业绑定失败，请联系开物核对绑定码")
		}
	}
	fmt.Printf("设备编号: %s\n", status.ConnectorID)
	fmt.Println("等待开物为此设备开通协助资格（最多10分钟）。当前未开启协助。")
	waitCtx, stopWait := context.WithTimeout(ctx, 10*time.Minute)
	defer stopWait()
	week := false
	for {
		available, err := instance.AssistStatus(waitCtx)
		if err == nil && available.Enabled {
			// A fresh local confirmation is still mandatory, even if there is a cloud offer.
			week = available.WeekConsent
			break
		}
		select {
		case <-waitCtx.Done():
			return fmt.Errorf("等待设备授权已结束，尚未开始协助")
		case <-time.After(3 * time.Second):
		}
	}
	if week {
		fmt.Println("本次授权自本机确认起固定7天（168小时）；终端保持运行时断线可重连，不能延长。按 Ctrl+C 停止，重启进程需重新确认。")
	} else {
		fmt.Println("当前服务端仅支持旧版授权：断线后30分钟到期，最长24小时。")
	}
	reply, err := assistPrompt(ctx, reader, os.Stdout, "确认允许开物工程师执行本次协助命令？输入 y 开始 [y/N]: ")
	if err != nil {
		return err
	}
	if !strings.EqualFold(reply, "y") {
		fmt.Println("已取消，未开启协助。")
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	offer, err := instance.OpenAssist(ctx, week)
	if err != nil {
		return fmt.Errorf("尚未开启协助，请核对本设备授权后重试")
	}
	printAssistStart(*offer)
	watchAssist(ctx, func(ctx context.Context) (cloud.AssistStatus, error) {
		return instance.AssistStatus(ctx)
	}, func(ctx context.Context) error {
		return instance.StopAssist(ctx, offer.ID)
	})
	return nil
}
