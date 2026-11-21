//go:build windows

package secretfile

import (
	"encoding/base64"
	"errors"
	"fmt"
	"runtime"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	platformEncryptionSupported = true
	cryptProtectLocalMachine    = 0x4
)

type dataBlob struct {
	size uint32
	data *byte
}

var (
	crypt32                = windows.NewLazySystemDLL("crypt32.dll")
	kernel32               = windows.NewLazySystemDLL("kernel32.dll")
	procCryptProtectData   = crypt32.NewProc("CryptProtectData")
	procCryptUnprotectData = crypt32.NewProc("CryptUnprotectData")
	procLocalFree          = kernel32.NewProc("LocalFree")
	dpapiEntropy           = []byte("KaiwuConnector/DPAPI/v1")
)

func protect(raw []byte) ([]byte, error) {
	encrypted, err := transform(procCryptProtectData, raw, cryptProtectLocalMachine)
	if err != nil {
		return nil, err
	}
	return []byte(base64.StdEncoding.EncodeToString(encrypted)), nil
}

func unprotect(raw []byte) ([]byte, error) {
	decoded, err := base64.StdEncoding.DecodeString(string(raw))
	if err != nil {
		return nil, errors.New("DPAPI 密文编码无效")
	}
	return transform(procCryptUnprotectData, decoded, 0)
}

func transform(proc *windows.LazyProc, raw []byte, flags uintptr) ([]byte, error) {
	var input dataBlob
	if len(raw) > 0 {
		input = dataBlob{size: uint32(len(raw)), data: &raw[0]}
	}
	entropy := dataBlob{size: uint32(len(dpapiEntropy)), data: &dpapiEntropy[0]}
	var output dataBlob
	var description *uint16
	returnCode, _, callErr := proc.Call(
		uintptr(unsafe.Pointer(&input)),
		uintptr(unsafe.Pointer(description)),
		uintptr(unsafe.Pointer(&entropy)),
		0,
		0,
		flags,
		uintptr(unsafe.Pointer(&output)),
	)
	runtime.KeepAlive(raw)
	runtime.KeepAlive(dpapiEntropy)
	if returnCode == 0 {
		if callErr != nil {
			return nil, fmt.Errorf("DPAPI 调用失败: %w", callErr)
		}
		return nil, errors.New("DPAPI 调用失败")
	}
	if output.data == nil || output.size == 0 {
		return nil, errors.New("DPAPI 返回空数据")
	}
	defer procLocalFree.Call(uintptr(unsafe.Pointer(output.data)))
	result := make([]byte, output.size)
	copy(result, unsafe.Slice(output.data, output.size))
	return result, nil
}
