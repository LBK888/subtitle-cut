"""虚拟硬盘管理模块"""

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)

# 虚拟硬盘配置
RAMDISK_LABEL = "SubtitleDisk"
DEFAULT_RAMDISK_SIZE_GB = 10


class RamDiskManager:
    """虚拟硬盘管理器"""
    
    def __init__(self, enabled: bool = True, size_gb: int = DEFAULT_RAMDISK_SIZE_GB):
        self.enabled = enabled
        self.size_gb = size_gb
        self.mount_point: Optional[str] = None
        self.mount_root: Optional[Path] = None
        self.imdisk_exe: Optional[str] = None
        
    def initialize(self) -> bool:
        """初始化虚拟硬盘，如果已存在则复用，否则创建新的
        
        Returns:
            是否成功初始化
        """
        # 如果未启用，直接返回False
        if not self.enabled:
            LOGGER.info("RAM disk is disabled by configuration")
            return False
        
        # 检查imdisk是否可用
        self.imdisk_exe = shutil.which("imdisk")
        if not self.imdisk_exe:
            LOGGER.warning("imdisk not found, RAM disk功能不可用")
            return False
        
        # 1. 先查找是否已经存在SubtitleDisk
        LOGGER.info("Searching for existing RAM disk with label '%s'...", RAMDISK_LABEL)
        existing_mount = self._find_existing_ramdisk()
        if existing_mount:
            self.mount_point = f"{existing_mount}:"
            self.mount_root = Path(f"{existing_mount}:\\")
            LOGGER.info("✓ Found existing RAM disk: %s (label: %s)", self.mount_point, RAMDISK_LABEL)
            return True
        
        # 2. 不存在，创建新的虚拟硬盘
        LOGGER.info("No existing RAM disk found, creating new one...")
        return self._create_ramdisk()
    
    def _find_existing_ramdisk(self) -> Optional[str]:
        """查找是否已存在SubtitleDisk虚拟硬盘
        
        Returns:
            如果找到，返回盘符（如"R"），否则返回None
        """
        try:
            # 方法1: 遍历所有盘符，检查卷标
            import string
            checked_drives = []
            for letter in reversed(string.ascii_uppercase):  # 从Z往前找
                if letter in {"A", "B", "C"}:  # 跳过系统盘
                    continue
                drive_path = Path(f"{letter}:\\")
                if not drive_path.exists():
                    continue
                
                checked_drives.append(letter)
                
                # 检查卷标
                try:
                    # 使用vol命令获取卷标
                    vol_result = subprocess.run(
                        ["cmd", "/c", f"vol {letter}:"],
                        capture_output=True,
                        text=True,
                        encoding="gbk",  # Windows中文系统使用gbk
                        errors="replace",
                        timeout=2
                    )
                    if vol_result.returncode == 0:
                        # 调试：输出卷标信息
                        LOGGER.debug("Drive %s: vol output: %s", letter, vol_result.stdout.strip())
                        if RAMDISK_LABEL in vol_result.stdout:
                            LOGGER.info("✓ Found existing RAM disk with label '%s' at %s:", RAMDISK_LABEL, letter)
                            return letter
                except Exception as e:
                    LOGGER.debug("Failed to check volume label for %s: %s", letter, e)
                    pass
            
            LOGGER.info("Checked drives: %s, no RAM disk with label '%s' found", 
                       ', '.join(checked_drives), RAMDISK_LABEL)
            return None
            
        except Exception as e:
            LOGGER.warning("Failed to find existing RAM disk: %s", e)
            return None
    
    def _create_ramdisk(self) -> bool:
        """创建新的虚拟硬盘
        
        Returns:
            是否成功创建
        """
        # 查找可用的盘符（从Z往前找）
        import string
        mount_letter: Optional[str] = None
        for letter in reversed(string.ascii_uppercase):
            if letter in {"A", "B", "C"}:  # 跳过系统盘
                continue
            drive = Path(f"{letter}:\\")
            if not drive.exists():
                mount_letter = letter
                break
        
        if mount_letter is None:
            LOGGER.error("No free drive letter available for RAM disk")
            return False
        
        self.mount_point = f"{mount_letter}:"
        self.mount_root = Path(f"{mount_letter}:\\")
        
        # 创建虚拟硬盘
        size_mb = self.size_gb * 1024
        create_cmd = [
            self.imdisk_exe,
            "-a",
            "-s",
            f"{size_mb}M",
            "-m",
            self.mount_point,
            "-p",
            f"/fs:ntfs /q /y /v:{RAMDISK_LABEL}",  # 添加卷标
        ]
        
        LOGGER.info("Creating RAM disk: %s (%dGB, label: %s)", 
                   self.mount_point, self.size_gb, RAMDISK_LABEL)
        
        result = subprocess.run(create_cmd, capture_output=True)
        if result.returncode != 0:
            error_msg = result.stderr.decode("utf-8", errors="replace") if result.stderr else "unknown error"
            LOGGER.error("Failed to create RAM disk: %s", error_msg)
            return False
        
        # 等待挂载点出现
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self.mount_root.exists():
                LOGGER.info("✓ RAM disk created successfully: %s", self.mount_root)
                
                # 验证卷标是否正确设置
                try:
                    vol_result = subprocess.run(
                        ["cmd", "/c", f"vol {mount_letter}:"],
                        capture_output=True,
                        text=True,
                        encoding="gbk",
                        errors="replace",
                        timeout=2
                    )
                    if RAMDISK_LABEL in vol_result.stdout:
                        LOGGER.info("✓ Volume label verified: %s", RAMDISK_LABEL)
                    else:
                        LOGGER.warning("Volume label not found in output: %s", vol_result.stdout)
                except Exception as e:
                    LOGGER.warning("Failed to verify volume label: %s", e)
                
                return True
            time.sleep(0.1)
        
        LOGGER.error("RAM disk mount point %s did not appear in time", self.mount_root)
        return False
    
    def get_uploads_dir(self) -> Path:
        """获取uploads目录路径（在虚拟硬盘上）"""
        if self.mount_root:
            return self.mount_root / "uploads"
        # 降级到本地
        return Path(__file__).resolve().parents[2] / "data" / "uploads"
    
    def get_tasks_dir(self) -> Path:
        """获取tasks目录路径（在虚拟硬盘上）"""
        if self.mount_root:
            return self.mount_root / "tasks"
        # 降级到本地
        return Path(__file__).resolve().parents[2] / "data" / "tasks"
    
    def ensure_directories(self):
        """确保必要的目录存在"""
        if self.mount_root:
            uploads_dir = self.get_uploads_dir()
            tasks_dir = self.get_tasks_dir()
            uploads_dir.mkdir(parents=True, exist_ok=True)
            tasks_dir.mkdir(parents=True, exist_ok=True)
            LOGGER.info("RAM disk directories created: uploads=%s, tasks=%s", 
                       uploads_dir, tasks_dir)
    
    def unmount(self) -> bool:
        """卸载虚拟硬盘
        
        Returns:
            是否成功卸载
        """
        if not self.mount_point or not self.imdisk_exe:
            LOGGER.warning("No RAM disk to unmount")
            return False
        
        LOGGER.info("Unmounting RAM disk: %s", self.mount_point)
        detach_cmd = [self.imdisk_exe, "-D", "-m", self.mount_point]
        result = subprocess.run(detach_cmd, capture_output=True, check=False)
        if result.returncode == 0:
            LOGGER.info("RAM disk unmounted successfully")
            self.mount_point = None
            self.mount_root = None
            return True
        else:
            error_msg = result.stderr.decode("utf-8", errors="replace") if result.stderr else "unknown error"
            LOGGER.warning("Failed to unmount RAM disk: %s", error_msg)
            return False
    
    def reset_size(self, new_size_gb: int) -> bool:
        """重置虚拟硬盘容量（需要先卸载再重新创建）
        
        Args:
            new_size_gb: 新的容量（GB）
            
        Returns:
            是否成功重置
        """
        if not self.mount_point:
            LOGGER.warning("No existing RAM disk to reset")
            return False
        
        LOGGER.info("Resetting RAM disk size from %dGB to %dGB", self.size_gb, new_size_gb)
        
        # 卸载现有虚拟硬盘
        if not self.unmount():
            return False
        
        # 更新容量并重新创建
        self.size_gb = new_size_gb
        success = self._create_ramdisk()
        if success:
            self.ensure_directories()
        return success
    
    def cleanup(self):
        """清理虚拟硬盘（注意：不要在每次退出时都清理，保持虚拟硬盘存在）"""
        # 这个方法保留，但不在正常流程中调用
        # 只在需要手动清理时使用
        self.unmount()


# 全局单例
_ramdisk_manager: Optional[RamDiskManager] = None


def get_ramdisk_manager(enabled: Optional[bool] = None, size_gb: Optional[int] = None) -> RamDiskManager:
    """获取全局虚拟硬盘管理器单例
    
    Args:
        enabled: 是否启用虚拟硬盘（仅在首次创建时有效）
        size_gb: 虚拟硬盘容量（GB）（仅在首次创建时有效）
    """
    global _ramdisk_manager
    if _ramdisk_manager is None:
        _enabled = enabled if enabled is not None else True
        _size_gb = size_gb if size_gb is not None else DEFAULT_RAMDISK_SIZE_GB
        _ramdisk_manager = RamDiskManager(enabled=_enabled, size_gb=_size_gb)
        _ramdisk_manager.initialize()
        _ramdisk_manager.ensure_directories()
    return _ramdisk_manager


def reset_ramdisk_manager():
    """重置全局虚拟硬盘管理器（用于重新配置）"""
    global _ramdisk_manager
    if _ramdisk_manager is not None:
        _ramdisk_manager.cleanup()
        _ramdisk_manager = None
