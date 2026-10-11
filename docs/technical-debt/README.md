# V1.2.1 技术债交付索引

实施分支 `codex/technical-debt-v1.2.1`，源基线 `mobile-app/a4ece80`。本轮只在隔离工作树修复，没有打包、安装、推送或发布，没有启动、停止或重启现场验证程序。

以上及下文“本轮”指技术债修复阶段。后续用户授权升级、打包、推送和发布 V1.2.2；实际产物检查与保留限制见 [V1.2.2 发布验收](../V1.2.2发布验收-2026-10-11.md)。现场主工作区、冻结模型及验证程序仍受保护。

- [分步验收与限制](acceptance.md)
- [原始源码快照索引](source-index.json)：92 个文件，原始字节 ZIP 在本聊天工作树 `.local/source-snapshot/v1.2.1-original.zip`，未提交二进制；完整 SHA-256 已登记。原实验冻结清单未重写。
- [共享文件清单](shared-files.json)与[必要分叉原因/摘要](expected-drift.json)：动态发现新增文件，单端缺失、双方删除既有模块、分叉摘要变化均失败。文本只规范 CRLF，二进制按原字节核对；冻结检查始终保留原始字节哈希。
- [环境精确锁](../../requirements-locks)：桌面 26、测试 32、Android 15、研究 43、Kronos 58、桌面构建工具 8 个依赖。测试锁在技术债阶段为 27 项，V1.2.2 远端 CI 补齐研究特征离线单测所需的 5 项既有依赖；版本取自本机已验证元数据，没有在现有环境安装或升级包。每个环境附独立版本来源及验证范围；Electron/Playwright 的依赖版本和 package-lock 完整性值未变。

## 离线检查

本机完整验收入口：

```powershell
$env:NODE_PATH = 'E:\codx project\期货评估器\electron\node_modules' # 本机仅只读复用已验证测试依赖
$env:QA_BROWSER = 'msedge'
python -B tools/check_offline.py --python38 .local/python38/python.exe
python -B tools/source_guard.py --git-only
```

普通克隆使用自身精确锁建立新的测试环境后运行 `check_offline.py --git-only`；CI 以桌面和 Python 3.8 两个作业验证，不运行真实 GUI/实盘脚本或 Gradle 构建。没有 `.local` 原始 ZIP 或 E 盘时，可移植守卫核对原快照 Git 对象、HEAD、索引和工作文件；本机仍必须通过 `--require-main` 的原始字节/现场检查。

Python 3.8 测试工具只在 `.local/python38` 解压，没有安装器或注册表修改。[来源与哈希](python38-runtime.json)来自 [Python 官方 3.8.10 发布页](https://www.python.org/downloads/release/python-3810/)。真实运行共享/Android Fake 测试，不能替代 Android 原生 ABI、设备或安装包验收。Android 锁的 15 个依赖与历史 V1.2.1 APK 的内嵌 METADATA 逐项一致；本轮没有重建该 APK。

## 正式发布门禁

`assembleDebug` 保留原开发路径。正式 release 禁止自动回退 debug 签名：要求 `mobile-app/keystore.properties` 中有 `storeFile / storePassword / keyAlias / keyPassword / certificateSha256`。相对 storeFile 路径相对于 `mobile-app/app`，生产证书指纹是公开的 SHA-256；本轮不创建或修改任何密钥/凭据文件。

未来正式构建前先通过离线检查、目标环境依赖检查和签名配置检查；构建后的 APK 必须另行通过签名、应用 ID、版本、ABI 和外部预期哈希核验：

```powershell
python -B tools/release_gate.py
python -B tools/release_gate.py --artifact <APK> --sha256 <预期SHA256> --apksigner <SDK工具> --aapt <SDK工具>
```

缺签名配置时命令明确失败；仅配置存在的检查不宣称密码、生产签名或产物已经验收。APK 核验调用 SDK 的 verify/dump，拒绝 debug 证书。本轮 SDK 验证使用 Fake 输出和临时 ZIP 测试，没有执行打包或签名任务。[Chaquopy 官方 pip 文件用法](https://chaquo.com/chaquopy/doc/current/android.html)用于接入 Android 精确依赖锁。

## 只读健康检查

`python -B tools/health_readonly.py --root <现场工作目录>` 只读取两份状态和 PID 存活信息，不导入监督或研究模块，不读取账本/权重，不写采集状态、停止标记或报告。PID 存活与心跳是旁路信号，输出明确标注进程身份尚未验证；不会据此自动修复或重启。

## 保留限制

- HTTP 工作按原超时自然完成。关闭等待超时保留旧行情线程引用；Android 后台入口继续等待旧 DIFF/恢复线程退出，期间再次启动不会创建新 Hub。不能立即强制取消已经运行的请求。
- 尚未构建 Windows/Android 产物、运行设备生命周期测试、验证实际生产签名或执行远端 CI。
- 研究环境只锁定现有元数据并运行已有离线单测；未加载模型权重、执行 GPU 推理、训练或更新实验结果。
- 冻结源码整理、旧模型副本清理、安装包与上线切换继续留待验证结束后的独立任务。
