# frpc 二进制放置说明

构建交付包需要把 frpc 0.61.1 的两个二进制放在本目录：

- `frpc-linux-amd64`
- `frpc-windows-amd64.exe`

来源：[fatedier/frp v0.61.1](https://github.com/fatedier/frp/releases/tag/v0.61.1)，Apache License 2.0。

为避免把约 29 MB 的第三方二进制提交进代码仓库，这两个文件已在 `connector/.gitignore` 中忽略。克隆仓库后请自行下载或从交付人员处获取后再运行 `scripts/build.sh`。
