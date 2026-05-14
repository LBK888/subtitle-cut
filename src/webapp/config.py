"""應用配置管理模塊。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

LOGGER = logging.getLogger(__name__)


class AppConfig:
    """應用配置管理器"""

    def __init__(self, config_file: Path):
        self.config_file = config_file
        self._config: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """從配置文件加載配置"""
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
                LOGGER.info("配置已從 %s 加載", self.config_file)
            except Exception as e:
                LOGGER.warning("加載配置文件失敗: %s", e)
                self._config = {}
        else:
            LOGGER.info("配置文件不存在,使用默認配置")
            self._config = {}

    def save(self) -> None:
        """保存配置到文件"""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
            LOGGER.info("配置已保存到 %s", self.config_file)
        except Exception as e:
            LOGGER.error("保存配置文件失敗: %s", e)
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """獲取配置項"""
        return self._config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """設置配置項"""
        self._config[key] = value

    def update(self, updates: Dict[str, Any]) -> None:
        """批量更新配置項"""
        self._config.update(updates)

    @property
    def ramdisk_enabled(self) -> bool:
        """獲取虛擬硬碟啟用狀態"""
        return self._config.get("ramdisk_enabled", False)

    @ramdisk_enabled.setter
    def ramdisk_enabled(self, value: bool) -> None:
        """設置虛擬硬碟啟用狀態"""
        self._config["ramdisk_enabled"] = value

    @property
    def ramdisk_size_gb(self) -> int:
        """獲取虛擬硬碟容量(GB)"""
        return self._config.get("ramdisk_size_gb", 10)

    @ramdisk_size_gb.setter
    def ramdisk_size_gb(self, value: int) -> None:
        """設置虛擬硬碟容量(GB)"""
        self._config["ramdisk_size_gb"] = value


# 全局配置實例
_app_config: AppConfig | None = None


def get_app_config(config_file: Path | None = None) -> AppConfig:
    """獲取全局配置實例"""
    global _app_config
    if _app_config is None:
        if config_file is None:
            raise ValueError("首次調用必須提供config_file參數")
        _app_config = AppConfig(config_file)
    return _app_config
