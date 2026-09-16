# Mobile Lockscreen Keepalive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the embedded Android market backend responsive during a locked screen by combining a foreground service, CPU wake lock, and one-time battery-optimization exemption request.

**Architecture:** `MainActivity` starts `BackendService` with the Android O+ foreground-service API and requests battery-optimization exemption once when needed. The service publishes its foreground notification before starting Chaquopy, holds a non-reference-counted `PARTIAL_WAKE_LOCK` for its lifecycle, and releases it on destruction or startup failure. Manifest permissions make both OS-managed behaviors legal.

**Tech Stack:** Android Java, Android SDK API 34, Gradle/AGP, Chaquopy, Python `unittest` source checks, ADB real-device verification.

---

## File Structure

- Modify: `mobile-app/app/src/main/AndroidManifest.xml` - wake-lock and battery-exemption declarations.
- Modify: `mobile-app/app/src/main/java/com/zaquant/mobile/MainActivity.java` - foreground-service startup and one-time exemption request.
- Modify: `mobile-app/app/src/main/java/com/zaquant/mobile/BackendService.java` - wake-lock acquisition/release around Python backend lifecycle.
- Modify: `tests/test_android_manifest.py` - static regression checks.
- Create: `docs/验证记录/2026-09-16/报告-真机候选-<commit>.md` - post-test evidence report.

### Task 1: Capture the Keepalive Contract in a Failing Test

**Files:**
- Modify: `tests/test_android_manifest.py`

- [ ] **Step 1: Define Java source paths**

After `MANIFEST_PATH`, add:

```python
JAVA_ROOT = MANIFEST_PATH.parent / "java" / "com" / "zaquant" / "mobile"
BACKEND_SERVICE_PATH = JAVA_ROOT / "BackendService.java"
MAIN_ACTIVITY_PATH = JAVA_ROOT / "MainActivity.java"
```

- [ ] **Step 2: Write the failing lifecycle and permission test**

Add this test method:

```python
    def test_lockscreen_keepalive_declares_wakelock_and_lifecycle_guards(self):
        root = ET.parse(MANIFEST_PATH).getroot()
        permissions = {
            element.get(f"{ANDROID_NS}name")
            for element in root.findall("uses-permission")
        }
        self.assertIn("android.permission.WAKE_LOCK", permissions)
        self.assertIn("android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS", permissions)

        service = BACKEND_SERVICE_PATH.read_text(encoding="utf-8")
        activity = MAIN_ACTIVITY_PATH.read_text(encoding="utf-8")
        self.assertIn("PowerManager.PARTIAL_WAKE_LOCK", service)
        self.assertIn("void onDestroy()", service)
        self.assertIn("wakeLock.release()", service)
        self.assertIn("startForegroundService", activity)
        self.assertIn("ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS", activity)
```

- [ ] **Step 3: Run the focused test to verify it fails**

```powershell
python -m unittest tests.test_android_manifest.AndroidManifestTests.test_lockscreen_keepalive_declares_wakelock_and_lifecycle_guards -v
```

Expected: failure because the manifest and Java source do not yet contain the requested behavior.

### Task 2: Add Service-Side Wake Lock Management

**Files:**
- Modify: `mobile-app/app/src/main/AndroidManifest.xml`
- Modify: `mobile-app/app/src/main/java/com/zaquant/mobile/BackendService.java`

- [ ] **Step 1: Declare the two OS permissions**

Insert after the existing notification permission:

```xml
    <uses-permission android:name="android.permission.WAKE_LOCK"/>
    <uses-permission android:name="android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"/>
```

- [ ] **Step 2: Add wake-lock ownership to the service**

Add the import and field:

```java
import android.os.PowerManager;

private PowerManager.WakeLock wakeLock;
```

Add these methods before `createChannel`:

```java
    private void acquireWakeLock() {
        PowerManager powerManager = getSystemService(PowerManager.class);
        if (powerManager == null || (wakeLock != null && wakeLock.isHeld())) {
            return;
        }
        wakeLock = powerManager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK, getPackageName() + ":backend");
        wakeLock.setReferenceCounted(false);
        wakeLock.acquire();
    }

    private void releaseWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        wakeLock = null;
    }

    @Override
    public void onDestroy() {
        releaseWakeLock();
        super.onDestroy();
    }
```

Immediately after `startForeground(NOTIFICATION_ID, notification);`, call `acquireWakeLock();`. Replace the direct Python start calls with:

```java
        try {
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            System.setProperty("za.filesdir", getFilesDir().getAbsolutePath());
            Python.getInstance().getModule("backend_main").callAttr("start");
        } catch (RuntimeException error) {
            releaseWakeLock();
            stopSelf();
            throw error;
        }
```

- [ ] **Step 3: Run the focused regression test**

```powershell
python -m unittest tests.test_android_manifest.AndroidManifestTests.test_lockscreen_keepalive_declares_wakelock_and_lifecycle_guards -v
```

Expected: PASS.

### Task 3: Start the Service Correctly and Request Exemption Once

**Files:**
- Modify: `mobile-app/app/src/main/java/com/zaquant/mobile/MainActivity.java`

- [ ] **Step 1: Add imports**

```java
import android.content.ActivityNotFoundException;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.PowerManager;
import android.provider.Settings;
```

- [ ] **Step 2: Use the foreground-service API**

Replace:

```java
        startService(new Intent(this, BackendService.class));
```

with:

```java
        Intent backendService = new Intent(this, BackendService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(backendService);
        } else {
            startService(backendService);
        }
        requestBatteryOptimizationExemptionOnce();
```

- [ ] **Step 3: Add the one-time request helper before `waitAndLoad`**

```java
    private void requestBatteryOptimizationExemptionOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            return;
        }
        PowerManager powerManager = getSystemService(PowerManager.class);
        if (powerManager == null
                || powerManager.isIgnoringBatteryOptimizations(getPackageName())) {
            return;
        }
        SharedPreferences preferences = getSharedPreferences("backend", MODE_PRIVATE);
        if (preferences.getBoolean("battery_optimization_requested", false)) {
            return;
        }
        preferences.edit().putBoolean("battery_optimization_requested", true).apply();
        try {
            Intent request = new Intent(
                    Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:" + getPackageName()));
            startActivity(request);
        } catch (ActivityNotFoundException ignored) {
            // 厂商系统未提供授权页面时仍继续使用前台服务和唤醒锁。
        }
    }
```

- [ ] **Step 4: Run Android static checks and build**

```powershell
python -m unittest tests.test_android_manifest -v
Set-Location mobile-app
.\gradlew.bat --no-daemon :app:assembleDebug
```

Expected: every static check passes, followed by `BUILD SUCCESSFUL` and `app/build/outputs/apk/debug/app-debug.apk`.

- [ ] **Step 5: Commit the Android change**

From repository root:

```powershell
git add mobile-app/app/src/main/AndroidManifest.xml mobile-app/app/src/main/java/com/zaquant/mobile/BackendService.java mobile-app/app/src/main/java/com/zaquant/mobile/MainActivity.java tests/test_android_manifest.py
git commit -m "fix(mobile): keep backend alive while locked"
```

Expected: one isolated Android lifecycle commit.

### Task 4: Build and Verify the Candidate on vivo V2528A

**Files:**
- Create: `dist/ZA量化-手机版-v1.1.1-candidate-<shortsha>.apk`
- Create: `docs/验证记录/2026-09-16/报告-真机候选-<shortsha>.md`

- [ ] **Step 1: Copy the traceable artifact and calculate its digest**

At the repository root:

```powershell
$candidateSha = git rev-parse --short HEAD
Copy-Item mobile-app/app/build/outputs/apk/debug/app-debug.apk "dist/ZA量化-手机版-v1.1.1-candidate-$candidateSha.apk"
Get-FileHash "dist/ZA量化-手机版-v1.1.1-candidate-$candidateSha.apk" -Algorithm SHA256
```

- [ ] **Step 2: Install and prepare the device**

```powershell
$adb = 'E:\android-sdk\platform-tools\adb.exe'
& $adb -s '10CG6319PZ002E5' install -r "dist/ZA量化-手机版-v1.1.1-candidate-$candidateSha.apk"
& $adb -s '10CG6319PZ002E5' forward tcp:18000 tcp:8000
```

Expected: install reports `Success`.

- [ ] **Step 3: Check the cold-start contract behavior**

Clear app data, start `com.zaquant.mobile`, then immediately submit `SHFE.au2612` through `/api/v1/subscriptions`. Record the initial response and `/api/v1/status`.

Acceptance: the first attempt is not rejected with `合约不存在`; later status contains the subscription and live quote activity.

- [ ] **Step 4: Check lock-screen behavior**

Capture the initial status and `quote_recv_total`, execute `adb shell input keyevent 26`, wait 30 seconds, then query the forwarded status endpoint with an 8-second client timeout. Capture:

```powershell
& $adb -s '10CG6319PZ002E5' shell dumpsys activity services com.zaquant.mobile
& $adb -s '10CG6319PZ002E5' shell dumpsys activity processes com.zaquant.mobile
```

Acceptance: locked HTTP returns 200, `quote_recv_total` increases, the backend service is foreground, and the app process is not frozen. Record whether the system battery-exemption grant succeeded. If the phone still freezes after that grant, document the evidence and report a vendor limitation instead of a pass.

- [ ] **Step 5: Check recovery and publish evidence**

Unlock, confirm subscriptions remain, perform one short network interruption/recovery check, and write the report with candidate path, commit, SHA-256, battery-exemption result, timing, counters, dumpsys observations, and a result for both issues. Commit only an accurate report:

```powershell
git add "docs/验证记录/2026-09-16/报告-真机候选-$candidateSha.md"
git commit -m "docs(qa): verify mobile keepalive candidate"
```

If repository policy includes APKs, add the candidate artifact to this commit too.

### Task 5: Release Regression Sweep

**Files:**
- Verify: `tests/`, `electron/`, `tools/check_core_drift.py`

- [ ] **Step 1: Run Python and mirror validation**

```powershell
python -m unittest discover -s tests -v
python tools/check_core_drift.py --strict
```

Expected: all tests pass and no unexpected mirror drift.

- [ ] **Step 2: Run Electron tests**

```powershell
Set-Location electron
npm test
```

Expected: the existing Electron suite passes.

- [ ] **Step 3: Report exact verification results**

The final handoff distinguishes automated checks from live-device evidence and names any remaining device-specific limitation.
