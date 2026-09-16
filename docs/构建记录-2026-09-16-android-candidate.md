# Android 候选包构建记录（2026-09-16）

## 候选信息

| 项目 | 值 |
| --- | --- |
| 候选文件 | `dist/ZA量化-手机版-v1.1.1-candidate-b815252.apk` |
| 构建类型 | Debug（Android debug keystore 签名） |
| 源码提交 | `b815252c96f415c3eb92f68092de4ef8084309cb` |
| 构建时间 | 2026-09-16 17:22 +08:00 |
| 文件大小 | 26,250,913 bytes |
| SHA-256 | `9b02f8ffaab6afc39637dbad6ea4eefe0b02e4ff831532e71391f1c4b3c77603` |

## 构建命令

```powershell
cd mobile-app
.\gradlew.bat --no-daemon :app:assembleDebug
```

构建结果：`BUILD SUCCESSFUL`（Gradle 8.9、JDK 17、AGP 8.2.2、Chaquopy 16.1）。

## 构建前验证

```powershell
python -m unittest discover -s tests
python tools/check_core_drift.py --strict
```

结果：56/56 Python 单元测试通过；桌面与移动端核心文件无预期外漂移。

## 范围与限制

- 包含 2026-09-16 A-G 修复，以及 Android 14+ `dataSync` 前台服务类型专用权限。
- 这是待真机交易时段验收的候选包，尚未验证 A、B、C、D、E、F、G 的最终设备行为。
- 这是 debug 签名包，不可作为正式 release 发布物；release 签名密钥和发布流程仍待建立。
- 构建存在已记录的 Gradle 弃用警告，不影响本次 debug 构建成功。
