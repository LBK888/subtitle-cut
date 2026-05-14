"""虛擬硬碟管理模塊"""

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)

# 虛擬硬碟配置
RAMDISK_LABEL = "SubtitleDisk"
DEFAULT_RAMDISK_SIZE_GB = 10


class RamDiskManager:
    """虛擬硬碟管理器"""
    
    def __init__(self, enabled: bool = True, size_gb: int = DEFAULT_RAMDISK_SIZE_GB):
        self.enabled = enabled
        self.size_gb = size_gb
        self.mount_point: Optional[str] = None
        self.mount_root: Optional[Path] = None
        self.imdisk_exe: Optional[str] = None
        
    def initialize(self) -> bool:
        """初始化虛擬硬碟，如果已存在則復用，否則創建新的
        
        Returns:
            是否成功初始化
        """
        # 如果未啟用，直接返回False
        if not self.enabled:
            LOGGER.info("RAM disk is disabled by configuration")
            return False
        
        # 檢查imdisk是否可用
        self.imdisk_exe = shutil.which("imdisk")
        if not self.imdisk_exe:
            LOGGER.warning("imdisk not found, RAM disk功能不可用")
            return False
        
        # 1. 先查找是否已經存在SubtitleDisk
        LOGGER.info("Searching for existing RAM disk with label '%s'...", RAMDISK_LABEL)
        existing_mount = self._find_existing_ramdisk()
        if existing_mount:
            self.mount_point = f"{existing_mount}:"
            self.mount_root = Path(f"{existing_mount}:\\")
            LOGGER.info("✓ Found existing RAM disk: %s (label: %s)", self.mount_point, RAMDISK_LABEL)
            return True
        
        # 2. 不存在，創建新的虛擬硬碟
        LOGGER.info("No existing RAM disk found, creating new one...")
        return self._create_ramdisk()
    
    def _find_existing_ramdisk(self) -> Optional[str]:
        """查找是否已存在SubtitleDisk虛擬硬碟
        
        Returns:
            如果找到，返回盤符（如"R"），否則返回None
        """
        try:
            # 方法1: 遍歷所有盤符，檢查卷標
            import string
            checked_drives = []
            for letter in reversed(string.ascii_uppercase):  # 從Z往前找
                if letter in {"A", "B", "C"}:  # 跳過系統盤
                    continue
                drive_path = Path(f"{letter}:\\")
                if not drive_path.exists():
                    continue
                
                checked_drives.append(letter)
                
                # 檢查卷標
                try:
                    # 使用vol命令獲取卷標
                    vol_result = subprocess.run(
                        ["cmd", "/c", f"vol {letter}:"],
                        capture_output=True,
                        text=True,
                        encoding="gbk",  # Windows中文系統使用gbk
                        errors="replace",
                        timeout=2
                    )
                    if vol_result.returncode == 0:
                        # 調試：輸出卷標信息
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
        """創建新的虛擬硬碟
        
        Returns:
            是否成功創建
        """
        # 查找可用的盤符（從Z往前找）
        import string
        mount_letter: Optional[str] = None
        for letter in reversed(string.ascii_uppercase):
            if letter in {"A", "B", "C"}:  # 跳過系統盤
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
        
        # 創建虛擬硬碟
        size_mb = self.size_gb * 1024
        create_cmd = [
            self.imdisk_exe,
            "-a",
            "-s",
            f"{size_mb}M",
            "-m",
            self.mount_point,
            "-p",
            f"/fs:ntfs /q /y /v:{RAMDISK_LABEL}",  # 添加卷標
        ]
        
        LOGGER.info("Creating RAM disk: %s (%dGB, label: %s)", 
                   self.mount_point, self.size_gb, RAMDISK_LABEL)
        
        result = subprocess.run(create_cmd, capture_output=True)
        if result.returncode != 0:
            error_msg = result.stderr.decode("utf-8", errors="replace") if result.stderr else "unknown error"
            LOGGER.error("Failed to create RAM disk: %s", error_msg)
            return False
        
        # 等待掛載點出現
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self.mount_root.exists():
                LOGGER.info("✓ RAM disk created successfully: %s", self.mount_root)
                
                # 驗證卷標是否正確設置
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
        """獲取uploads目錄路徑（在虛擬硬碟上）"""
        if self.mount_root:
            return self.mount_root / "uploads"
        # 降級到本地
        return Path(__file__).resolve().parents[2] / "data" / "uploads"
    
    def get_tasks_dir(self) -> Path:
        """獲取tasks目錄路徑（在虛擬硬碟上）"""
        if self.mount_root:
            return self.mount_root / "tasks"
        # 降級到本地
        return Path(__file__).resolve().parents[2] / "data" / "tasks"
    
    def ensure_directories(self):
        """確保必要的目錄存在"""
        if self.mount_root:
            uploads_dir = self.get_uploads_dir()
            tasks_dir = self.get_tasks_dir()
            uploads_dir.mkdir(parents=True, exist_ok=True)
            tasks_dir.mkdir(parents=True, exist_ok=True)
            LOGGER.info("RAM disk directories created: uploads=%s, tasks=%s", 
                       uploads_dir, tasks_dir)
    
    def unmount(self) -> bool:
        """卸載虛擬硬碟
        
        Returns:
            是否成功卸載
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
        """重置虛擬硬碟容量（需要先卸載再重新創建）
        
        Args:
            new_size_gb: 新的容量（GB）
            
        Returns:
            是否成功重置
        """
        if not self.mount_point:
            LOGGER.warning("No existing RAM disk to reset")
            return False
        
        LOGGER.info("Resetting RAM disk size from %dGB to %dGB", self.size_gb, new_size_gb)
        
        # 卸載現有虛擬硬碟
        if not self.unmount():
            return False
        
        # 更新容量並重新創建
        self.size_gb = new_size_gb
        success = self._create_ramdisk()
        if success:
            self.ensure_directories()
        return success
    
    def cleanup(self):
        """清理虛擬硬碟（注意：不要在每次退出時都清理，保持虛擬硬碟存在）"""
        # 這個方法保留，但不在正常流程中調用
        # 只在需要手動清理時使用
        self.unmount()


# 全局單例
_ramdisk_manager: Optional[RamDiskManager] = None


def get_ramdisk_manager(enabled: Optional[bool] = None, size_gb: Optional[int] = None) -> RamDiskManager:
    """獲取全局虛擬硬碟管理器單例
    
    Args:
        enabled: 是否啟用虛擬硬碟（僅在首次創建時有效）
        size_gb: 虛擬硬碟容量（GB）（僅在首次創建時有效）
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
    """重置全局虛擬硬碟管理器（用於重新配置）"""
    global _ramdisk_manager
    if _ramdisk_manager is not None:
        _ramdisk_manager.cleanup()
        _ramdisk_manager = None
