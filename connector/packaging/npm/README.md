# 开物 Linux 临时协助（NPX）
使用经过企业批准的非 root 普通账号，在可见的交互式 Linux 终端运行。需要 Node.js 18+、npm 9+、bash、sha256sum、flock。一次命令完成下载、选择 CPU 架构及启动，不安装系统服务，也不自动授予远程权限。
首次输入单独的一次性企业绑定码（不写在命令行参数中），等待开物为本设备开放协助资格，输入 y 明确开始；终端显示命令和结果，按 Ctrl+C 停止。已绑定设备再次运行仍必须确认。运行身份必须由客户批准，勿使用 sudo 或 root。运行结束后不会在后台自动重连协助。
该包不提供任何业务凭据，不打开公网入站端口，不依赖客户数据库密码。仅为授权的当次支持使用。
本包须经正式分发审核后才能使用公网下载地址；尚未发布 npm registry。

When the server advertises the week policy, the interactive terminal discloses a fixed 168-hour grant before the customer confirms. Network reconnection while the process stays running preserves the deadline. Ctrl+C, revocation, expiry, or restarting the process ends local authority; restarting requires new confirmation. Older servers retain the legacy policy.
