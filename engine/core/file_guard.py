"""
engine/core/file_guard.py — 文件编辑守卫

自动备份代码文件，支持回滚。
Claude Code 风格：编辑前备份，失败时回滚。

覆盖工具: code_write, code_append, code_create
"""

import logging
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class FileEditGuard:
    """
    文件编辑守卫 — 集中管理代码文件的备份和回滚

    备份策略:
    - 备份目录: {working_dir}/.jarvis/backups/
    - 命名规则: {timestamp}_{filename}.bak
    - 保留策略: 每个文件最多保留 10 个版本
    """

    GUARDED_TOOLS = {"code_write", "code_append", "code_create"}
    MAX_BACKUPS_PER_FILE = 10

    def __init__(self, working_dir: str = "."):
        self._working_dir = Path(working_dir).resolve()
        self._backup_dir = self._working_dir / ".jarvis" / "backups"
        # file_path -> [backup_path, ...] (时间顺序)
        self._backup_index: Dict[str, List[str]] = {}

    def should_guard(self, tool_name: str) -> bool:
        """判断工具是否需要备份"""
        return tool_name in self.GUARDED_TOOLS

    def backup_before_edit(self, file_path: str) -> Optional[str]:
        """
        在编辑前备份文件。

        Args:
            file_path: 被编辑的文件路径

        Returns:
            备份文件路径，如果文件不存在则返回 None
        """
        src = Path(file_path)
        if not src.is_absolute():
            src = self._working_dir / src

        if not src.exists():
            return None

        # 创建备份目录
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        # 生成带时间戳的备份文件名
        timestamp = int(time.time() * 1000)
        backup_name = f"{timestamp}_{src.name}.bak"
        backup_path = self._backup_dir / backup_name

        # 复制文件
        shutil.copy2(src, backup_path)

        # 更新索引
        key = str(src.resolve())
        if key not in self._backup_index:
            self._backup_index[key] = []
        self._backup_index[key].append(str(backup_path))

        # 清理超过上限的旧版本
        backups = self._backup_index[key]
        if len(backups) > self.MAX_BACKUPS_PER_FILE:
            old_backups = backups[:len(backups) - self.MAX_BACKUPS_PER_FILE]
            for old_path in old_backups:
                try:
                    Path(old_path).unlink(missing_ok=True)
                except Exception:
                    pass
            self._backup_index[key] = backups[-self.MAX_BACKUPS_PER_FILE:]

        logger.debug(f"🛡️ FileGuard: 已备份 {src} -> {backup_path}")
        return str(backup_path)

    def rollback(self, file_path: str, version: int = -1) -> bool:
        """
        从备份恢复文件。

        Args:
            file_path: 要恢复的文件路径
            version: 版本索引，-1 表示最近一次备份

        Returns:
            是否恢复成功
        """
        src = Path(file_path)
        if not src.is_absolute():
            src = self._working_dir / src

        key = str(src.resolve())
        backups = self._backup_index.get(key, [])

        if not backups:
            logger.warning(f"FileGuard: 无备份可恢复: {file_path}")
            return False

        try:
            backup_path = backups[version]
        except IndexError:
            logger.warning(f"FileGuard: 版本 {version} 不存在: {file_path}")
            return False

        if not Path(backup_path).exists():
            logger.warning(f"FileGuard: 备份文件不存在: {backup_path}")
            return False

        # 恢复文件
        shutil.copy2(backup_path, src)

        # 移除已使用的备份（及其之后的版本）
        if version == -1:
            version = len(backups) - 1
        self._backup_index[key] = backups[:version]

        logger.info(f"⏪ FileGuard: 已回滚 {file_path} <- {backup_path}")
        return True

    def list_backups(self, file_path: str) -> List[Dict]:
        """列出文件的所有备份版本"""
        src = Path(file_path)
        if not src.is_absolute():
            src = self._working_dir / src

        key = str(src.resolve())
        backups = self._backup_index.get(key, [])

        result = []
        for i, backup_path in enumerate(backups):
            bp = Path(backup_path)
            if bp.exists():
                stat = bp.stat()
                result.append({
                    "version": i,
                    "path": backup_path,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
        return result

    def get_backup_dir(self) -> str:
        """返回备份目录路径"""
        return str(self._backup_dir)

    def get_backup_count(self, file_path: str) -> int:
        """获取文件的备份数量"""
        src = Path(file_path)
        if not src.is_absolute():
            src = self._working_dir / src
        key = str(src.resolve())
        return len(self._backup_index.get(key, []))
