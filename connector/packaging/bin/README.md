# frpc 二进制放置说明

构建交付包需要把 frpc 0.61.1 的两个二进制放在本目录：

- `frpc-linux-amd64`
- `frpc-windows-amd64.exe`

来源：[fatedier/frp v0.61.1](https://github.com/fatedier/frp/releases/tag/v0.61.1)，Apache License 2.0。

为避免把约 29 MB 的第三方二进制提交进代码仓库，这两个文件已在 `connector/.gitignore` 中忽略。克隆仓库后请自行下载或从交付人员处获取后再运行 `scripts/build.sh`。

`scripts/build.sh` 会校验官方发布包解压后的固定 SHA-256：

- Windows `frpc.exe`：`d9a8cf8bc61bd5c8d57a2a4801316a18291f76d45dc5fd8aaf425bd91b57159d`
- Linux `frpc`：`0bb98b7a69ce8ee23bc60fec1664f22040a2d44032296e03c8b6573a5d28dd0b`

官方发布归档本身必须同时匹配 `third_party/frp/OFFICIAL_ARCHIVE_CHECKSUMS.txt` 中记录的官方值。
