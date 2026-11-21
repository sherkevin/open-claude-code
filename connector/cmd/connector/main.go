// 开物内网连接器：在客户内网守护 MCP 通道，提供本机配置界面与诊断。
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/sherkevin/open-claude-code/connector/internal/agent"
	"github.com/sherkevin/open-claude-code/connector/internal/config"
	"github.com/sherkevin/open-claude-code/connector/internal/version"
	"github.com/sherkevin/open-claude-code/connector/internal/web"
)

func main() {
	if runServiceIfNeeded() {
		return
	}
	if len(os.Args) < 2 {
		serve(os.Args[1:])
		return
	}
	switch os.Args[1] {
	case "serve", "run":
		serve(os.Args[2:])
	case "version", "--version", "-v":
		fmt.Println(version.String())
	case "import":
		importCommand(os.Args[2:])
	case "export":
		exportCommand(os.Args[2:])
	case "diagnose":
		diagnoseCommand(os.Args[2:])
	case "report-install":
		reportInstallCommand(os.Args[2:])
	case "help", "--help", "-h":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "未知命令: %s\n\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Print(`开物内网连接器

用法:
  connector serve       启动本机界面与通道守护（默认）
  connector import      校验并导入 MCP 配置文件
  connector export      生成平台可导入的配置
  connector diagnose    执行一次内网诊断
  connector report-install --attempt <编号>  服务停止时提交当前设备的安装记录
  connector version     查看版本

常用参数:
  --data-dir   本机数据目录（默认按操作系统选择）
  --listen     本机界面监听地址（默认 127.0.0.1:17872）
  --cloud      云端控制面地址
  --edge       云端 Edge 地址，用于导出平台配置
  --frpc       frpc 可执行文件路径（默认与主程序同目录）
`)
}

func newServer(args []string) (*web.Server, *agent.Agent, *agent.Options, *string, error) {
	flags := flag.NewFlagSet("serve", flag.ContinueOnError)
	opts, listen := registerCommon(flags)
	opts.ExclusiveDiagnostics = true
	if err := flags.Parse(args); err != nil {
		return nil, nil, nil, nil, err
	}
	instance, err := agent.New(*opts)
	if err != nil {
		return nil, nil, nil, nil, err
	}
	server, err := web.New(instance, *listen)
	if err != nil {
		instance.Close()
		return nil, nil, nil, nil, err
	}
	return server, instance, opts, listen, nil
}

func serve(args []string) {
	server, instance, opts, listen, err := newServer(args)
	if err != nil {
		fatal(err)
	}
	defer instance.Close()
	instance.StartRuntime()
	fmt.Printf("开物内网连接器 %s\n", version.String())
	fmt.Printf("本机界面: http://%s/\n", displayAddr(*listen))
	fmt.Printf("数据目录: %s\n", opts.DataDir)
	if err := server.ListenAndServe(); err != nil {
		fatal(err)
	}
}

func importCommand(args []string) {
	flags := flag.NewFlagSet("import", flag.ExitOnError)
	flags.String("data-dir", defaultDataDir(), "数据目录")
	flags.String("cloud", "", "云端控制面地址")
	flags.String("edge", "", "云端 Edge 地址")
	file := flags.String("file", "", "待导入的配置文件")
	allowStdio := flags.Bool("allow-stdio", false, "允许导入标准输入输出型 MCP（条件分支）")
	flags.Parse(args)
	if *file == "" {
		fatal(fmt.Errorf("请用 --file 指定配置文件"))
	}
	raw, err := os.ReadFile(*file)
	if err != nil {
		fatal(err)
	}
	report, err := config.ParseHostConfig(raw, config.ImportOptions{AllowStdio: *allowStdio})
	if err != nil {
		fatal(err)
	}
	printJSON(report)
	if report.HasErrors() {
		os.Exit(1)
	}
}

func exportCommand(args []string) {
	flags := flag.NewFlagSet("export", flag.ExitOnError)
	dataDir := flags.String("data-dir", defaultDataDir(), "数据目录")
	edge := flags.String("edge", "", "云端 Edge 地址")
	flags.Parse(args)
	instance, err := agent.New(agent.Options{DataDir: *dataDir, EdgeBase: *edge, CloudBase: "https://api.example.com"})
	if err != nil {
		fatal(err)
	}
	raw, err := instance.Export()
	if err != nil {
		fatal(err)
	}
	fmt.Println(string(raw))
}

func diagnoseCommand(args []string) {
	flags := flag.NewFlagSet("diagnose", flag.ExitOnError)
	opts, _ := registerCommon(flags)
	sourceID := flags.String("source", "", "只诊断指定来源")
	probe := flags.String("probe", "", "试调用的工具名称")
	flags.Parse(args)
	instance, err := agent.New(*opts)
	if err != nil {
		fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	reports, err := instance.Diagnose(ctx, *sourceID, *probe)
	if err != nil {
		fatal(err)
	}
	printJSON(reports)
	for _, report := range reports {
		if !report.OK {
			os.Exit(1)
		}
	}
}

func reportInstallCommand(args []string) {
	flags := flag.NewFlagSet("report-install", flag.ExitOnError)
	opts, _ := registerCommon(flags)
	attempt := flags.String("attempt", "", "脱敏安装记录的编号")
	flags.Parse(args)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	count, err := agent.ReportInstall(ctx, *opts, *attempt)
	if err != nil {
		fatal(err)
	}
	printJSON(map[string]int{"accepted": count})
}

func registerCommon(flags *flag.FlagSet) (*agent.Options, *string) {
	opts := &agent.Options{}
	flags.StringVar(&opts.DataDir, "data-dir", defaultDataDir(), "本机数据目录")
	flags.StringVar(&opts.CloudBase, "cloud", os.Getenv("KAIWU_CLOUD_BASE"), "云端控制面地址")
	flags.StringVar(&opts.EdgeBase, "edge", os.Getenv("KAIWU_EDGE_BASE"), "云端 Edge 地址")
	flags.StringVar(&opts.FrpcBinary, "frpc", "", "frpc 可执行文件路径")
	flags.BoolVar(&opts.AllowStdio, "allow-stdio", false, "允许标准输入输出型 MCP（条件分支）")
	listen := flags.String("listen", "127.0.0.1:17872", "本机界面监听地址")
	return opts, listen
}

func defaultDataDir() string {
	if value := os.Getenv("KAIWU_CONNECTOR_DATA"); value != "" {
		return value
	}
	switch runtime.GOOS {
	case "windows":
		if programData := os.Getenv("ProgramData"); programData != "" {
			return filepath.Join(programData, "Kaiwu", "Connector")
		}
		return filepath.Join(os.Getenv("USERPROFILE"), "Kaiwu", "Connector")
	case "darwin":
		home, _ := os.UserHomeDir()
		return filepath.Join(home, "Library", "Application Support", "Kaiwu", "Connector")
	default:
		return "/var/lib/kaiwu-connector"
	}
}

func displayAddr(addr string) string {
	if strings.HasPrefix(addr, "0.0.0.0:") {
		return "127.0.0.1:" + strings.TrimPrefix(addr, "0.0.0.0:")
	}
	return addr
}

func printJSON(value any) {
	raw, _ := json.MarshalIndent(value, "", "  ")
	fmt.Println(string(raw))
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "错误:", err)
	os.Exit(1)
}
