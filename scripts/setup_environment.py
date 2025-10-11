#!/usr/bin/env python3
"""
One-click environment bootstrap script for the subtitle-cut project.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
import venv


def ensure_python_version() -> None:
    if sys.version_info < (3, 10):
        message = (
            "Detected Python version lower than 3.10. "
            "Please upgrade the interpreter before running this setup script."
        )
        raise RuntimeError(message)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_venv_paths(venv_dir: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        bin_dir = venv_dir / "Scripts"
        python_binary = bin_dir / "python.exe"
        pip_binary = bin_dir / "pip.exe"
    else:
        bin_dir = venv_dir / "bin"
        python_binary = bin_dir / "python"
        pip_binary = bin_dir / "pip"
    return python_binary, pip_binary


def create_virtual_env(venv_dir: Path) -> bool:
    if venv_dir.exists():
        return False
    builder = venv.EnvBuilder(with_pip=True, clear=False, system_site_packages=False)
    builder.create(str(venv_dir))
    return True


def run_command(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, check=True, cwd=str(cwd))


def upgrade_pip_tools(python_binary: Path, *, cwd: Path) -> None:
    command = [
        str(python_binary),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "setuptools",
        "wheel",
    ]
    run_command(command, cwd=cwd)


def install_project_dependencies(
    python_binary: Path,
    *,
    cwd: Path,
    extras: list[str],
) -> None:
    target = "."
    if extras:
        target = f".[{','.join(extras)}]"
    command = [str(python_binary), "-m", "pip", "install", "-e", target]
    run_command(command, cwd=cwd)


def ensure_env_file(root: Path) -> None:
    env_example = root / ".env.example"
    env_file = root / ".env"
    if env_file.exists() or not env_example.exists():
        return
    shutil.copy2(env_example, env_file)


def check_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a virtual environment and install subtitle-cut dependencies automatically."
    )
    parser.add_argument(
        "--venv",
        default=".venv",
        help="Virtual environment directory (default: .venv in the project root).",
    )
    parser.add_argument(
        "--extras",
        nargs="*",
        choices=["dev", "paraformer"],
        default=[],
        help="Optional extras to install, for example: --extras dev paraformer.",
    )
    parser.add_argument(
        "--skip-env",
        action="store_true",
        help="Skip automatic .env creation.",
    )
    parser.add_argument(
        "--skip-ffmpeg-check",
        action="store_true",
        help="Skip ffmpeg availability check.",
    )
    return parser.parse_args()


def main() -> None:
    ensure_python_version()
    args = parse_arguments()
    root = project_root()
    venv_dir = (root / args.venv).resolve()

    created = create_virtual_env(venv_dir)
    python_binary, pip_binary = resolve_venv_paths(venv_dir)

    if not pip_binary.exists():
        run_command([str(python_binary), "-m", "ensurepip", "--upgrade"], cwd=root)

    upgrade_pip_tools(python_binary, cwd=root)
    install_project_dependencies(python_binary, cwd=root, extras=args.extras)

    if not args.skip_env:
        ensure_env_file(root)

    ffmpeg_ready = True if args.skip_ffmpeg_check else check_ffmpeg_available()

    summary_lines = [
        "Environment setup completed.",
        f"Virtual environment directory: {venv_dir}",
        "Virtual environment created" if created else "Existing virtual environment reused",
        f"Installed optional extras: {', '.join(args.extras) if args.extras else 'none'}",
    ]

    if not args.skip_env:
        summary_lines.append("Copied .env.example to .env because it was missing.")

    if not ffmpeg_ready:
        summary_lines.append(
            "Warning: ffmpeg not found. See scripts/install_ffmpeg_help.md for installation guidance."
        )

    activate_hint = (
        f"{venv_dir / 'Scripts' / 'activate'}"
        if os.name == "nt"
        else f"source {venv_dir}/bin/activate"
    )
    summary_lines.append(f"Activate the virtual environment with: {activate_hint}")
    summary_lines.append(
        "To launch the web app, run run_webapp.bat in the project root or use the Flask CLI."
    )

    print("\n".join(textwrap.dedent(line).strip() for line in summary_lines))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(error)
        sys.exit(1)
