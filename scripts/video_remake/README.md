# 2026-11-16 演示视频重制

推荐成片为 `submission/演示视频-复核修正版-r2-20261116.mp4`。原 `演示视频.mp4` 和首发 `演示视频-新版-20261116.mp4` 均不覆盖。浅色画面、晓晓中文女声、27 个镜头、8 章、逐句字幕，约 3 分 23 秒。

## 事实与素材边界

- 开发过程：历史真实模型运行 `4af425ca8a1e44beb8b5cb2110e5edb0`，并非视频拍摄时重新生成；14 项 HTTP 验收、独立 12 项 HTTP 用例分别保存。
- 修正过程：`1ea0bc86900e4113809f82c22f66a9b5` 是独立副本的明确预设缺陷；1 项失败后修正为 12 项通过。不计为自然失败修正成功。
- 浏览器：本次重新操作原自然项目 v2，创建、完成、筛选、刷新，随后停止预览。40 次真实 HTTP 请求；0 新模型请求。详情见 `output/video-remake-20261116/preview-receipt.json`。
- 实际操作使用 CUA。截图不改写 UI 内容，剪辑只做裁切、重点放大和字幕排版。部分静态详情采用真实截图；事件回放与看板操作采用真实帧序列。每个镜头的来源和裁切记录在 `edit.json`。
- 配音是合成语音 `zh-CN-XiaoxiaoNeural`，语速 -5%，用户试听确认。无背景音乐、无真人声音克隆。

## 复现导出

视频制作环境与项目开发环境分离，不改动 `.venv` 和应用依赖。Windows 需要 `C:/Windows/Fonts/msyh.ttc` 与 `msyhbd.ttc`；字体不随素材包分发。

```powershell
py -m venv artifacts/private/video-tools-20261116
.\artifacts\private\video-tools-20261116\Scripts\python.exe -m pip install -r scripts/video_remake/requirements-media.txt
```

从 [r2 GitHub Release](https://github.com/yancheng0518/agentforge-future-cup/releases/tag/video-remake-20261116-r2) 下载 `video-remake-20261116-r2-source.zip`，并使用同名 Git 标签的代码，在仓库根目录解压素材。包内保留 `submission/video-work/remake-20261116/` 相对路径，含已确认的旁白、字幕、原始截图、入片录屏及其帧和时间戳。既有源码仓库没有直接追踪大体积视频和工作缓存。

```powershell
# 现成旁白与字幕直接复用，无语音或模型网络请求。
.\artifacts\private\video-tools-20261116\Scripts\python.exe scripts/video_remake/build_edit.py
.\artifacts\private\video-tools-20261116\Scripts\python.exe scripts/video_remake/render.py --config scripts/video_remake/edit.json --output submission/演示视频-复核修正版-r2-20261116.mp4 --preset fast
.\artifacts\private\video-tools-20261116\Scripts\python.exe scripts/video_remake/check_sync.py --video submission/演示视频-复核修正版-r2-20261116.mp4 --output-dir submission/video-work/remake-20261116/reproduced-sync
.\artifacts\private\video-tools-20261116\Scripts\python.exe scripts/video_remake/package_review.py --video submission/演示视频-复核修正版-r2-20261116.mp4 --output-dir output/video-remake-20261116-r2 --source-archive submission/video-remake-20261116-r2-source.zip
```

可选：重建录屏 clip 时运行 `encode_captures.py`；它按真实时间戳补帧，保留动作顺序与原速。`board-actions-edit` 取新增和完成筛选两段各前 3.8 秒，剪去等待，不循环伪造操作。

可选：重新合成旁白时运行 `synthesize_narration.py`。有效缓存不会重新请求语音；缺失缓存会通过 edge-tts 请求在线语音服务，仅传入公开旁白文案。服务变更可能影响声音与逐字边界，不承诺跨版本生成相同字节。

```powershell
.\artifacts\private\video-tools-20261116\Scripts\python.exe scripts/video_remake/encode_captures.py
.\artifacts\private\video-tools-20261116\Scripts\python.exe scripts/video_remake/synthesize_narration.py
```

最终输出包含 MP4、SRT、镜头联系表与 `.build.json`。后者保存每项素材 SHA-256、FFmpeg 版本、字幕时点、原片保护校验和完整解码结果。使用相同素材、字体和媒体工具才能比较字节哈希；一般重建应检查画面/音频与事实一致性。

`render.py` 以实际视频帧数为时间基准：30fps 下每帧对应 1600 个 48kHz PCM 采样，全部音频合并后只做一次 AAC 编码。本机可加 `--reuse-build` 复用旧缓存中的相同视觉片段；完整素材包不包含缓存，因此全新环境按上面的无复用命令重建。`check_sync.py` 按媒体时间戳检测音频间隙/重叠，比较原旁白音量包络，并从最终视频像素定位字幕；它输出测量报告，不替代听审或语音内容识别。

## 已保留的制作失败

初始服务未就绪、WSL 生命周期与本次端口 Origin 配置问题先处理后录制；没有改应用代码或原密钥。首次帧提取遇到 Windows 编码问题，改为 UTF-8 错误替换后成功。

部分早期 CDP 片段帧数过少；两个录制尝试因目标定位失败中断。第一版合成抽查还发现四段跨标签截图序列画面错误：`graph-node-detail-live`、`graph-events-live`、`project-source`、`preview-stop`。相关原始素材与初稿在本机工作目录保留，未进入最终素材包；公开质检记录列明问题和替换方式。最终改用逐张核验的正确截图及正确的 CDP 协作回放片段。

这些是视频制作/浏览器自动化问题，不是候选代码自然失败，也不增加模型修正成功次数。项目的旧运行、失败证据、正式旧视频和既有比赛提交包保持不变。

## 再次复核修正

首发片的分段 AAC 在接缝处有 52 处时间戳重叠。按时间戳校正后未测得累计音画漂移；直接串接解码采样产生的初步漂移推断已撤回。r2 改为统一音轨编码，处理此兼容性问题，并修正 06-03 镜头中“下一步，继续改进”标题顶部裁切。首发视频、原始量测和中间候选记录分别保留，详见 `output/video-remake-20261116-r2/`。
