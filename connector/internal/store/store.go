// Package store 负责本机配置与状态的持久化。
//
// 约定：
//   - 普通配置与设备身份分开存放；
//   - 所有文件权限 0600，目录 0700；
//   - 写入使用临时文件加原子替换，避免断电产生半截文件。
package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// Store 是本机数据目录。
type Store struct {
	Dir string
}

// New 创建并确保数据目录存在。
func New(dir string) (*Store, error) {
	if dir == "" {
		return nil, errors.New("数据目录不能为空")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	return &Store{Dir: dir}, nil
}

// Path 返回目录内某个文件的完整路径。
func (s *Store) Path(name string) string {
	return filepath.Join(s.Dir, name)
}

// ReadJSON 读取 JSON 文件到目标对象；文件不存在时返回 fs.ErrNotExist。
func (s *Store) ReadJSON(name string, target any) error {
	raw, err := os.ReadFile(s.Path(name))
	if err != nil {
		return err
	}
	if err := json.Unmarshal(raw, target); err != nil {
		return fmt.Errorf("读取 %s 失败: %w", name, err)
	}
	return nil
}

// WriteJSON 原子写入 JSON 文件，权限 0600。
func (s *Store) WriteJSON(name string, value any) error {
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return s.WriteFile(name, raw)
}

// WriteFile 原子写入任意内容，权限 0600。
func (s *Store) WriteFile(name string, raw []byte) error {
	path := s.Path(name)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Remove 删除文件，不存在时视为成功。
func (s *Store) Remove(name string) error {
	err := os.Remove(s.Path(name))
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	return err
}

// Exists 判断文件是否存在。
func (s *Store) Exists(name string) bool {
	_, err := os.Stat(s.Path(name))
	return err == nil
}
