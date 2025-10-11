# FFmpeg 安装指引

## Windows
1. 从 [FFmpeg 官网](https://ffmpeg.org/download.html) 下载预编译版本。
2. 解压后将 in 目录加入系统 PATH。
3. 打开新终端执行 fmpeg -version 验证。

## macOS (Homebrew)
`ash
brew install ffmpeg
ffmpeg -version
`

## Linux (Debian/Ubuntu)
`ash
sudo apt update
sudo apt install -y ffmpeg
ffmpeg -version
`

## 其他平台
请参考官方文档或通过源码编译，确保版本 ≥ 6.0。
