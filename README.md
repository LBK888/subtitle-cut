<div align="center">

# 🎬 Subtitle-Cut (Qwen3 Edition)
_基於 Qwen3 ASR 模型的 AI 自動字幕剪輯與語音辨識工具_  
AI-powered subtitle cutting and speech recognition tool based on the **Qwen3 ASR** model.

---

[![Python](https://img.shields.io/badge/Python-3.10--3.12-blue.svg?logo=python)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![OS](https://img.shields.io/badge/Platform-Windows-blue.svg?logo=windows)](https://github.com/foxmooner2021/subtitle-cut)

---

<img src="docs/preview_screenshot.png" width="640" alt="Subtitle-Cut preview screenshot" />
<br/>

</div>

---

## ✨ 專案簡介 | Overview

**Subtitle-Cut** 是一個輕量級的本地化 AI 字幕剪輯與語音辨識工具。本專案基於 [subtitle-cut] 與 [QwenASRMiniTool] 進行改寫，採用 **Qwen3 ASR 模型**，並全面將後端升級為 **FastAPI** 架構，同時新增了**區域網路 (LAN) 服務功能**，讓多裝置存取更加便利。

**Subtitle-Cut** is a lightweight, localized AI subtitle editing and speech recognition tool. This project is a modified version based on [subtitle-cut] and [QwenASRMiniTool], utilizing the **Qwen3 ASR model**. The backend has been completely rewritten using **FastAPI**, and a **Local Area Network (LAN) service feature** has been added for convenient multi-device access.

### 🌟 核心功能 | Core Features

- 🎧 **Qwen3 ASR 模型**: 支援高精準度的語音轉文字辨識 (包含 0.6B 與 1.7B 模型)，並支援 OpenVINO (CPU) 與 Vulkan (GPU) 推理。
  **Qwen3 ASR Model**: Supports high-accuracy speech-to-text recognition (including 0.6B and 1.7B models) with OpenVINO (CPU) and Vulkan (GPU) inference support.
- ⚡ **FastAPI 後端 & 區域網路服務**: 高效能後端架構，支援在區域網路內透過瀏覽器跨裝置 (如手機、平板) 使用服務與麥克風。
  **FastAPI Backend & LAN Service**: High-performance backend supporting cross-device usage and microphone access via browser within a local network.
- 🎬 **影音轉字幕**: 支援 MP3/WAV/MP4/MKV 等多種格式自動提取音軌並產生 SRT 字幕。
  **Audio/Video to Subtitles**: Automatically extracts audio from various formats (MP3/WAV/MP4/MKV, etc.) and generates SRT subtitles.
- 🎤 **即時語音辨識**: 支援麥克風輸入，透過 VAD 靜音偵測自動分段處理辨識。
  **Real-time Recognition**: Supports microphone input with automatic segmentation via VAD silence detection.
- 👥 **說話者分離 (Speaker Diarization)**: 自動標記不同說話者的身份 (可指定人數，適合雙人對談等情境)。
  **Speaker Diarization**: Automatically tags different speakers' identities (supports specifying the number of speakers, ideal for dialogues).
- 🌍 **多語系辨識**: 支援中文、日文、英文等 30 種語系，並可自動偵測。
  **Multi-language Support**: Supports 30 languages including Chinese, Japanese, and English, with auto-detection.
- 📝 **辨識提示 (Prompt)**: 可輸入關鍵字或上下文參考，提升專有名詞與歌詞辨識準確度。
  **Recognition Prompts**: Input keywords or context references to improve the accuracy of proper nouns and lyrics.
- 📦 **批次處理**: 支援一次匯入多個影音檔案進行排程辨識。
  **Batch Processing**: Supports importing multiple audio/video files for scheduled recognition.
- 🌗 **使用者介面**: 支援簡繁體中文輸出轉換 (內建 OpenCC)。
  **User Interface**: Supports Simplified/Traditional Chinese output conversion (built-in OpenCC).

---

## ⚙️ 環境配置 | Setup Instructions

### 0️⃣ 解壓縮專案 / Unpack Project
將專案資料夾解壓縮到任意位置，例如 `E:\subtitle-cut\`。
Extract the project folder to any path, e.g., `E:\subtitle-cut\`.

### 1️⃣ 安裝 Python 3.10 - 3.12 / Install Python 3.10 - 3.12
請確保系統已安裝 Python 3.10 至 3.12 版本。安裝時請**勾選 “Add Python to PATH”**。
Ensure Python 3.10 - 3.12 is installed on your system. During setup, **enable “Add Python to PATH”**.

### 2️⃣ 初始化虛擬環境 / Create Virtual Environment
在專案目錄下執行安裝腳本，該腳本會建立虛擬環境並自動安裝 `requirements.txt` 中的依賴。
Run the installation script in the project directory. It will create a virtual environment and install dependencies from `requirements.txt`.
```bash
install.bat
```

### 3️⃣ 下載並設定 FFmpeg / Install FFmpeg
若要處理影片檔，系統需要 FFmpeg 進行音軌提取。
To process video files, FFmpeg is required for audio extraction.
- **自動下載**: 系統在需要時會提示並引導一鍵下載 FFmpeg。
  **Auto-download**: The system will prompt and guide you to download FFmpeg automatically when needed.
- **手動下載**: [FFmpeg 官方下載頁面](https://ffmpeg.org/download.html)，並將其加入系統 PATH 或放置於指定資料夾。
  **Manual download**: [FFmpeg Official Download](https://ffmpeg.org/download.html), and add it to system PATH or place it in the designated folder.

### 4️⃣ 啟動專案 / Run the App
使用批次檔啟動 FastAPI Web 服務：
Use the batch file to start the FastAPI web service:
```bash
run_webapp.bat
```

啟動後，終端機會顯示服務網址：
- **本機存取 (Local Access)**: `http://127.0.0.1:8000`
- **區域網路存取 (LAN Access)**: `http://<您的區域網路IP>:8000`

> 💡 **提示 (Tips)**:
> 首次啟動時，若尚未下載模型，程式會引導下載所需的 Qwen3 模型 (0.6B 或 1.7B) 及其他附屬模型 (約需要 1.2GB ~ 4.3GB 不等)。
> Upon the first launch, if models are not downloaded, the system will guide you to download the required Qwen3 models (0.6B or 1.7B) and other dependency models (ranging from 1.2GB to 4.3GB).

---

## 🧩 模型與依賴 | Models & Dependencies

| 模組 (Module) | 用途 (Purpose) | 來源 (Source) |
| ------------- | -------------- | ------------- |
| **Qwen3-ASR** | 核心語音辨識模型 (0.6B/1.7B) | Alibaba Qwen Team |
| **Silero VAD** | 語音靜音與片段偵測 | snakers4/silero-vad |
| **Speaker Diarization** | 說話者分離 (聲音特徵聚類) | altunenes |
| **OpenVINO / Vulkan** | CPU / GPU 推理加速引擎 | Intel / chatllm.cpp |
| **FastAPI** | 高效能 Web 伺服器與 API 後端 | tiangolo (MIT) |
| **FFmpeg** | 影音軌道提取與處理 | FFmpeg (LGPL/GPL) |

---

## 📂 目錄結構 | Directory Layout

```
subtitle-cut/
├─ models/           # 離線模型存放目錄 (Qwen3, VAD, Diarization) / Models directory
├─ src/              # 核心原始碼 (FastAPI 後端與 Web 前端) / Core source code
├─ QwenASRMiniTool/  # QwenASRMiniTool 原始碼與工具 / QwenASRMiniTool core tools
├─ third_party/      # 第三方依賴路徑 / Third-party dependencies
├─ install.bat       # 環境初始化腳本 / Environment setup script
├─ run_webapp.bat    # Web 服務啟動腳本 / Web service startup script
├─ requirements.txt  # Python 依賴清單 / Python dependencies
└─ README.md         # 專案說明文件 / Project documentation
```

---

## 📜 許可證 | License

本專案原始碼基於 **MIT License** 開源發布。
The source code of this project is released under the **MIT License**.

> Qwen3 ASR、FFmpeg 及其他整合之開源套件保留其原始授權方式。
> Qwen3 ASR, FFmpeg, and other integrated open-source packages retain their original licenses.

---

## ❤️ 致謝 | Acknowledgements

* 原創專案 1 (Original Project 1): [subtitle-cut](https://github.com/foxmooner2021/subtitle-cut)
* 原創專案 2 (Original Project 2): [QwenASRMiniTool](https://github.com/dseditor/QwenASRMiniTool)

---

<div align="center">

A modified version of [subtitle-cut] and [QwenASRMiniTool] by **kAI LBK888** 

</div>