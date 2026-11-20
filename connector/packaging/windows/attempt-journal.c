#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0601
#include <windows.h>
#include <winternl.h>
#include <aclapi.h>
#include <bcrypt.h>
#include <sddl.h>
#include <shlobj.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>

/* NSIS Unicode plug-in ABI. All paths are derived from Windows known folders. */
typedef struct Stack { struct Stack *next; WCHAR text[1]; } Stack;
#define EXPORT(name) __declspec(dllexport) void __cdecl name( \
    HWND window __attribute__((unused)), int size __attribute__((unused)), \
    WCHAR *vars __attribute__((unused)), Stack **stack __attribute__((unused)), \
    void *extra __attribute__((unused)))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define MAX_ATTEMPTS 128
#define MAX_BYTES 4096
#define MAX_EVENTS 48
#define ATTEMPT_NAME_LENGTH 38
#define SUMMARY_TEXT_LENGTH 512
#define SUMMARY_NAME L"install-failure-summary.txt"
#define SUMMARY_PENDING L"install-failure-summary.pending"
#define READ_SHARE (FILE_SHARE_READ | FILE_SHARE_WRITE)
#define PRIVATE_DIRECTORY L"O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
#define PRIVATE_FILE L"O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)"

typedef struct { HANDLE items[MAX_PATH / 2]; size_t count; } DirectoryChain;
static DirectoryChain journal_dirs, workspace_dirs, program_dirs;
static HANDLE journal = INVALID_HANDLE_VALUE;
static BOOL journal_started, journal_finished;
static WCHAR attempt[33], workspace[MAX_PATH];
static HANDLE logs_directory = INVALID_HANDLE_VALUE;
typedef struct {
    const WCHAR *step;
    const WCHAR *file;
    DWORD error;
    DWORD cleanup_error;
} SummaryResult;
static SummaryResult logs_failure = {.step = L"准备日志目录"};
static DWORD attempt_error;
static PSID admins, system_account, trusted_installer;

static void push(Stack **stack, int size, const WCHAR *value) {
    Stack *item = GlobalAlloc(GPTR, sizeof(*item) + (size_t)size * sizeof(WCHAR));
    if (!item) return;
    lstrcpynW(item->text, value, size);
    item->next = *stack;
    *stack = item;
}

static BOOL pop(Stack **stack, WCHAR *value, int size) {
    Stack *item = *stack;
    if (!item) return FALSE;
    lstrcpynW(value, item->text, size);
    *stack = item->next;
    GlobalFree(item);
    return TRUE;
}

static void close_chain(DirectoryChain *chain) {
    while (chain->count) CloseHandle(chain->items[--chain->count]);
}

static BOOL trusted_sid(PSID sid) {
    return sid && (EqualSid(sid, admins) || EqualSid(sid, system_account) || EqualSid(sid, trusted_installer));
}

static BOOL safe_permissions(HANDLE object, DWORD allowed_writes) {
    PSID owner = NULL;
    PACL dacl = NULL;
    PSECURITY_DESCRIPTOR sd = NULL;
    DWORD status = GetSecurityInfo(object, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                                   &owner, NULL, &dacl, NULL, &sd);
    if (status != ERROR_SUCCESS) {
        SetLastError(status);
        return FALSE;
    }

    DWORD error = ERROR_SUCCESS;
    DWORD write_rights = DELETE | WRITE_DAC | WRITE_OWNER | FILE_DELETE_CHILD | FILE_WRITE_EA |
                         FILE_WRITE_ATTRIBUTES | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY;
    write_rights &= ~allowed_writes;
    GENERIC_MAPPING mapping = {FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS};
    BOOL safe = trusted_sid(owner) && dacl;
    for (DWORD i = 0; safe && i < dacl->AceCount; ++i) {
        ACE_HEADER *header = NULL;
        if (!GetAce(dacl, i, (void **)&header)) {
            error = GetLastError();
            safe = FALSE;
            break;
        }
        if (header->AceFlags & INHERIT_ONLY_ACE || header->AceType == ACCESS_DENIED_ACE_TYPE) continue;
        if (header->AceType != ACCESS_ALLOWED_ACE_TYPE) { safe = FALSE; break; }
        ACCESS_ALLOWED_ACE *ace = (ACCESS_ALLOWED_ACE *)header;
        DWORD rights = ace->Mask;
        MapGenericMask(&rights, &mapping);
        safe = !(rights & write_rights) || trusted_sid(&ace->SidStart);
    }
    LocalFree(sd);
    if (!safe) SetLastError(error);
    return safe;
}

static BOOL keep_directory(DirectoryChain *chain, HANDLE dir, DWORD allowed_writes) {
    if (dir == INVALID_HANDLE_VALUE) return FALSE;
    BY_HANDLE_FILE_INFORMATION info;
    DWORD error = ERROR_SUCCESS;
    if (chain->count == ARRAY_SIZE(chain->items)) goto rejected;
    if (!GetFileInformationByHandle(dir, &info)) {
        error = GetLastError();
        goto rejected;
    }
    if ((info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) != FILE_ATTRIBUTE_DIRECTORY)
        goto rejected;
    if (!safe_permissions(dir, allowed_writes)) {
        error = GetLastError();
        goto rejected;
    }
    /* No share-delete while paths below this validated directory are in use. */
    chain->items[chain->count++] = dir;
    return TRUE;

rejected:
    CloseHandle(dir);
    SetLastError(error);
    return FALSE;
}

#define DIR_ACCESS (FILE_LIST_DIRECTORY | READ_CONTROL | FILE_READ_ATTRIBUTES | SYNCHRONIZE)
#define COMMON_DATA_WRITES (FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA)

static HANDLE open_child_shared(HANDLE parent, const WCHAR *name, ACCESS_MASK access, ULONG disposition,
                                ULONG options, PSECURITY_DESCRIPTOR security, ULONG sharing,
                                BOOL *created) {
    UNICODE_STRING child;
    RtlInitUnicodeString(&child, name);
    OBJECT_ATTRIBUTES attributes;
    InitializeObjectAttributes(&attributes, &child, OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE, parent, security);
    IO_STATUS_BLOCK status;
    HANDLE file;
    NTSTATUS opened = NtCreateFile(&file, access | SYNCHRONIZE, &attributes, &status, NULL, FILE_ATTRIBUTE_NORMAL,
                                   sharing, disposition, options | FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT,
                                   NULL, 0);
    if (opened < 0) {
        SetLastError(RtlNtStatusToDosError(opened));
        return INVALID_HANDLE_VALUE;
    }
    if (created) *created = status.Information == FILE_CREATED;
    return file;
}

static HANDLE open_child(HANDLE parent, const WCHAR *name, ACCESS_MASK access, ULONG disposition,
                         ULONG options, PSECURITY_DESCRIPTOR security) {
    return open_child_shared(parent, name, access, disposition, options, security, READ_SHARE, NULL);
}

static HANDLE parent_handle(const DirectoryChain *chain) {
    return chain->items[chain->count - 1];
}

static BOOL append_path(WCHAR *path, const WCHAR *name) {
    size_t len = wcslen(path), add = wcslen(name);
    BOOL separator = len && path[len - 1] != L'\\';
    if (len + separator + add >= MAX_PATH) return FALSE;
    if (separator) path[len++] = L'\\';
    memcpy(path + len, name, (add + 1) * sizeof(WCHAR));
    return TRUE;
}

static BOOL pin_known_path(DirectoryChain *chain, WCHAR *path, DWORD allowed_writes) {
    /* Reject redirected UNC paths, non-canonical components and alternate streams. */
    SetLastError(ERROR_SUCCESS);
    WCHAR *next = NULL;
    if (wcslen(path) < 3 || path[1] != L':' || path[2] != L'\\' ||
        wcspbrk(path + 2, L"/:*?\"<>|") || !((path[0] >= L'A' && path[0] <= L'Z') ||
        (path[0] >= L'a' && path[0] <= L'z'))) return FALSE;
    for (WCHAR *component = path + 3; ; component = next + 1) {
        next = wcschr(component, L'\\');
        size_t len = next ? (size_t)(next - component) : wcslen(component);
        if (!len || component[len - 1] == L'.' || component[len - 1] == L' ') return FALSE;
        if (!next) break;
    }
    WCHAR root[] = {path[0], L':', L'\\', 0};
    if (GetDriveTypeW(root) != DRIVE_FIXED) {
        SetLastError(ERROR_SUCCESS);
        return FALSE;
    }
    HANDLE drive = CreateFileW(root, DIR_ACCESS, READ_SHARE, NULL, OPEN_EXISTING,
                               FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (!keep_directory(chain, drive, FILE_ADD_SUBDIRECTORY)) return FALSE;
    for (WCHAR *component = path + 3; ; component = next + 1) {
        next = wcschr(component, L'\\');
        if (next) *next = 0;
        HANDLE dir = open_child(parent_handle(chain), component, DIR_ACCESS, FILE_OPEN, FILE_DIRECTORY_FILE, NULL);
        if (next) *next = L'\\';
        if (!keep_directory(chain, dir, next ? FILE_ADD_SUBDIRECTORY : allowed_writes)) return FALSE;
        if (!next) return TRUE;
    }
}

static BOOL open_journal_dir(const WCHAR *name, PSECURITY_DESCRIPTOR sd, DWORD allowed_writes) {
    HANDLE dir = open_child(parent_handle(&journal_dirs), name, DIR_ACCESS, FILE_OPEN_IF, FILE_DIRECTORY_FILE, sd);
    return keep_directory(&journal_dirs, dir, allowed_writes);
}

static BOOL create_workspace(DirectoryChain *chain, WCHAR *path, const WCHAR *name) {
    if (!append_path(path, name)) return FALSE;
    PSECURITY_DESCRIPTOR sd = NULL;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(PRIVATE_DIRECTORY, SDDL_REVISION_1, &sd, NULL))
        return FALSE;
    SECURITY_ATTRIBUTES sa = {sizeof(sa), sd, FALSE};
    BOOL created = CreateDirectoryW(path, &sa);
    LocalFree(sd);
    if (!created) return FALSE;
    HANDLE dir = CreateFileW(path, DIR_ACCESS, READ_SHARE, NULL, OPEN_EXISTING,
                             FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (keep_directory(chain, dir, 0)) return TRUE;
    if (created) RemoveDirectoryW(path);
    return FALSE;
}

static const char *const stages[] = {"started", "precheck", "extract", "lock", "backup", "replace",
                                    "service", "verify", "rollback", "finish"};
static const char *const outcomes[] = {"started", "ok", "failed"};
static const char *const codes[] = {"ok", "unknown", "unsupported_host", "busy", "file_missing",
    "checksum_mismatch", "access_denied", "service_registration", "service_start", "local_page_unready",
    "write_failed", "restored", "recovery_unconfirmed"};

static BOOL private_regular_file(HANDLE file, BY_HANDLE_FILE_INFORMATION *info, const WCHAR **check) {
    if (check) *check = L"读取文件属性";
    if (!GetFileInformationByHandle(file, info)) return FALSE;
    if (info->nNumberOfLinks != 1 ||
        (info->dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
        if (check) *check = L"校验普通文件与链接数";
        SetLastError(ERROR_SUCCESS);
        return FALSE;
    }
    if (check) *check = L"校验文件访问权限";
    return safe_permissions(file, 0);
}

static BOOL consume_text(const char **cursor, const char *expected) {
    size_t length = strlen(expected);
    if (strncmp(*cursor, expected, length)) return FALSE;
    *cursor += length;
    return TRUE;
}

static const char *consume_word(const char **cursor, const char *const *words, size_t count) {
    for (size_t i = 0; i < count; ++i) {
        size_t length = strlen(words[i]);
        if (!strncmp(*cursor, words[i], length) && (*cursor)[length] == '"') {
            *cursor += length + 1;
            return words[i];
        }
    }
    return NULL;
}

static BOOL completed_attempt(const char *contents) {
    const char *cursor = contents;
    for (unsigned int seq = 1; seq <= MAX_EVENTS; ++seq) {
        char prefix[40];
        snprintf(prefix, sizeof(prefix), "{\"v\":1,\"seq\":%u,\"stage\":\"", seq);
        if (!consume_text(&cursor, prefix)) return FALSE;
        const char *stage = consume_word(&cursor, stages, ARRAY_SIZE(stages));
        if (!stage || !consume_text(&cursor, ",\"outcome\":\"")) return FALSE;
        const char *outcome = consume_word(&cursor, outcomes, ARRAY_SIZE(outcomes));
        if (!outcome || !consume_text(&cursor, ",\"code\":\"")) return FALSE;
        const char *code = consume_word(&cursor, codes, ARRAY_SIZE(codes));
        if (!code || !consume_text(&cursor, "}\n")) return FALSE;

        if (seq == 1) {
            if (strcmp(stage, "started") || strcmp(outcome, "started") || strcmp(code, "ok")) return FALSE;
        } else if (!strcmp(stage, "started")) {
            return FALSE;
        }
        if (!strcmp(stage, "finish"))
            return !*cursor && ((!strcmp(outcome, "ok") && !strcmp(code, "ok")) ||
                                (!strcmp(outcome, "failed") && strcmp(code, "ok")));
    }
    return FALSE;
}

static BOOL attempt_name(const WCHAR *name) {
    for (size_t i = 0; i < 32; ++i)
        if (name[i] < L'0' || (name[i] > L'9' && name[i] < L'a') || name[i] > L'f') return FALSE;
    return !wcscmp(name + 32, L".jsonl");
}

static BOOL delete_on_close(HANDLE file) {
    FILE_DISPOSITION_INFO disposition = {TRUE};
    return SetFileInformationByHandle(file, FileDispositionInfo, &disposition, sizeof(disposition));
}

static BOOL recyclable_attempt(HANDLE file, BY_HANDLE_FILE_INFORMATION *info) {
    char contents[MAX_BYTES + 1];
    LARGE_INTEGER length;
    DWORD size;
    if (!private_regular_file(file, info, NULL) || (info->dwFileAttributes & FILE_ATTRIBUTE_READONLY) ||
        !GetFileSizeEx(file, &length) ||
        length.QuadPart <= 0 || length.QuadPart > MAX_BYTES ||
        !ReadFile(file, contents, (DWORD)length.QuadPart, &size, NULL) ||
        size != (DWORD)length.QuadPart || memchr(contents, 0, size)) return FALSE;
    contents[size] = 0;
    return completed_attempt(contents);
}

/* Caller holds the journal quota mutex. A live launcher or child holds a
   handle without delete sharing, and a file with no finish is left alone. */
static BOOL reserve_journal_slot(HANDLE directory, const WCHAR *id) {
    union { FILE_ID_BOTH_DIR_INFO alignment; BYTE bytes[8192]; } listing;
    FILE_INFO_BY_HANDLE_CLASS query = FileIdBothDirectoryRestartInfo;
    WCHAR names[MAX_ATTEMPTS][ATTEMPT_NAME_LENGTH + 1];
    unsigned int count = 0, name_count = 0;
    while (GetFileInformationByHandleEx(directory, query, listing.bytes, sizeof(listing.bytes))) {
        FILE_ID_BOTH_DIR_INFO *entry = &listing.alignment;
        for (;;) {
            size_t length = entry->FileNameLength / sizeof(WCHAR);
            if (!(length == 1 && entry->FileName[0] == L'.') &&
                !(length == 2 && entry->FileName[0] == L'.' && entry->FileName[1] == L'.')) {
                if (++count > MAX_ATTEMPTS) return FALSE;
                if (entry->FileNameLength == ATTEMPT_NAME_LENGTH * sizeof(WCHAR) &&
                    !(entry->FileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT))) {
                    memcpy(names[name_count], entry->FileName, entry->FileNameLength);
                    names[name_count][ATTEMPT_NAME_LENGTH] = 0;
                    if (wcslen(names[name_count]) == ATTEMPT_NAME_LENGTH && attempt_name(names[name_count])) {
                        if (!wcsncmp(names[name_count], id, 32)) return FALSE;
                        ++name_count;
                    }
                }
            }
            if (!entry->NextEntryOffset) break;
            entry = (FILE_ID_BOTH_DIR_INFO *)((BYTE *)entry + entry->NextEntryOffset);
        }
        query = FileIdBothDirectoryInfo;
    }
    DWORD error = GetLastError();
    if (error != ERROR_NO_MORE_FILES && (count || error != ERROR_FILE_NOT_FOUND)) return FALSE;
    if (count < MAX_ATTEMPTS) return TRUE;

    HANDLE oldest = INVALID_HANDLE_VALUE;
    FILETIME last_write = {0};
    unsigned int oldest_name = 0;
    for (unsigned int i = 0; i < name_count; ++i) {
        HANDLE file = open_child_shared(directory, names[i], DIR_ACCESS | DELETE,
                                        FILE_OPEN, FILE_NON_DIRECTORY_FILE, NULL, 0, NULL);
        if (file == INVALID_HANDLE_VALUE) continue;
        BY_HANDLE_FILE_INFORMATION info;
        if (recyclable_attempt(file, &info)) {
            LONG comparison = CompareFileTime(&info.ftLastWriteTime, &last_write);
            if (oldest == INVALID_HANDLE_VALUE || comparison < 0 ||
                (comparison == 0 && wcscmp(names[i], names[oldest_name]) < 0)) {
                if (oldest != INVALID_HANDLE_VALUE) CloseHandle(oldest);
                oldest = file;
                last_write = info.ftLastWriteTime;
                oldest_name = i;
                continue;
            }
        }
        CloseHandle(file);
    }
    if (oldest == INVALID_HANDLE_VALUE) return FALSE;
    BOOL removed = delete_on_close(oldest);
    CloseHandle(oldest);
    return removed;
}

static HANDLE lock_journal(PSECURITY_DESCRIPTOR sd) {
    SECURITY_ATTRIBUTES sa = {sizeof(sa), sd, FALSE};
    HANDLE quota = CreateMutexW(&sa, FALSE, L"Global\\KaiwuConnector.InstallJournalQuota");
    if (quota) {
        DWORD wait = WaitForSingleObject(quota, 1000);
        if (wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED) return quota;
        DWORD error = wait == WAIT_FAILED ? GetLastError() : ERROR_TIMEOUT;
        CloseHandle(quota);
        SetLastError(error);
    }
    return NULL;
}

static void unlock_journal(HANDLE quota) {
    ReleaseMutex(quota);
    CloseHandle(quota);
}

static HANDLE create_journal(HANDLE directory, PSECURITY_DESCRIPTOR sd, const WCHAR *id) {
    HANDLE file = INVALID_HANDLE_VALUE;
    HANDLE quota = lock_journal(sd);
    if (quota) {
        WCHAR name[ATTEMPT_NAME_LENGTH + 1];
        wsprintfW(name, L"%ls.jsonl", id);
        if (reserve_journal_slot(directory, id)) {
            file = open_child(directory, name, GENERIC_READ | FILE_APPEND_DATA, FILE_CREATE,
                              FILE_NON_DIRECTORY_FILE | FILE_WRITE_THROUGH, sd);
        }
        unlock_journal(quota);
    }
    return file;
}

static void open_journal(void) {
    WCHAR path[MAX_PATH];
    HRESULT folder = SHGetFolderPathW(NULL, CSIDL_COMMON_APPDATA, NULL, SHGFP_TYPE_CURRENT, path);
    if (FAILED(folder)) {
        DWORD error = HRESULT_FACILITY(folder) == FACILITY_WIN32 ? HRESULT_CODE(folder) : (DWORD)folder;
        logs_failure = (SummaryResult){.step = L"定位 Windows 公共应用数据目录", .error = error};
        return;
    }
    if (!pin_known_path(&journal_dirs, path, COMMON_DATA_WRITES)) {
        logs_failure = (SummaryResult){.step = L"打开并校验公共应用数据目录", .error = GetLastError()};
        return;
    }

    PSECURITY_DESCRIPTOR dir_sd = NULL;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(PRIVATE_DIRECTORY, SDDL_REVISION_1, &dir_sd, NULL)) {
        logs_failure = (SummaryResult){.step = L"构造日志目录访问权限", .error = GetLastError()};
        return;
    }
    /* Common AppData allows standard users to create children and change attributes.
       All journal operations use validated directory handles and never resolve that path again. */
    BOOL safe = open_journal_dir(L"Kaiwu", dir_sd, COMMON_DATA_WRITES) &&
                open_journal_dir(L"Connector", dir_sd, COMMON_DATA_WRITES) &&
                open_journal_dir(L"logs", dir_sd, COMMON_DATA_WRITES);
    if (safe) {
        logs_directory = parent_handle(&journal_dirs);
        safe = open_journal_dir(L"install-attempts", dir_sd, 0);
    } else {
        logs_failure = (SummaryResult){.step = L"打开并校验日志目录", .error = GetLastError()};
    }
    LocalFree(dir_sd);
    if (!safe) return;

    PSECURITY_DESCRIPTOR sd = NULL;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(PRIVATE_FILE, SDDL_REVISION_1, &sd, NULL)) return;
    journal = create_journal(parent_handle(&journal_dirs), sd, attempt);
    LocalFree(sd);
}

static const char *allowed(const WCHAR *value, const char *const *words, size_t count) {
    char ascii[32];
    int length = WideCharToMultiByte(CP_UTF8, 0, value, -1, ascii, sizeof(ascii), NULL, NULL);
    if (length) for (size_t i = 0; i < count; ++i) if (!strcmp(ascii, words[i])) return words[i];
    return NULL;
}

static BOOL append_event(const WCHAR *stage_arg, const WCHAR *outcome_arg, const WCHAR *code_arg) {
    if (journal == INVALID_HANDLE_VALUE) return FALSE;
    const char *stage = allowed(stage_arg, stages, ARRAY_SIZE(stages));
    const char *outcome = allowed(outcome_arg, outcomes, ARRAY_SIZE(outcomes));
    const char *code = allowed(code_arg, codes, ARRAY_SIZE(codes));
    if (!stage || !outcome || !code) return FALSE;

    char previous[MAX_BYTES], line[160];
    LARGE_INTEGER length, start = {0};
    DWORD size, written;
    if (!GetFileSizeEx(journal, &length) || length.QuadPart >= MAX_BYTES ||
        !SetFilePointerEx(journal, start, NULL, FILE_BEGIN) ||
        !ReadFile(journal, previous, (DWORD)length.QuadPart, &size, NULL) || size != (DWORD)length.QuadPart ||
        (size && previous[size - 1] != '\n')) return FALSE;
    unsigned int seq = 1;
    for (DWORD i = 0; i < size; ++i) if (previous[i] == '\n') ++seq;
    if (seq > MAX_EVENTS) return FALSE;
    int count = snprintf(line, sizeof(line), "{\"v\":1,\"seq\":%u,\"stage\":\"%s\",\"outcome\":\"%s\",\"code\":\"%s\"}\n",
                         seq, stage, outcome, code);
    return count > 0 && count < (int)sizeof(line) && size + (DWORD)count <= MAX_BYTES &&
           WriteFile(journal, line, (DWORD)count, &written, NULL) && written == (DWORD)count;
}

EXPORT(Begin) {
    static const WCHAR hex[] = L"0123456789abcdef";
    BYTE random[16];
    NTSTATUS rng = BCryptGenRandom(NULL, random, sizeof(random), BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    attempt_error = rng ? RtlNtStatusToDosError(rng) : ERROR_SUCCESS;
    if (rng == 0) {
        for (size_t i = 0; i < ARRAY_SIZE(random); ++i) {
            attempt[i * 2] = hex[random[i] >> 4];
            attempt[i * 2 + 1] = hex[random[i] & 15];
        }
    }
    if (*attempt && ConvertStringSidToSidW(L"S-1-5-32-544", &admins) &&
        ConvertStringSidToSidW(L"S-1-5-18", &system_account) &&
        ConvertStringSidToSidW(L"S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464", &trusted_installer)) {
        open_journal();
        if (logs_directory == INVALID_HANDLE_VALUE) close_chain(&journal_dirs);
    } else if (*attempt) {
        logs_failure = (SummaryResult){.step = L"初始化日志权限校验", .error = GetLastError()};
    }
    journal_started = append_event(L"started", L"started", L"ok");
}

EXPORT(Record) {
    WCHAR stage[64], outcome[64], code[64];
    if (pop(stack, stage, ARRAY_SIZE(stage)) && pop(stack, outcome, ARRAY_SIZE(outcome)) &&
        pop(stack, code, ARRAY_SIZE(code))) append_event(stage, outcome, code);
}

static void finish_journal(const WCHAR *code) {
    BOOL finished = append_event(L"finish", !wcscmp(code, L"ok") ? L"ok" : L"failed", code);
    journal_finished = journal_started && finished;
}

EXPORT(Finish) {
    WCHAR code[32];
    if (pop(stack, code, ARRAY_SIZE(code))) finish_journal(code);
}

typedef struct { const WCHAR *key, *label; } FailureLabel;

static const WCHAR *failure_label(const WCHAR *key, const FailureLabel *labels, size_t count,
                                  const WCHAR *fallback) {
    for (size_t i = 0; i < count; ++i)
        if (!wcscmp(key, labels[i].key)) return labels[i].label;
    return fallback;
}

static BOOL format_failure(WCHAR *summary, const WCHAR *stage_code, const WCHAR *reason_code,
                           const WCHAR *file_code, const WCHAR *result_code) {
    static const FailureLabel stages_zh[] = {
        {L"precheck", L"检查系统和 Windows PowerShell"},
        {L"lock", L"取得安装事务锁"},
        {L"extract", L"解包安装文件"},
        {L"verify", L"保存配置入口和卸载登记"},
        {L"source_precheck", L"校验解包后的安装文件"},
        {L"transaction_lock", L"取得安装事务锁"},
        {L"backup_prepare", L"检查并备份已有安装"},
        {L"quiesce", L"停止原服务和相关进程"},
        {L"pre_change_verification", L"替换程序前再次校验安装文件"},
        {L"replace_install", L"替换程序或配置服务"},
        {L"final_verification", L"最终校验并记录安装状态"},
    };
    static const FailureLabel reasons_zh[] = {
        {L"host_x64", L"本安装包仅支持 64 位 Windows。"},
        {L"host_version", L"本安装包要求 Windows 10 或 Windows Server 2016 及以上版本。"},
        {L"host_powershell_missing", L"系统缺少 64 位 Windows PowerShell。"},
        {L"host_powershell_version", L"无法启动 64 位 Windows PowerShell 5.1 及以上版本。"},
        {L"unsupported_host", L"安装脚本需要 64 位 Windows PowerShell。"},
        {L"lock_busy", L"已有开物安装或卸载任务正在运行。"},
        {L"lock_unavailable", L"无法取得或确认安装事务锁。"},
        {L"workspace_unavailable", L"无法建立安全的安装临时目录。"},
        {L"extract_write_failed", L"无法完整解包安装文件。"},
        {L"install_script_missing", L"解包后找不到安装脚本。"},
        {L"manifest_missing", L"缺少校验清单 SHA256SUMS。"},
        {L"manifest_invalid", L"校验清单 SHA256SUMS 含非法路径。"},
        {L"file_missing", L"未找到安装包清单中的文件。"},
        {L"file_unlisted", L"校验清单 SHA256SUMS 缺少安装文件的记录。"},
        {L"checksum_mismatch", L"安装包中的文件未通过 SHA256 校验。"},
        {L"service_registration", L"无法注册连接器服务。"},
        {L"service_start", L"无法启动连接器服务。"},
        {L"local_page_unready", L"本机配置页面未在规定时间内就绪。"},
        {L"access_denied", L"系统拒绝本次操作所需的访问。"},
        {L"busy", L"已有安装或卸载事务正在运行。"},
        {L"result_unavailable", L"无法确认安装脚本的结果。"},
        {L"maintenance_write_failed", L"无法保存配置入口或卸载登记。"},
        {L"interrupted", L"安装程序中途退出。"},
    };
    static const FailureLabel results_zh[] = {
        {L"result=PRECHECK_FAILED", L"未开始更改已有安装。"},
        {L"result=FAILED_RESTORED", L"已核验恢复安装前的程序和配置。业务连通性待确认。"},
        {L"result=FAILED_RECOVERY_UNCONFIRMED", L"未能确认已恢复安装前的状态。"},
        {L"result=FAILED_NO_PRIOR_CLEANED", L"已清理本次新增的受管文件。"},
        {L"result=FAILED_NO_PRIOR_CLEANUP_UNCONFIRMED", L"未能确认已清理本次新增的受管文件。"},
        {L"not_started", L"本次安装未开始更改程序或服务。"},
        {L"maintenance_failed", L"连接器服务已安装，配置入口或卸载登记未确认。"},
    };
    static const FailureLabel files[] = {
        {L"kaiwu-connector.exe", L"kaiwu-connector.exe"},
        {L"frpc.exe", L"frpc.exe"},
        {L"service-handoff.ps1", L"service-handoff.ps1"},
    };
    const WCHAR *stage = failure_label(stage_code, stages_zh, ARRAY_SIZE(stages_zh), L"无法确认");
    const WCHAR *reason = failure_label(reason_code, reasons_zh, ARRAY_SIZE(reasons_zh), L"无法确认具体原因。");
    const WCHAR *recovery = failure_label(result_code, results_zh, ARRAY_SIZE(results_zh), L"恢复或清理状态无法确认。");
    const WCHAR *file = failure_label(file_code, files, ARRAY_SIZE(files), L"");
    if (wcscmp(reason_code, L"file_missing") && wcscmp(reason_code, L"file_unlisted") &&
        wcscmp(reason_code, L"checksum_mismatch")) file = L"";
    int length = _snwprintf(summary, SUMMARY_TEXT_LENGTH,
        L"安装失败\r\n安装编号：%ls\r\n失败阶段：%ls\r\n原因：%ls%ls%ls\r\n恢复结果：%ls\r\n",
        *attempt ? attempt : L"未生成", stage, reason, *file ? L" 涉及文件：" : L"", file, recovery);
    return length > 0 && length < SUMMARY_TEXT_LENGTH;
}

static SummaryResult write_summary(HANDLE file, BOOL replace, const WCHAR *summary) {
    WCHAR bom = 0xfeff;
    DWORD written, bytes = (DWORD)(wcslen(summary) * sizeof(WCHAR));
    if (!SetEndOfFile(file))
        return (SummaryResult){.step = L"截断暂存摘要", .error = GetLastError()};
    if (!WriteFile(file, &bom, sizeof(bom), &written, NULL))
        return (SummaryResult){.step = L"写入摘要编码标记", .error = GetLastError()};
    if (written != sizeof(bom))
        return (SummaryResult){.step = L"摘要编码标记写入不完整"};
    if (!WriteFile(file, summary, bytes, &written, NULL))
        return (SummaryResult){.step = L"写入摘要正文", .error = GetLastError()};
    if (written != bytes)
        return (SummaryResult){.step = L"摘要正文写入不完整"};
    if (!FlushFileBuffers(file))
        return (SummaryResult){.step = L"提交暂存摘要写入", .error = GetLastError()};

    union {
        FILE_RENAME_INFORMATION info;
        BYTE bytes[sizeof(FILE_RENAME_INFORMATION) + sizeof(SUMMARY_NAME)];
    } rename = {0};
    /* A simple name with no root handle renames within the open file's directory. */
    rename.info.ReplaceIfExists = replace;
    rename.info.FileNameLength = sizeof(SUMMARY_NAME) - sizeof(WCHAR);
    memcpy(rename.info.FileName, SUMMARY_NAME, sizeof(SUMMARY_NAME));
    IO_STATUS_BLOCK status;
    NTSTATUS renamed = NtSetInformationFile(file, &status, &rename, sizeof(rename), FileRenameInformation);
    if (renamed < 0)
        return (SummaryResult){.step = L"发布正式摘要", .error = RtlNtStatusToDosError(renamed)};
    return (SummaryResult){0};
}

static SummaryResult replace_summary(HANDLE directory, PSECURITY_DESCRIPTOR sd, const WCHAR *summary) {
    /* The caller holds the quota mutex and the logs directory has no untrusted
       writers. Inspect any previous destination without following links. */
    HANDLE old = open_child(directory, SUMMARY_NAME, READ_CONTROL | FILE_READ_ATTRIBUTES,
                            FILE_OPEN, FILE_NON_DIRECTORY_FILE, NULL);
    BY_HANDLE_FILE_INFORMATION info;
    BOOL replace = old != INVALID_HANDLE_VALUE;
    const WCHAR *check;
    if (replace) {
        BOOL safe = private_regular_file(old, &info, &check);
        DWORD error = safe ? ERROR_SUCCESS : GetLastError();
        CloseHandle(old);
        if (!safe) return (SummaryResult){.step = check, .file = L"原摘要", .error = error};
    } else {
        DWORD error = GetLastError();
        if (error != ERROR_FILE_NOT_FOUND)
            return (SummaryResult){.step = L"打开原摘要", .error = error};
    }

    /* A single reserved staging name also bounds leftovers after process death.
       Recover a stale staging file only after validating it by handle. */
    BOOL created;
    HANDLE file = open_child_shared(directory, SUMMARY_PENDING, GENERIC_WRITE | READ_CONTROL | DELETE,
                                    FILE_OPEN_IF, FILE_NON_DIRECTORY_FILE | FILE_WRITE_THROUGH, sd, 0, &created);
    if (file == INVALID_HANDLE_VALUE)
        return (SummaryResult){.step = L"打开暂存摘要", .error = GetLastError()};

    SummaryResult result;
    BOOL validated = private_regular_file(file, &info, &check);
    if (validated) {
        result = write_summary(file, replace, summary);
    } else {
        result = (SummaryResult){.step = check, .file = L"暂存摘要", .error = GetLastError()};
    }
    /* Only remove the file we just created or the staging file we validated. */
    if (result.step && (created || validated) && !delete_on_close(file))
        result.cleanup_error = GetLastError();
    CloseHandle(file);
    return result;
}

static SummaryResult save_summary(const WCHAR *summary) {
    if (!*attempt) return (SummaryResult){.step = L"生成安装编号", .error = attempt_error};
    if (logs_directory == INVALID_HANDLE_VALUE) return logs_failure;
    if (!safe_permissions(logs_directory, 0))
        return (SummaryResult){.step = L"复核日志目录访问权限", .error = GetLastError()};
    PSECURITY_DESCRIPTOR sd = NULL;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(PRIVATE_FILE, SDDL_REVISION_1, &sd, NULL))
        return (SummaryResult){.step = L"构造摘要文件访问权限", .error = GetLastError()};
    SummaryResult result;
    HANDLE quota = lock_journal(sd);
    if (quota) {
        result = replace_summary(logs_directory, sd, summary);
        unlock_journal(quota);
    } else {
        result = (SummaryResult){.step = L"取得摘要保存锁", .error = GetLastError()};
    }
    LocalFree(sd);
    return result;
}

static void failure_message(WCHAR *message, size_t capacity, const WCHAR *summary, SummaryResult result) {
    WCHAR failure[192] = L"", cleanup[64] = L"", event[192] = L"";
    if (result.step) {
        const WCHAR *file = result.file ? result.file : L"";
        const WCHAR *separator = result.file ? L"：" : L"";
        if (result.cleanup_error)
            _snwprintf(cleanup, ARRAY_SIZE(cleanup), L" 清理暂存摘要失败，系统错误码 %lu（0x%08lX）。",
                       result.cleanup_error, result.cleanup_error);
        if (result.error)
            _snwprintf(failure, ARRAY_SIZE(failure), L"未能保存本次摘要。%ls%ls%ls，系统错误码 %lu（0x%08lX）。%ls",
                       file, separator, result.step, result.error, result.error, cleanup);
        else
            _snwprintf(failure, ARRAY_SIZE(failure), L"未能保存本次摘要。%ls%ls%ls（系统未返回错误码）。%ls",
                       file, separator, result.step, cleanup);
    }
    if (journal_finished)
        _snwprintf(event, ARRAY_SIZE(event),
                   L"本次事件文件已写入结束标记。\r\n位置：Windows 公共应用数据目录下的 "
                   L"Kaiwu\\Connector\\logs\\install-attempts\\%ls.jsonl", attempt);

    _snwprintf(message, capacity,
        L"%ls\r\n%ls\r\n%ls\r\n请复制此窗口内容并转发给技术支持（Ctrl+C）。", summary,
        result.step ? failure : L"已保存本次摘要。此文件保存最近一次失败，请先核对安装编号。\r\n"
                                L"位置：Windows 公共应用数据目录下的 Kaiwu\\Connector\\logs\\" SUMMARY_NAME,
        journal_finished ? event : L"本次事件文件未能确认写完。");
    message[capacity - 1] = 0;
}

EXPORT(ExplainFailure) {
    WCHAR stage[64], reason[64], file[64], recovery[64];
    WCHAR summary[SUMMARY_TEXT_LENGTH], message[SUMMARY_TEXT_LENGTH + 512];
    if (!pop(stack, stage, ARRAY_SIZE(stage)) || !pop(stack, reason, ARRAY_SIZE(reason)) ||
        !pop(stack, file, ARRAY_SIZE(file)) || !pop(stack, recovery, ARRAY_SIZE(recovery)) ||
        !format_failure(summary, stage, reason, file, recovery)) {
        push(stack, size, L"安装失败。未能生成本次摘要，请复制此窗口内容并转发给技术支持（Ctrl+C）。");
        return;
    }
    SummaryResult result = save_summary(summary);
    failure_message(message, ARRAY_SIZE(message), summary, result);
    push(stack, size, message);
}

EXPORT(Workspace) {
    WCHAR name[36];
    if (*attempt && admins && system_account && trusted_installer) {
        wsprintfW(name, L"kw-%ls", attempt);
        if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_PROGRAM_FILES, NULL, SHGFP_TYPE_CURRENT, workspace)) &&
            wcslen(workspace) < MAX_PATH - 128 && pin_known_path(&workspace_dirs, workspace, 0) &&
            create_workspace(&workspace_dirs, workspace, name)) {
            push(stack, size, workspace);
            return;
        }
        close_chain(&workspace_dirs);
    }
    *workspace = 0;
    push(stack, size, L"");
}

static BOOL start_installer(const WCHAR *powershell, const WCHAR *source, const WCHAR *argument,
                            STARTUPINFOEXW *startup, DWORD flags, PROCESS_INFORMATION *process) {
    WCHAR command[5 * MAX_PATH];
    int length = _snwprintf(command, ARRAY_SIZE(command),
        L"\"%ls\" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"%ls\\install.ps1\" "
        L"-ResultPath \"%ls\\result.txt\" -FailureDetailsPath \"%ls\\failure.txt\" -FailureDetailsCodes -UsePackageTemp%ls",
        powershell, source, source, source, argument);
    return length > 0 && (size_t)length < ARRAY_SIZE(command) &&
           CreateProcessW(powershell, command, NULL, NULL, startup->lpAttributeList != NULL, flags, NULL,
                          source, &startup->StartupInfo, process);
}

EXPORT(RunInstaller) {
    WCHAR powershell[MAX_PATH + 1], source[MAX_PATH + 1], result[16];
    if (!pop(stack, powershell, ARRAY_SIZE(powershell)) || !pop(stack, source, ARRAY_SIZE(source)) ||
        !*powershell || !*source || wcslen(powershell) >= MAX_PATH || wcslen(source) >= MAX_PATH) {
        push(stack, size, L"error");
        return;
    }

    /* Inherit only the append-only journal handle. PowerShell never opens a diagnostic path. */
    HANDLE child_journal = NULL;
    STARTUPINFOEXW startup = {0};
    startup.StartupInfo.cb = sizeof(STARTUPINFOW);
    DWORD flags = CREATE_NO_WINDOW;
    WCHAR argument[48] = L"";
    if (journal != INVALID_HANDLE_VALUE && DuplicateHandle(GetCurrentProcess(), journal, GetCurrentProcess(),
            &child_journal, 0, TRUE, DUPLICATE_SAME_ACCESS)) {
        SIZE_T bytes = 0;
        InitializeProcThreadAttributeList(NULL, 1, 0, &bytes);
        LPPROC_THREAD_ATTRIBUTE_LIST attributes = LocalAlloc(LMEM_FIXED, bytes);
        if (attributes && InitializeProcThreadAttributeList(attributes, 1, 0, &bytes)) {
            if (UpdateProcThreadAttribute(attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                          &child_journal, sizeof(child_journal), NULL, NULL)) {
                startup.lpAttributeList = attributes;
                startup.StartupInfo.cb = sizeof(startup);
                flags |= EXTENDED_STARTUPINFO_PRESENT;
                wsprintfW(argument, L" -AttemptHandle %lu", (DWORD)(ULONG_PTR)child_journal);
            } else {
                DeleteProcThreadAttributeList(attributes);
            }
        }
        if (!startup.lpAttributeList) LocalFree(attributes);
    }

    PROCESS_INFORMATION process;
    BOOL started = start_installer(powershell, source, argument, &startup, flags, &process);
    if (startup.lpAttributeList) {
        DeleteProcThreadAttributeList(startup.lpAttributeList);
        LocalFree(startup.lpAttributeList);
        if (!started) {
            startup.lpAttributeList = NULL;
            startup.StartupInfo.cb = sizeof(STARTUPINFOW);
            started = start_installer(powershell, source, L"", &startup, CREATE_NO_WINDOW, &process);
        }
    }
    if (child_journal) CloseHandle(child_journal);
    if (!started) {
        push(stack, size, L"error");
        return;
    }
    CloseHandle(process.hThread);
    DWORD exit_code;
    BOOL finished = WaitForSingleObject(process.hProcess, INFINITE) == WAIT_OBJECT_0 &&
                    GetExitCodeProcess(process.hProcess, &exit_code);
    CloseHandle(process.hProcess);
    if (finished) wsprintfW(result, L"%lu", exit_code);
    push(stack, size, finished ? result : L"error");
}

EXPORT(GuardProgramWrites) {
    WCHAR path[MAX_PATH + 1];
    static const WCHAR *const destinations[] = {L"open-config.cmd", L"uninstall.exe"};
    BOOL safe = admins && system_account && trusted_installer && pop(stack, path, ARRAY_SIZE(path)) &&
                wcslen(path) < MAX_PATH && pin_known_path(&program_dirs, path, 0);
    size_t len = safe ? wcslen(path) : 0;
    for (size_t i = 0; safe && i < ARRAY_SIZE(destinations); ++i) {
        path[len] = 0;
        safe = append_path(path, destinations[i]);
        if (!safe) break;
        HANDLE file = CreateFileW(path, READ_CONTROL | FILE_READ_ATTRIBUTES, READ_SHARE, NULL, OPEN_EXISTING,
                                  FILE_FLAG_OPEN_REPARSE_POINT, NULL);
        if (file == INVALID_HANDLE_VALUE) {
            safe = GetLastError() == ERROR_FILE_NOT_FOUND;
            continue;
        }
        BY_HANDLE_FILE_INFORMATION info;
        safe = GetFileInformationByHandle(file, &info) && info.nNumberOfLinks == 1 &&
               !(info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) &&
               safe_permissions(file, 0);
        if (safe && program_dirs.count < ARRAY_SIZE(program_dirs.items)) {
            program_dirs.items[program_dirs.count++] = file;
        } else {
            CloseHandle(file);
            safe = FALSE;
        }
    }
    if (!safe) close_chain(&program_dirs);
    push(stack, size, safe ? L"1" : L"0");
}

EXPORT(ReleaseWorkspace) {
    close_chain(&workspace_dirs);
}

EXPORT(Close) {
    if (journal != INVALID_HANDLE_VALUE) {
        CloseHandle(journal);
        journal = INVALID_HANDLE_VALUE;
    }
    close_chain(&program_dirs);
    close_chain(&workspace_dirs);
    close_chain(&journal_dirs);
    logs_directory = INVALID_HANDLE_VALUE;
    LocalFree(trusted_installer);
    LocalFree(system_account);
    LocalFree(admins);
    trusted_installer = system_account = admins = NULL;
}
