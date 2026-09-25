# ZA量化 手机版（Android APK · Chaquopy 内嵌 Python）

> 架构：Chaquopy 内嵌 Python 后端（`app/src/main/python/`，与桌面端共用 market/tqdiff 逻辑），
> WebView 加载同源前端 `http://127.0.0.1:8000/`。无云服务器、不依赖电脑。

## 构建环境要求（已验证的组合）

| 组件 | 版本 | 说明 |
|---|---|---|
| JDK | 17（Temurin 17.0.20 验证通过） | `JAVA_HOME` 指向它 |
| Gradle | **8.9**（wrapper 已提交，`gradlew` 自动下载） | 本机也可用 `E:/tools/gradle-8.2` 直跑 |
| Android Gradle Plugin | 8.2.2（`build.gradle` 里 force 钉死） | Chaquopy 16.1 只兼容 AGP 8.0~8.2 |
| Chaquopy | 16.1.0 | `apply plugin: 'com.chaquo.python'` |
| Android SDK | Platform 34 + Build-Tools 34.0.0 | `local.properties` 写 `sdk.dir=E:/android-sdk` |
| target ABI | arm64-v8a、x86_64（`ndk.abiFilters`） | Chaquopy 必须显式声明 |

## 构建

```bash
cd mobile-app
# 首次：确认 local.properties（不进仓库）：
#   sdk.dir=E:/android-sdk
./gradlew --no-daemon assembleDebug          # 干净机器：wrapper 自动下载 Gradle 8.9
# 产物：app/build/outputs/apk/debug/app-debug.apk
# 交付：复制为 dist/ZA量化-手机版-v1.1.3.apk 并记录 SHA-256
```

> 本机若无外网下载 Gradle 发行版，可直接用本地已装 Gradle：
> `E:/tools/gradle-8.2/bin/gradle.bat --no-daemon assembleDebug`

## release 签名（V1.1.3 起）

- 正式发布用 `./gradlew --no-daemon assembleRelease`，签名配置读 `mobile-app/keystore.properties`
  （**不进仓库**，指向仓库外 keystore：本机为 `E:/keys/zaquant-release.keystore`，alias `zaquant`）。
  文件缺失时 release 构建自动回退 debug 签名，clone 后仍可直接出包。
- keystore 与密码**离线备份**（密码在 `keystore.properties`）。丢失后无法再对已发布应用出升级包。
- 验签：`E:/android-sdk/build-tools/34.0.0/apksigner.bat verify --print-certs <apk>`，
  证书 SHA-256 应为 `f9e19c0c2c435a37c801b6876f20943d81350d8f42585077d2dd473e448d037d`。
- **签名变更不能覆盖安装**：从 debug 包升级到 release 包必须先卸载（会清 App 数据）；
  此后所有升级必须用同一 keystore 签名。
- minify 保持 `false`：Chaquopy 的 Python/Java 桥接在混淆下风险高，无混淆需求。

## 已知构建警告（已核实、可接受）

1. **`Failed to compile to .pyc format: buildPython version 3.x is incompatible`**
   —— buildPython（构建机 Python）与目标运行时（Chaquopy 内置 CPython 3.8）大版本不一致时，
   Chaquopy 跳过 .pyc 预编译。**仅影响首次启动速度（运行时即时编译），不影响正确性。**
   若要消除：安装与目标一致的 Python（3.8）并设 `python { buildPython "..." }`。
2. **`org.gradle.util.VersionNumber has been deprecated`**
   —— 栈指向 `apply plugin: 'com.chaquo.python'`，来自 **Chaquopy 16.1 插件内部**，
   我方构建脚本无此用法（已用 `=` 赋值与单字符串 classpath）。随 Chaquopy 升级消失。
3. **`android.overridePathCheck=true` 实验性开关**
   —— 项目路径含中文，必须保留该开关，否则 AGP 路径检查直接报错。

## 工程要点（改代码前先读）

- `python { pip { install ... } }` 块**必须放在 `android.defaultConfig` 内部**
  （Chaquopy 的 createDsl 把 python 扩展挂在 defaultConfig 上，放 `android{}` 里会报
  "Could not find method python()"）。
- 后端 Python 源码在 `app/src/main/python/`，与根目录桌面版是**两份拷贝**：
  改 `tqdiff/`、`market/`、`services.py` 等核心逻辑时**两端要同步**，
  并跑 `python tools/check_core_drift.py` 确认没有意外分叉。
- App 私有可写目录通过 Java 系统属性 `za.filesdir` 传入后端
  （`MainActivity.java` 设置，`config.py`/`backend_main.py` 读取），凭据与合约缓存都写在那里。
- 合约目录采用 gzip 下载 + ijson 流式解析 + pickle 精简索引（`tqdiff/symbol_index.py`），
  索引文件约 25MB，二次启动秒级加载；原始 .gz 约 8.3MB 也保留在私有目录。

## 应用图标与后台运行

- Launcher 图标的可编辑母版为 `../assets/icon-android.svg`。Android 资源位于
  `app/src/main/res/mipmap-anydpi/`（API 24/25 矢量图标）、
  `mipmap-anydpi-v26/`（自适应图标）和 `drawable/`（前景、单色层及通知小图标）。
  矢量资源在支持的屏幕密度下由系统绘制，无需维护多套 PNG。
- Manifest 引用 `@mipmap/ic_launcher` 和 `@mipmap/ic_launcher_round`；前台服务通知使用
  `@drawable/ic_notification`。打包后用 `aapt dump badging` 检查应用图标非空。
- vivo V2528A（Android 16）真机验证发现：系统“后台耗电管理”若选中“智能控制后台耗电”，
  锁屏后可能冻结本应用，即使前台服务与 WakeLock 已启动、应用也在电池优化白名单中。
  在**设置 → 应用 → ZA量化 → 电量 → 后台耗电管理**中改为“允许后台耗电”后，
  约 70 秒锁屏期间本机状态接口保持可响应。此设置会增加后台耗电；换设备或系统升级后
  应重新验证。
