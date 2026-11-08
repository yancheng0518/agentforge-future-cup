# 专用 WSL2 + Docker Engine 环境

2026-11-08，本机为 Windows 11 家庭版 build 22000。此版本不满足当前 Docker Desktop 的最低版本要求，但满足微软 WSL2 的系统要求。因此本项目采用专用 WSL2 Ubuntu 发行版内运行 Docker Engine 的路径，不升级整个 Windows，不开放无鉴权 Docker TCP 服务。

## 官方来源与下载校验

- [Microsoft WSL 安装说明](https://learn.microsoft.com/en-us/windows/wsl/install)：Windows 10 build19041+ 或 Windows11 支持该安装路径；离线安装可以使用官方 MSI。
- [Microsoft WSL 3.0.3 官方发布](https://github.com/microsoft/WSL/releases/tag/3.0.3)：下载 `wsl.3.0.3.0.x64.msi`，367,575,040 字节；SHA256 `ae977286974255bd26bf925f00f40f5692c6b2084155cbc0563c09aea091f211`。本机校验与 GitHub 发布值一致，Authenticode 状态 Valid，签名人为 Microsoft Corporation。
- [Canonical Ubuntu22.04 WSL rootfs](https://cloud-images.ubuntu.com/wsl/jammy/current/)：`ubuntu-jammy-wsl-amd64-ubuntu22.04lts.rootfs.tar.gz`，341,130,963 字节；SHA256 `1483cc5c1dce13064f774834cbffdff226559fd522a67a381a8ea77d63fb4109`。本机与同站点 SHA256SUMS 校验一致。该固定文件为2025-03构建，发行版内软件包需由签名APT仓库更新/安装。
- [Docker Engine Ubuntu 安装文档](https://docs.docker.com/engine/install/ubuntu/)：Ubuntu22.04为支持发行版，安装使用 Docker 官方签名 APT 仓库。

下载文件位于 `.agentforge/sandbox-install/`，不提交仓库。曾额外检查微软旧版 `wsl_update_x64.msi`，其版本为5.10.16，虽然微软签名有效，但本次优先使用当前WSL完整运行时以避免以旧内核搭建新环境。

## 安装步骤

1. 当前进程不是管理员。`scripts/install-wsl-runtime.ps1` 需要Windows UAC提权：只确认现有WSL和VirtualMachinePlatform功能已启用，检查MSI签名/哈希，安装WSL运行时。使用`/norestart`，不会主动重启机器。若系统要求重启，会在结果文件标记，必须等用户处理。
2. 安装结果写入 `.agentforge/sandbox-install/wsl-install-result.json`，原始 MSI 日志在同目录 `wsl-install.log`。
3. `scripts/setup-wsl-sandbox.ps1` 将发行版注册为 `AgentForge`，虚拟磁盘位于项目D盘 `.agentforge/wsl/AgentForge`。不会注销或覆盖其他发行版。
4. `scripts/provision-wsl-docker.sh` 只接受专用 Ubuntu22.04，以Docker官方APT密钥验证并安装Engine、CLI、containerd、buildx/compose，启动发行版内daemon。
5. 在该发行版构建 `agentforge-test:py313`；测试仍通过运行时的网络关闭、只读挂载、低权限和资源限制执行。

Windows侧调用格式：`wsl --distribution AgentForge --user root --exec docker ...`。项目路径必须先用该发行版的`wslpath -a`转换后作为Linux Docker的bind source。该调用不需要Windows Docker Desktop，也不在TCP上暴露Docker控制端口。

Windows重启或发行版停止后，执行 `powershell -ExecutionPolicy Bypass -File .\scripts\start-sandbox.ps1`。该脚本仅启动已经安装的专用发行版Docker服务并检查验收镜像，不安装软件，不更改防火墙或暴露TCP。随后运行项目 `scripts/preflight.py`。

## 本次状态

用户确认Windows UAC后，安装脚本于2026-11-08 15:15:57–15:16:29执行完成：MSI退出码0，`success=true`，`restart_required=false`。WSL和VirtualMachinePlatform两项现有Windows功能均为Enabled；没有升级或重启Windows。

随后已真实完成专用发行版导入、Docker Engine安装与验收镜像构建，`setup-wsl-sandbox.ps1`退出码0。实际结果：

- WSL版本：3.0.3.0；Linux kernel：6.18.54.1-microsoft-standard-WSL2。
- 专用发行版：AgentForge，Ubuntu22.04，PID1为systemd，已有`/etc/wsl.conf`的`systemd=true`生效。
- Windows磁盘：`D:\未来杯\.agentforge\wsl\AgentForge\ext4.vhdx`；项目映射路径：`/mnt/d/未来杯`。
- Docker Engine与CLI：29.9.3；containerd：2.4.2；存储驱动overlayfs，cgroup v2；默认seccomp与cgroup namespace开启。
- Docker服务参数：`dockerd -H fd:// --containerd=/run/containerd/containerd.sock`；实查2375/2376没有监听，未开放Docker TCP控制端口。
- 验收镜像：`agentforge-test:py313`，镜像ID `sha256:1edc3cba75dd1917bb78226e897fdb801a0272ea2e02319345de0bd3b8cae225`，镜像配置用户`65534:65534`。
- 本次实际拉取基础镜像：`python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`。应将此digest固定到正式复现配置，不能只记录浮动tag。
- 重启后启动脚本`start-sandbox.ps1`已真实执行并返回0，Linux引擎与验收镜像都可访问。初次执行发现Windows PowerShell5向`sh -c`转义失败，已改为直接调用`ps`、`systemctl`或`service`并复验通过。

Docker的安装与镜像构建是真实完成；这还不能替代项目隔离探针、Todo独立验收以及真实模型修正闭环的结果。这些由项目运行记录单独证明。Ubuntu基础rootfs为固定的旧发行镜像，本次仅更新安装所需证书/curl与官方Docker包，没有进行整套Linux发行版升级；后续维护仍应按团队环境策略更新安全包。
