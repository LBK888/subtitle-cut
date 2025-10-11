# Third-Party Licenses / 第三方依赖许可说明

本项目中使用了若干第三方组件和模型。以下为各依赖及其授权信息：

---

### 🧠 Paraformer (by Alibaba)
- License: **Apache License 2.0**
- Source: [https://github.com/alibaba-damo-academy/FunASR](https://github.com/alibaba-damo-academy/FunASR)
- Notes: 本项目仅调用 Paraformer 模型推理接口，不修改其源代码。

---

### ⚙️ ONNX Runtime (by Microsoft)
- License: **MIT License**
- Source: [https://github.com/microsoft/onnxruntime](https://github.com/microsoft/onnxruntime)

---

### 🎬 FFmpeg
- License: **LGPL 2.1 / GPL**
- Website: [https://ffmpeg.org/legal.html](https://ffmpeg.org/legal.html)
- Notes: 本项目未分发 FFmpeg，本地运行需用户自行下载并将可执行文件放置于  
  `third_party/ffmpeg/bin/` 目录中。  
  This project does **not** distribute FFmpeg binaries.  
  Users must obtain their own FFmpeg build and place it under `third_party/ffmpeg/bin/`.

---

### 🧩 Other Dependencies
- NumPy, Flask, Requests, etc.
- License: **BSD / MIT compatible**

---

**Summary / 总结：**  
本项目源代码基于 **MIT License** 开源，所有外部依赖保持其原有授权方式。  
The main code of this project is released under the **MIT License**,  
and all third-party libraries retain their original licenses.
