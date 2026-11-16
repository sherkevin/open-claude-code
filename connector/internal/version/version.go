// Package version 保存构建时注入的版本信息。
package version

import "fmt"

// 下面三个变量在编译时通过 -ldflags "-X ...=..." 注入。
var (
	Version   = "1.0.0-dev"
	Commit    = "unknown"
	BuildTime = "unknown"
)

// String 返回给用户和心跳上报使用的版本描述。
func String() string {
	return fmt.Sprintf("%s (commit %s, built %s)", Version, Commit, BuildTime)
}
