"""应用配置管理模块。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

LOGGER = logging.getLogger(__name__)


class AppConfig:
    """应用配置管理器"""

    def __init__(self, config_file: Path):
        self.config_file = config_file
        self._config: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """从配置文件加载配置"""
        if self.config_file.exists():
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
                LOGGER.info("配置已从 %s 加载", self.config_file)
            except Exception as e:
                LOGGER.warning("加载配置文件失败: %s", e)
                self._config = {}
        else:
            LOGGER.info("配置文件不存在,使用默认配置")
            self._config = {}

    def save(self) -> None:
        """保存配置到文件"""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
            LOGGER.info("配置已保存到 %s", self.config_file)
        except Exception as e:
            LOGGER.error("保存配置文件失败: %s", e)
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        return self._config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置配置项"""
        self._config[key] = value

    def update(self, updates: Dict[str, Any]) -> None:
        """批量更新配置项"""
        self._config.update(updates)

    @property
    def ramdisk_enabled(self) -> bool:
        """获取虚拟硬盘启用状态"""
        return self._config.get("ramdisk_enabled", False)

    @ramdisk_enabled.setter
    def ramdisk_enabled(self, value: bool) -> None:
        """设置虚拟硬盘启用状态"""
        self._config["ramdisk_enabled"] = value

    @property
    def ramdisk_size_gb(self) -> int:
        """获取虚拟硬盘容量(GB)"""
        return self._config.get("ramdisk_size_gb", 10)

    @ramdisk_size_gb.setter
    def ramdisk_size_gb(self, value: int) -> None:
        """设置虚拟硬盘容量(GB)"""
        self._config["ramdisk_size_gb"] = value


# 全局配置实例
_app_config: AppConfig | None = None


def get_app_config(config_file: Path | None = None) -> AppConfig:
    """获取全局配置实例"""
    global _app_config
    if _app_config is None:
        if config_file is None:
            raise ValueError("首次调用必须提供config_file参数")
        _app_config = AppConfig(config_file)
    return _app_config
