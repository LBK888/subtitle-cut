"""运行 ffmpeg 命令。"""

from __future__ import annotations

import re
import shutil
import subprocess
import io
import threading
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Union

PathLike = Union[str, Path]



def run_ffmpeg(
    args: Sequence[str],
    *,
    binary: str = "ffmpeg",
    check: bool = True,
    env: Optional[Mapping[str, str]] = None,
    progress_callback: Optional[Callable[[float], None]] = None,
    progress_duration: Optional[float] = None,
    capture_stdout: bool = False,
) -> subprocess.CompletedProcess:
    """运行 ffmpeg 命令。"""

    command = [binary, "-y", *args]
    needs_stream = progress_callback is not None or capture_stdout
    if not needs_stream:
        if capture_stdout:
            return subprocess.run(
                command,
                check=check,
                env=None if env is None else dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        return subprocess.run(command, check=check, env=None if env is None else dict(env))

    duration = progress_duration if progress_duration and progress_duration > 0 else None
    stderr_lines: list[str] = []
    time_pattern = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=None if env is None else dict(env),
        text=False,
    )
    stdout_buffer: Optional[io.BytesIO] = None
    stdout_thread: Optional[threading.Thread] = None
    try:
        if capture_stdout:
            stdout_buffer = io.BytesIO()
            if process.stdout is not None:
                def _pump_stdout() -> None:
                    while True:
                        chunk = process.stdout.read(65536)
                        if not chunk:
                            break
                        stdout_buffer.write(chunk)

                stdout_thread = threading.Thread(target=_pump_stdout, daemon=True)
                stdout_thread.start()

        if progress_callback is not None:
            progress_callback(0.0)

        if process.stderr is not None:
            for raw_line in iter(process.stderr.readline, b""):
                if not raw_line:
                    if process.poll() is not None:
                        break
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                stderr_lines.append(line)
                match = time_pattern.search(line)
                if match and duration:
                    hours = int(match.group(1))
                    minutes = int(match.group(2))
                    seconds = float(match.group(3))
                    current = hours * 3600 + minutes * 60 + seconds
                    if duration > 0:
                        fraction = max(0.0, min(1.0, current / duration))
                        if progress_callback is not None:
                            progress_callback(fraction)
        returncode = process.wait()
    finally:
        if capture_stdout and process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if capture_stdout and stdout_thread is not None:
            stdout_thread.join()

    if returncode == 0 and progress_callback is not None:
        progress_callback(1.0)

    stdout_value: Optional[bytes] = None
    if capture_stdout and stdout_buffer is not None:
        stdout_value = stdout_buffer.getvalue()

    stderr_text = "".join(stderr_lines) or None
    completed = subprocess.CompletedProcess(
        args=command,
        returncode=returncode,
        stdout=stdout_value,
        stderr=stderr_text,
    )
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, command, stderr=stderr_text)
    return completed

def ensure_ffmpeg_available(binary: str = "ffmpeg") -> None:
    """运行 ffmpeg 命令。"""

    if shutil.which(binary) is None:
        raise FileNotFoundError(f"FFmpeg binary '{binary}' not found in PATH")
