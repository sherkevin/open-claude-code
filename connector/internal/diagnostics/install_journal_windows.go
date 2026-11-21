package diagnostics

import (
	"os"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const installReadFlags = os.O_RDONLY

func installCreated(file *os.File) time.Time {
	handle := windows.Handle(file.Fd())
	var info windows.ByHandleFileInformation
	if err := windows.GetFileInformationByHandle(handle, &info); err != nil ||
		info.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 || info.NumberOfLinks != 1 ||
		!installACLTrusted(handle) {
		return time.Time{}
	}
	return time.Unix(0, info.CreationTime.Nanoseconds())
}

func installDirectoryTrusted(file *os.File, before os.FileInfo) bool {
	info, err := file.Stat()
	if err != nil || !info.IsDir() || !os.SameFile(info, before) {
		return false
	}
	handle := windows.Handle(file.Fd())
	var attrs windows.ByHandleFileInformation
	return windows.GetFileInformationByHandle(handle, &attrs) == nil &&
		attrs.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT == 0 && installACLTrusted(handle)
}

func installACLTrusted(handle windows.Handle) bool {
	sd, err := windows.GetSecurityInfo(handle, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return false
	}
	owner, _, err := sd.Owner()
	if err != nil || !installTrustee(owner) {
		return false
	}
	acl, _, err := sd.DACL()
	if err != nil || acl == nil {
		return false
	}
	const deleteChild = 0x40
	const write = windows.GENERIC_ALL | windows.GENERIC_WRITE | windows.DELETE | windows.WRITE_DAC |
		windows.WRITE_OWNER | windows.FILE_WRITE_DATA | windows.FILE_APPEND_DATA |
		windows.FILE_WRITE_EA | windows.FILE_WRITE_ATTRIBUTES | deleteChild
	for i := uint32(0); i < uint32(acl.AceCount); i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if windows.GetAce(acl, i, &ace) != nil {
			return false
		}
		if ace.Header.AceFlags&windows.INHERIT_ONLY_ACE != 0 || ace.Header.AceType == windows.ACCESS_DENIED_ACE_TYPE {
			continue
		}
		if ace.Header.AceType != windows.ACCESS_ALLOWED_ACE_TYPE ||
			ace.Mask&write != 0 && !installTrustee((*windows.SID)(unsafe.Pointer(&ace.SidStart))) {
			return false
		}
	}
	return true
}

func installTrustee(sid *windows.SID) bool {
	if sid == nil {
		return false
	}
	switch sid.String() {
	case "S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464":
		return true // Local System, Administrators, Windows TrustedInstaller.
	default:
		return false
	}
}
