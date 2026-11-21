package diagnostics

import (
	"context"
	"errors"
	"os"
	"time"
)

// ErrReportingBusy means another process holds the diagnostic queue lock.
var ErrReportingBusy = errors.New("连接器或另一条诊断命令正在使用本机诊断")

// LockReporting serializes the serving connector and the offline diagnostic
// command before either loads the pause flag or starts writing the shared queue.
func LockReporting(dataDir string) (*os.File, error) {
	root, err := os.OpenRoot(dataDir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	lock, err := root.OpenFile("diagnostics.lock", installReadFlags|os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if info, err := lock.Stat(); err != nil || !info.Mode().IsRegular() {
		lock.Close()
		return nil, errors.New("本机诊断锁不可用")
	}
	if err := lockReportingFile(lock); err != nil {
		lock.Close()
		return nil, err
	}
	return lock, nil
}

// ReportInstall sends exactly one eligible journal using the normal durable
// queue. The caller holds LockReporting throughout this operation.
func ReportInstall(ctx context.Context, dataDir, attemptID, connectorID, tenantID string, since time.Time, send Uploader) (int, error) {
	if since.IsZero() {
		return 0, errors.New("无法确定当前设备的绑定时间")
	}
	if !installIDRE.MatchString(attemptID) {
		return 0, errors.New("请提供 32 位小写十六进制安装编号")
	}
	manager, err := openManager(dataDir, true)
	if err != nil || manager.ownerID != connectorID || manager.ownerTenant != tenantID {
		return 0, errors.New("缺少与当前设备身份匹配的本机诊断状态")
	}
	if manager.paused {
		return 0, errors.New("诊断上报已暂停")
	}
	journals, err := openInstallDirectory(dataDir)
	if err != nil {
		return 0, errors.New("无法访问可信的安装记录目录")
	}
	defer journals.Close()
	manager.SetOwnerSince(connectorID, tenantID, since)
	manager.SetUploader(send)
	importErr := manager.readInstallJournal(journals, attemptID+".jsonl", attemptID, manager.installOwnerLocked())
	if importErr != nil && !errors.Is(importErr, errInstallQueueUnavailable) {
		return 0, importErr
	}
	if !manager.persistNow() {
		return 0, errors.New("未能保存本机诊断队列")
	}
	n, err := manager.uploadSelected(ctx, attemptID)
	if err != nil {
		return 0, errors.New("诊断提交失败（" + CodeForError(err) + "），可以重试")
	}
	if !manager.persistNow() {
		return n, errors.New("未能保存诊断提交结果，可以重试")
	}
	return n, importErr
}
