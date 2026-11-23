#!/usr/bin/env python3
"""Guard the Windows setup staging-directory policy.

Staging must live under the product's ProgramData directory so endpoint
protection policies that refuse to trust a Program Files root can still be
satisfied, and the resolved path must stay observable without changing the
fixed journal schema (two parsers enforce it byte for byte).
"""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
JOURNAL = ROOT / "packaging/windows/attempt-journal.c"
INSTALLER = ROOT / "packaging/windows/installer.nsi"
UNINSTALL = ROOT / "packaging/windows/uninstall.ps1"
BUILD = ROOT / "scripts/build.sh"
README = ROOT / "README.md"

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def export_body(text, name):
    match = re.search(r"^EXPORT\(" + re.escape(name) + r"\) \{\n", text, re.M)
    if not match:
        return None
    start = match.end()
    end = text.find("\n}\n", start)
    return None if end < 0 else text[start:end]


def main():
    journal = JOURNAL.read_text(encoding="utf-8")
    body = export_body(journal, "Workspace")
    check(body is not None, "attempt-journal.c 缺少 EXPORT(Workspace)")

    if body:
        check("CSIDL_COMMON_APPDATA" in body, "Workspace 必须从公共应用数据目录定位暂存根")
        common_data = body.find("pin_known_path(&workspace_dirs, workspace, COMMON_DATA_WRITES)")
        product_path = body.find('append_path(workspace, L"Kaiwu")')
        product_handle = body.find('open_child(parent_handle(&workspace_dirs), L"Kaiwu"')
        check(0 <= common_data < product_path < product_handle,
              "必须先按日志规则校验 ProgramData，再从已验证的句柄打开 Kaiwu")
        check("keep_directory(&workspace_dirs, product, COMMON_DATA_WRITES)" in body,
              "Kaiwu 必须校验属主、权限及目录属性")
        check("CSIDL_PROGRAM_FILES" not in body, "Workspace 不能回到 Program Files 根目录")
        check("create_workspace(&workspace_dirs, workspace, name)" in body, "Workspace 必须仍创建私有 SDDL 的暂存子目录")

    installer = INSTALLER.read_text(encoding="utf-8")
    detail = installer.find('DetailPrint "解包目录：$WorkDir"')
    extract = installer.find('SetOutPath "$WorkDir"')
    check(detail > 0, 'installer.nsi 必须打印解析后的解包目录')
    check(extract > 0 and detail < extract, "解包目录必须在写入暂存目录之前打印")
    check('logs\\install-workspace.txt' in installer, "installer.nsi 必须把解包目录写入 install-workspace.txt")
    check("$COMMONAPPDATA" not in installer, "NSIS 没有 $COMMONAPPDATA 常量，必须从环境变量解析 ProgramData")
    check('ReadEnvStr $R9 "ProgramData"' in installer, "installer.nsi 必须通过 ReadEnvStr 取得 ProgramData")

    uninstall = UNINSTALL.read_text(encoding="utf-8")
    check("^kw-[0-9a-f]{32}$" in uninstall, "卸载脚本必须只清理 kw-<32 hex> 形式的遗留暂存目录")

    version = re.search(r'VERSION="\$\{VERSION:-([0-9.]+)\}"', BUILD.read_text(encoding="utf-8"))
    check(version is not None, "build.sh 缺少默认版本号")
    if version:
        documented = re.search(r"本目录随 `([0-9.]+)` 内部开发版本构建", README.read_text(encoding="utf-8"))
        check(documented is not None, "README 缺少版本声明")
        if documented:
            check(documented.group(1) == version.group(1),
                  f"README 版本 {documented.group(1)} 与 build.sh 默认版本 {version.group(1)} 不一致")

    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("PASS staging directory policy: ProgramData root, create-child rights, observable path, uninstall sweep, version parity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
