/* Run with MinGW and Windows or Wine. Exercise the same plug-in entry as NSIS. */
#include "../../packaging/windows/attempt-journal.c"
#include <assert.h>

static void check_popup(const WCHAR *const args[], size_t count, const WCHAR *reason,
                        const WCHAR *exit_code, const WCHAR *file, BOOL expect_support_note) {
    Stack *stack = NULL;
    for (size_t i = count; i > 0; --i) push(&stack, 1024, args[i - 1]);
    ExplainFailure(NULL, 1024, NULL, &stack, NULL);
    WCHAR message[1024];
    assert(pop(&stack, message, ARRAY_SIZE(message)) && !stack);
    assert(wcsstr(message, attempt) && wcsstr(message, reason) && wcsstr(message, exit_code));
    assert(wcsstr(message, L"未能保存本次摘要") && wcsstr(message, L"Ctrl+C"));
    const WCHAR *ts = wcsstr(message, L"记录时间（UTC）：");
    assert(ts);
    ts += wcslen(L"记录时间（UTC）：");
    const WCHAR *pattern = L"dddd-dd-ddTdd:dd:ddZ\r\n";
    for (size_t i = 0; pattern[i]; ++i) {
        if (pattern[i] == L'd') assert(ts[i] >= L'0' && ts[i] <= L'9');
        else assert(ts[i] == pattern[i]);
    }
    assert(!wcsstr(message, L"private-canary"));
    if (file) assert(wcsstr(message, file));
    assert((wcsstr(message, L"同一时段涉及该文件的终端防护处置记录") != NULL) == expect_support_note);
}

int main(void) {
    wcscpy(attempt, L"1234567890abcdef1234567890abcdef");
    const WCHAR *full[] = {L"source_precheck", L"file_missing", L"frpc.exe",
                           L"result=PRECHECK_FAILED", L"1"};
    const WCHAR *unknown[] = {L"private-canary", L"private-canary", L"private-canary",
                              L"private-canary", L"private-canary"};
    const WCHAR *late[] = {L"pre_change_verification", L"file_missing_after_check", L"frpc.exe",
                           L"result=FAILED_RECOVERY_UNCONFIRMED", L"1"};
    const WCHAR *missing_unknown_file[] = {L"source_precheck", L"file_missing", L"private-canary",
                                           L"result=PRECHECK_FAILED", L"1"};
    const WCHAR *workspace_unavailable[] = {L"extract", L"workspace_unavailable", L"",
                                            L"not_started", L"1"};
    check_popup(late, 5, L"原因码：file_missing_after_check", L"安装器退出码：1", L"首次校验通过", TRUE);
    check_popup(full, 5, L"原因码：file_missing", L"安装器退出码：1", L"涉及文件：frpc.exe", TRUE);
    check_popup(full, 4, L"原因码：file_missing", L"安装器退出码：未确认", L"涉及文件：frpc.exe", TRUE);
    check_popup(full, 1, L"原因码：unknown", L"安装器退出码：未确认", L"校验解包后的安装文件", FALSE);
    check_popup(NULL, 0, L"原因码：unknown", L"安装器退出码：未确认", L"失败阶段：无法确认", FALSE);
    check_popup(unknown, 5, L"原因码：unknown", L"安装器退出码：未确认", L"恢复或清理状态无法确认", FALSE);
    check_popup(missing_unknown_file, 5, L"原因码：file_missing", L"安装器退出码：1", NULL, FALSE);
    workspace_failure = (SummaryResult){.step = L"创建并校验私有暂存目录", .error = ERROR_ACCESS_DENIED};
    check_popup(workspace_unavailable, 5, L"原因码：workspace_unavailable", L"安装器退出码：1",
                L"临时目录检查：创建并校验私有暂存目录（系统错误码 5）", FALSE);
    workspace_failure = (SummaryResult){.step = L"校验 Windows 公共应用数据目录"};
    check_popup(workspace_unavailable, 5, L"原因码：workspace_unavailable", L"安装器退出码：1",
                L"临时目录检查：校验 Windows 公共应用数据目录（未返回系统错误码）", FALSE);
    puts("PASS failure popup: file checks, workspace diagnostics, and untrusted input");
    return 0;
}
