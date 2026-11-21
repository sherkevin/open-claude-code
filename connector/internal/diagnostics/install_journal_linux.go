package diagnostics

import (
	"os"
	"time"

	"golang.org/x/sys/unix"
)

const installReadFlags = os.O_RDONLY | unix.O_NONBLOCK | unix.O_NOFOLLOW

func installCreated(file *os.File) time.Time {
	var info unix.Statx_t
	if err := unix.Statx(int(file.Fd()), "", unix.AT_EMPTY_PATH, unix.STATX_BTIME|unix.STATX_NLINK|unix.STATX_MODE, &info); err != nil ||
		info.Mask&unix.STATX_BTIME == 0 || info.Nlink != 1 || info.Mode&0o022 != 0 {
		return time.Time{}
	}
	return time.Unix(info.Btime.Sec, int64(info.Btime.Nsec))
}

func installDirectoryTrusted(file *os.File, before os.FileInfo) bool {
	info, err := file.Stat()
	return err == nil && info.IsDir() && os.SameFile(info, before) && info.Mode().Perm()&0o022 == 0
}
