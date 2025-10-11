#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$ROOT_DIR/data"
ASSETS_DIR="$ROOT_DIR/tests/assets"
DIST_DIR="$ROOT_DIR/dist"

mkdir -p "$DIST_DIR"

INPUT="$DIST_DIR/demo_input.mp4"
OUTPUT_VIDEO="$DIST_DIR/demo_output.mp4"
OUTPUT_SRT="$DIST_DIR/demo_output.srt"

if [[ ! -f "$ASSETS_DIR/tiny.wav" ]]; then
  echo "缺少 tests/assets/tiny.wav，请替换为真实音频后再运行 demo。" >&2
  exit 1
fi

if [[ ! -f "$INPUT" ]]; then
  echo "生成示例视频..."
  ffmpeg -y -f lavfi -i color=c=black:s=640x360:d=3 \
         -i "$ASSETS_DIR/tiny.wav" -shortest \
         -c:v libx264 -pix_fmt yuv420p -c:a aac "$INPUT"
fi

echo "执行一体化流程..."
python -m src.cli run \
  --input "$INPUT" \
  --output "$OUTPUT_VIDEO" \
  --export-srt "$OUTPUT_SRT" \
  --delete-words-file "$DATA_DIR/fillerwords_zh.txt" \
  --merge-gap-ms 120 \
  --padding-ms 80 \
  --snap keyframe \
  --reencode auto
