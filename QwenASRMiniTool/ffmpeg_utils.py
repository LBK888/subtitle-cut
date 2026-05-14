"""ffmpeg_utils.py — ffmpeg 偵測、音軌提取、一鍵下載

設計原則：
  1. 零磁碟開銷的 pipe 提取（f32le → numpy），僅用於需要 ndarray 的場合
  2. 引擎層需要 Path 的場合，改寫臨時 WAV 再傳入
  3. 下載 dialog 使用 after() 輪詢，不阻塞 Tkinter 事件循環
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

import customtkinter as ctk

# ── 影片副檔名集合 ──────────────────────────────────────────────────
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".ts", ".m2ts", ".mpg", ".mpeg", ".m4v",
    ".vob", ".3gp", ".f4v", ".mxf",
}

# ── ffmpeg Windows 下載來源（BtbN essentials，約 55 MB）────────────
_FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
    "ffmpeg-master-latest-win64-gpl-essentials.zip"
)
# ZIP 內 ffmpeg.exe 的路徑前綴（版本號不固定，只比對後綴）
_FFMPEG_ZIP_SUFFIX = "bin/ffmpeg.exe"


def is_video(path: Path) -> bool:
    """回傳 True 代表此檔案是影片（需要 ffmpeg 提取音軌）。"""
    return path.suffix.lower() in VIDEO_EXTS


def find_ffmpeg() -> Path | None:
    """按順序搜尋 ffmpeg：系統 PATH → App 目錄 → 常見安裝路徑。"""
    # 1. 系統 PATH
    which = shutil.which("ffmpeg")
    if which:
        return Path(which)

    # 2. App 目錄下的 ffmpeg/ 子目錄（EXE 模式 or 原始碼模式）
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    local = base / "ffmpeg" / "ffmpeg.exe"
    if local.exists():
        return local

    # 3. 常見 Windows 安裝路徑
    for candidate in [
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files (x86)/ffmpeg/bin/ffmpeg.exe"),
    ]:
        if candidate.exists():
            return candidate

    return None


def get_default_ffmpeg_dest() -> Path:
    """回傳下載後的儲存目錄（<app_dir>/ffmpeg/ffmpeg.exe）。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    return base / "ffmpeg"


# ── 音訊提取 ──────────────────────────────────────────────────────────

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def extract_audio_to_wav(
    video_path: Path,
    out_wav: Path,
    ffmpeg_exe: Path,
    sr: int = 16000,
) -> None:
    """用 ffmpeg 把影片音軌提取成 16kHz mono PCM WAV。

    出現 ffmpeg 錯誤時拋出 RuntimeError（含 stderr 的最後一行）。
    """
    cmd = [
        str(ffmpeg_exe), "-y",
        "-i", str(video_path),
        "-vn",               # 丟棄影像流
        "-ar", str(sr),      # 取樣率
        "-ac", "1",          # 單聲道
        "-f", "wav",
        str(out_wav),
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace")
        last_line = next(
            (l.strip() for l in reversed(err.splitlines()) if l.strip()), "未知錯誤"
        )
        raise RuntimeError(f"ffmpeg 提取失敗：{last_line}")


# ── 下載對話框 ────────────────────────────────────────────────────────

class FFmpegDownloadDialog(ctk.CTkToplevel):
    """一鍵下載 ffmpeg essentials 的進度對話框。

    使用方式
    --------
    dlg = FFmpegDownloadDialog(parent, on_success=lambda ffmpeg_path: ...)
    # 對話框自帶 grab_set()；下載成功後呼叫 on_success(Path)，然後關閉。
    # 使用者取消 → 直接 destroy，不呼叫 on_success。
    """

    def __init__(
        self,
        parent,
        on_success: Callable[[Path], None] | None = None,
    ):
        super().__init__(parent)
        self._on_success = on_success
        self._q: queue.Queue = queue.Queue()   # (type, value) 進度訊息
        self._cancelled = False

        self.title("下載 ffmpeg")
        self.geometry("460x260")
        self.resizable(False, False)
        self.grab_set()

        self._build_ui()
        self.after(120, self._bring_to_front)
        self.after(300, self._poll)   # 開始輪詢進度佇列

        # 啟動下載執行緒
        dest_dir = get_default_ffmpeg_dest()
        threading.Thread(
            target=self._download_thread,
            args=(dest_dir,),
            daemon=True,
        ).start()

    def _bring_to_front(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _build_ui(self):
        ctk.CTkLabel(
            self,
            text="下載 ffmpeg（影片音軌提取工具）",
            font=("Microsoft JhengHei", 14, "bold"),
            text_color="#AAAACC",
        ).pack(pady=(20, 6))

        ctk.CTkLabel(
            self,
            text="來源：BtbN/FFmpeg-Builds（Windows gpl-essentials）",
            font=("Microsoft JhengHei", 11),
            text_color="#555566",
        ).pack()

        ctk.CTkLabel(
            self,
            text="下載完成後儲存於：<App 目錄>/ffmpeg/ffmpeg.exe",
            font=("Microsoft JhengHei", 10),
            text_color="#444455",
        ).pack(pady=(2, 10))

        self._prog_bar = ctk.CTkProgressBar(self, width=380, height=14,
                                             mode="indeterminate")
        self._prog_bar.pack(pady=(0, 6))
        self._prog_bar.start()

        self._size_lbl = ctk.CTkLabel(
            self, text="正在連線…",
            font=("Consolas", 11), text_color="#778899",
        )
        self._size_lbl.pack()

        self._status_lbl = ctk.CTkLabel(
            self, text="",
            font=("Microsoft JhengHei", 11), text_color="#888899",
        )
        self._status_lbl.pack(pady=(4, 0))

        self._cancel_btn = ctk.CTkButton(
            self, text="取消", width=90, height=32,
            fg_color="#38181A", hover_color="#552428",
            font=("Microsoft JhengHei", 12),
            command=self._cancel,
        )
        self._cancel_btn.pack(pady=(14, 0))

    def _poll(self):
        """主執行緒輪詢進度佇列，更新 UI。"""
        try:
            while True:
                msg_type, value = self._q.get_nowait()
                if msg_type == "progress":
                    downloaded_mb, total_mb = value
                    if total_mb > 0:
                        pct = downloaded_mb / total_mb
                        self._prog_bar.stop()
                        self._prog_bar.configure(mode="determinate")
                        self._prog_bar.set(pct)
                        self._size_lbl.configure(
                            text=f"{downloaded_mb:.1f} / {total_mb:.1f} MB"
                        )
                    else:
                        self._size_lbl.configure(
                            text=f"{downloaded_mb:.1f} MB 已下載"
                        )
                elif msg_type == "status":
                    self._status_lbl.configure(text=value)
                elif msg_type == "done":
                    ffmpeg_path: Path = value
                    self._prog_bar.set(1.0)
                    self._size_lbl.configure(text="下載完成 ✓")
                    self._status_lbl.configure(
                        text=str(ffmpeg_path), text_color="#58D68D"
                    )
                    self._cancel_btn.configure(
                        text="關閉", fg_color="#183A1A", hover_color="#245528"
                    )
                    self.after(800, lambda p=ffmpeg_path: self._finish(p))
                    return
                elif msg_type == "error":
                    self._prog_bar.stop()
                    self._prog_bar.set(0)
                    self._status_lbl.configure(
                        text=f"❌ {value}", text_color="#F1948A"
                    )
                    self._cancel_btn.configure(text="關閉")
                    return
        except queue.Empty:
            pass
        if not self._cancelled:
            self.after(200, self._poll)

    def _download_thread(self, dest_dir: Path):
        import io
        import ssl
        import urllib.request

        def _ssl_ctx():
            try:
                import certifi
                return ssl.create_default_context(cafile=certifi.where())
            except Exception:
                return ssl.create_default_context()

        dest_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg_exe = dest_dir / "ffmpeg.exe"
        zip_buf = io.BytesIO()

        self._q.put(("status", "正在下載 ZIP…"))
        try:
            with urllib.request.urlopen(_FFMPEG_URL, timeout=60, context=_ssl_ctx()) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                total_mb = total / 1024 / 1024
                downloaded = 0
                chunk_size = 65536
                while True:
                    if self._cancelled:
                        return
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    zip_buf.write(chunk)
                    downloaded += len(chunk)
                    self._q.put(("progress", (downloaded / 1024 / 1024, total_mb)))
        except Exception as e:
            self._q.put(("error", str(e)))
            return

        self._q.put(("status", "解壓縮中…"))
        try:
            zip_buf.seek(0)
            with zipfile.ZipFile(zip_buf) as zf:
                # 找到 bin/ffmpeg.exe（路徑前綴不固定）
                target = next(
                    (n for n in zf.namelist() if n.endswith(_FFMPEG_ZIP_SUFFIX)),
                    None,
                )
                if not target:
                    self._q.put(("error", "ZIP 中找不到 ffmpeg.exe"))
                    return
                data = zf.read(target)
                ffmpeg_exe.write_bytes(data)
        except Exception as e:
            self._q.put(("error", str(e)))
            return

        self._q.put(("done", ffmpeg_exe))

    def _finish(self, ffmpeg_path: Path):
        if self._on_success:
            self._on_success(ffmpeg_path)
        self.destroy()

    def _cancel(self):
        self._cancelled = True
        self.destroy()


# ── 公開輔助函式 ──────────────────────────────────────────────────────

def ensure_ffmpeg(
    parent_widget,
    on_ready: Callable[[Path], None],
    on_fail: Callable[[], None] | None = None,
) -> None:
    """確認 ffmpeg 可用；若不可用則彈出下載對話框。

    on_ready(ffmpeg_path) 在 ffmpeg 就緒後（無論本來就有或剛下載）呼叫。
    on_fail() 在使用者取消下載時呼叫（可為 None）。
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        on_ready(ffmpeg)
        return

    # 沒有 ffmpeg → 詢問是否下載
    from tkinter import messagebox
    want = messagebox.askyesno(
        "需要 ffmpeg",
        "處理影片檔案需要 ffmpeg。\n\n"
        "是否自動下載？（約 55 MB，一次性，存於 App 目錄）\n\n"
        "也可手動安裝後重試：https://ffmpeg.org/download.html",
        parent=parent_widget,
    )
    if not want:
        if on_fail:
            on_fail()
        return

    def _on_dl_success(ffmpeg_path: Path):
        on_ready(ffmpeg_path)

    def _on_dl_cancel():
        if on_fail:
            on_fail()

    dlg = FFmpegDownloadDialog(parent_widget, on_success=_on_dl_success)
    dlg.protocol("WM_DELETE_WINDOW", _on_dl_cancel)
