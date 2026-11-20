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
#define READ_SHARE (FILE_SHARE_READ | FILE_SHARE_WRITE)
#define PRIVATE_DIRECTORY L"O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
#define PRIVATE_FILE L"O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)"

typedef struct { HANDLE items[MAX_PATH / 2]; size_t count; } DirectoryChain;
static DirectoryChain journal_dirs, workspace_dirs, program_dirs;
static HANDLE journal = INVALID_HANDLE_VALUE;
static WCHAR attempt[33], workspace[MAX_PATH];
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
    if (status != ERROR_SUCCESS) return FALSE;

    DWORD write_rights = DELETE | WRITE_DAC | WRITE_OWNER | FILE_DELETE_CHILD | FILE_WRITE_EA |
                         FILE_WRITE_ATTRIBUTES | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY;
    write_rights &= ~allowed_writes;
    GENERIC_MAPPING mapping = {FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS};
    BOOL safe = trusted_sid(owner) && dacl;
    for (DWORD i = 0; safe && i < dacl->AceCount; ++i) {
        ACE_HEADER *header = NULL;
        if (!GetAce(dacl, i, (void **)&header)) { safe = FALSE; break; }
        if (header->AceFlags & INHERIT_ONLY_ACE || header->AceType == ACCESS_DENIED_ACE_TYPE) continue;
        if (header->AceType != ACCESS_ALLOWED_ACE_TYPE) { safe = FALSE; break; }
        ACCESS_ALLOWED_ACE *ace = (ACCESS_ALLOWED_ACE *)header;
        DWORD rights = ace->Mask;
        MapGenericMask(&rights, &mapping);
        safe = !(rights & write_rights) || trusted_sid(&ace->SidStart);
    }
    LocalFree(sd);
    return safe;
}

static BOOL keep_directory(DirectoryChain *chain, HANDLE dir, DWORD allowed_writes) {
    if (dir == INVALID_HANDLE_VALUE) return FALSE;
    BY_HANDLE_FILE_INFORMATION info;
    if (chain->count == ARRAY_SIZE(chain->items) || !GetFileInformationByHandle(dir, &info) ||
        (info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) != FILE_ATTRIBUTE_DIRECTORY ||
        !safe_permissions(dir, allowed_writes)) {
        CloseHandle(dir);
        return FALSE;
    }
    /* No share-delete while paths below this validated directory are in use. */
    chain->items[chain->count++] = dir;
    return TRUE;
}

#define DIR_ACCESS (FILE_LIST_DIRECTORY | READ_CONTROL | FILE_READ_ATTRIBUTES | SYNCHRONIZE)
#define COMMON_DATA_WRITES (FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA)

static HANDLE open_child(HANDLE parent, const WCHAR *name, ACCESS_MASK access, ULONG disposition,
                         ULONG options, PSECURITY_DESCRIPTOR security) {
    UNICODE_STRING child;
    RtlInitUnicodeString(&child, name);
    OBJECT_ATTRIBUTES attributes;
    InitializeObjectAttributes(&attributes, &child, OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE, parent, security);
    IO_STATUS_BLOCK status;
    HANDLE file;
    if (NtCreateFile(&file, access | SYNCHRONIZE, &attributes, &status, NULL, FILE_ATTRIBUTE_NORMAL,
                     READ_SHARE, disposition, options | FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT,
                     NULL, 0) < 0) return INVALID_HANDLE_VALUE;
    return file;
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
    if (GetDriveTypeW(root) != DRIVE_FIXED) return FALSE;
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

static BOOL journal_has_capacity(HANDLE directory) {
    union { FILE_ID_BOTH_DIR_INFO alignment; BYTE bytes[8192]; } listing;
    FILE_INFO_BY_HANDLE_CLASS query = FileIdBothDirectoryRestartInfo;
    unsigned int count = 0;
    while (GetFileInformationByHandleEx(directory, query, listing.bytes, sizeof(listing.bytes))) {
        FILE_ID_BOTH_DIR_INFO *entry = &listing.alignment;
        for (;;) {
            if (!((entry->FileNameLength == sizeof(WCHAR) && entry->FileName[0] == L'.') ||
                  (entry->FileNameLength == 2 * sizeof(WCHAR) && entry->FileName[0] == L'.' && entry->FileName[1] == L'.')) &&
                ++count >= MAX_ATTEMPTS) return FALSE;
            if (!entry->NextEntryOffset) break;
            entry = (FILE_ID_BOTH_DIR_INFO *)((BYTE *)entry + entry->NextEntryOffset);
        }
        query = FileIdBothDirectoryInfo;
    }
    return GetLastError() == ERROR_NO_MORE_FILES || (count == 0 && GetLastError() == ERROR_FILE_NOT_FOUND);
}

static void open_journal(void) {
    WCHAR path[MAX_PATH];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_COMMON_APPDATA, NULL, SHGFP_TYPE_CURRENT, path)) ||
        !pin_known_path(&journal_dirs, path, COMMON_DATA_WRITES)) return;

    PSECURITY_DESCRIPTOR dir_sd = NULL;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(PRIVATE_DIRECTORY, SDDL_REVISION_1, &dir_sd, NULL))
        return;
    /* Common AppData allows standard users to create children and change attributes.
       All journal operations use validated directory handles and never resolve that path again. */
    BOOL safe = open_journal_dir(L"Kaiwu", dir_sd, COMMON_DATA_WRITES) &&
                open_journal_dir(L"Connector", dir_sd, COMMON_DATA_WRITES) &&
                open_journal_dir(L"logs", dir_sd, COMMON_DATA_WRITES) &&
                open_journal_dir(L"install-attempts", dir_sd, 0);
    LocalFree(dir_sd);
    if (!safe) return;

    /* Serialize the cap check and CREATE_NEW across sessions, independent of setup's lock. */
    PSECURITY_DESCRIPTOR sd = NULL;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(PRIVATE_FILE, SDDL_REVISION_1, &sd, NULL)) return;
    SECURITY_ATTRIBUTES sa = {sizeof(sa), sd, FALSE};
    HANDLE quota = CreateMutexW(&sa, FALSE, L"Global\\KaiwuConnector.InstallJournalQuota");
    if (quota) {
        DWORD wait = WaitForSingleObject(quota, 1000);
        if (wait == WAIT_OBJECT_0 || wait == WAIT_ABANDONED) {
            WCHAR name[39];
            wsprintfW(name, L"%ls.jsonl", attempt);
            HANDLE directory = parent_handle(&journal_dirs);
            if (journal_has_capacity(directory)) {
                journal = open_child(directory, name, GENERIC_READ | FILE_APPEND_DATA, FILE_CREATE,
                                     FILE_NON_DIRECTORY_FILE | FILE_WRITE_THROUGH, sd);
            }
            ReleaseMutex(quota);
        }
        CloseHandle(quota);
    }
    LocalFree(sd);
}

static const char *allowed(const WCHAR *value, const char *const *words, size_t count) {
    char ascii[32];
    int length = WideCharToMultiByte(CP_UTF8, 0, value, -1, ascii, sizeof(ascii), NULL, NULL);
    if (length) for (size_t i = 0; i < count; ++i) if (!strcmp(ascii, words[i])) return words[i];
    return NULL;
}

static BOOL append_event(const WCHAR *stage_arg, const WCHAR *outcome_arg, const WCHAR *code_arg) {
    static const char *const stages[] = {"started", "precheck", "extract", "lock", "backup", "replace",
                                          "service", "verify", "rollback", "finish"};
    static const char *const outcomes[] = {"started", "ok", "failed"};
    static const char *const codes[] = {"ok", "unknown", "unsupported_host", "busy", "file_missing",
        "checksum_mismatch", "access_denied", "service_registration", "service_start", "local_page_unready",
        "write_failed", "restored", "recovery_unconfirmed"};
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
    if (BCryptGenRandom(NULL, random, sizeof(random), BCRYPT_USE_SYSTEM_PREFERRED_RNG) == 0) {
        for (size_t i = 0; i < ARRAY_SIZE(random); ++i) {
            attempt[i * 2] = hex[random[i] >> 4];
            attempt[i * 2 + 1] = hex[random[i] & 15];
        }
    }
    if (*attempt && ConvertStringSidToSidW(L"S-1-5-32-544", &admins) &&
        ConvertStringSidToSidW(L"S-1-5-18", &system_account) &&
        ConvertStringSidToSidW(L"S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464", &trusted_installer)) {
        open_journal();
        if (journal == INVALID_HANDLE_VALUE) close_chain(&journal_dirs);
    }
    push(stack, size, append_event(L"started", L"started", L"ok") ? L"1" : L"0");
    push(stack, size, attempt);
}

EXPORT(Record) {
    WCHAR stage[64], outcome[64], code[64];
    if (pop(stack, stage, ARRAY_SIZE(stage)) && pop(stack, outcome, ARRAY_SIZE(outcome)) &&
        pop(stack, code, ARRAY_SIZE(code))) append_event(stage, outcome, code);
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
        L"-ResultPath \"%ls\\result.txt\" -FailureDetailsPath \"%ls\\failure.txt\" -UsePackageTemp%ls",
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
    LocalFree(trusted_installer);
    LocalFree(system_account);
    LocalFree(admins);
    trusted_installer = system_account = admins = NULL;
}
