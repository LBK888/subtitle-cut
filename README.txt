0.解压项目

1.安装 Python 3.11（安装包在subtitle-cut\third_party\)
----安装时保留 py launcher（默认勾选）或将 Python 加入 PATH”，至少保留其中一种方式。

2.如需使用 GPU，需安装NVIDIA CUDA 11.8，可直接下载百度盘对应文件或自行去官网下载。
----https://developer.nvidia.com/cuda-toolkit-archive
下载后执行安装，选择 “Express” 或 “Custom” 都能安装 CUDA runtime 与驱动（如果系统已有更新版驱动，可只装 toolkit）。安装完成后，torch==2.3.1+cu118、onnxruntime-gpu 等就能正常加载。

3.针对 CUDA 11.8，配套的 cuDNN 官方组合是 cuDNN 8.9.x 系列（8.9.0～8.9.7 均支持）。
----https://developer.nvidia.com/rdp/cudnn-archive
可以到 NVIDIA Developer 的 cuDNN 下载页，选择：CUDA Version: 11.x
下载后解压，将 bin, include, lib 目录里的文件分别复制到 CUDA Toolkit 安装路径下对应的 bin, include, lib\x64 下，或按需求设置 CUDNN_PATH。完成后即可让 PyTorch/ONNXRuntime 正常调用 cuDNN。

4.下载ffmpeg免安装版，把ffmpeg\bin目录下的所有文件复制到subtitle-cut\third_party\ffmpeg\bin同名目录下。
如果非要自定义其他目录，可以以文本格式打开install.bat搜索ffm，大概在那个位置可以自己定义路径，具体的我不会，如果你也不会，就下载免安装版，复制bin目录覆盖subtitle-cut\third_party\ffmpeg\下的同名目录。

3.运行 install.bat将创建虚拟环境。

4.运行run_webapp.bat启动项目。

5.离线模型会在首次运行程序时自动下载，也可单独下载模型包解压到models。

6.ImDisk能够将一部分虚拟内存转为临时虚拟磁盘以加速视频读写效率，如果决定使用，可以自行安装并把安装后的路径加入系统环境path。
