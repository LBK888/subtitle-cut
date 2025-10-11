"""WhisperX 模型与设备管理。"""

from __future__ import annotations

import warnings
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Literal

DeviceType = Literal["cpu", "cuda", "auto"]


@dataclass
class ModelConfig:
    engine: str = "whisperx"
    name: str = "large-v2"
    device: DeviceType = "auto"
    compute_type: str = "auto"
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelBundle:
    model: Any
    device: str
    compute_type: str


def resolve_device(preferred: DeviceType) -> str:
    """根据偏好与可用性返回最终设备字符串。"""

    try:
        import torch  # type: ignore
    except ImportError:
        if preferred == "cuda":
            raise RuntimeError("Torch with CUDA support is required for GPU execution.")
        return "cpu"

    if preferred == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"

    if preferred == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("Requested CUDA device but no GPU is available.")
        return "cuda"

    if preferred == "cpu":
        return "cpu"

    raise ValueError(f"Unsupported device preference: {preferred}")


def load_whisperx_components(config: ModelConfig) -> ModelBundle:
    """加载 WhisperX 所需的模型组件。"""

    try:
        import whisperx  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "WhisperX is not installed. Run `pip install whisperx` before calling transcribe."
        ) from exc

    _ensure_cuda_dll_paths()

    device = resolve_device(config.device)
    compute_type = _resolve_compute_type(device=device, preferred=config.compute_type)

    try:
        model = whisperx.load_model(
            config.name, device=device, compute_type=compute_type
        )
    except ValueError as exc:
        if "float16" in str(exc) and compute_type == "float16":
            fallback_type = "int8"
            warnings.warn(
                "float16 compute type unavailable; falling back to int8.",
                RuntimeWarning,
                stacklevel=2,
            )
            model = whisperx.load_model(
                config.name, device=device, compute_type=fallback_type
            )
            compute_type = fallback_type
        else:
            raise
    except (RuntimeError, OSError) as exc:
        if device == "cuda":
            warnings.warn(
                "CUDA execution unavailable; falling back to CPU.",
                RuntimeWarning,
                stacklevel=2,
            )
            device = "cpu"
            compute_type = _resolve_compute_type(device=device, preferred=config.compute_type)
            model = whisperx.load_model(
                config.name, device=device, compute_type=compute_type
            )
        else:
            raise

    return ModelBundle(model=model, device=device, compute_type=compute_type)


def load_asr_components(config: ModelConfig) -> Any:
    engine = (config.engine or "whisperx").strip().lower()
    if engine == "whisperx":
        return load_whisperx_components(config)
    if engine == "paraformer":
        from .paraformer import ParaformerBundle

        device = resolve_device(config.device)
        return ParaformerBundle.from_config(config, device=device)
    raise ValueError(f"Unknown ASR engine: {config.engine}")


def _resolve_compute_type(*, device: str, preferred: str) -> str:
    if preferred != "auto":
        return preferred
    if device == "cuda":
        try:
            import ctranslate2  # type: ignore

            supported = set(ctranslate2.supported_compute_types("cuda"))
            if "float16" in supported:
                return "float16"
            if "int8_float16" in supported:
                return "int8_float16"
        except Exception:
            return "int8"
        return "int8"
    return "int8"


def _ensure_cuda_dll_paths() -> None:
    if os.name != "nt":
        return

    paths: list[Path] = []

    try:
        import nvidia.cudnn  # type: ignore

        paths.append(Path(nvidia.cudnn.__file__).resolve().parent / "bin")
    except Exception:
        pass

    for name in ("CUDNN_BIN_DIR", "CUDA_PATH", "CUDA_PATH_V11_8", "CUDA_PATH_V12_4"):
        value = os.environ.get(name)
        if value:
            paths.append(Path(value) / "bin")

    paths.extend(
        [
            Path("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v11.8/bin"),
            Path("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.4/bin"),
        ]
    )

    for path in paths:
        try:
            os.add_dll_directory(str(path))
        except (FileNotFoundError, OSError):
            continue
