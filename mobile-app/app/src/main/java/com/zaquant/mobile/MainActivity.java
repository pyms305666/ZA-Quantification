package com.zaquant.mobile;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.Manifest;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.webkit.WebSettings;
import android.webkit.WebView;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * App 主界面：全屏 WebView 加载本机 Python 后端的 Web UI。
 *
 * 职责链：申请通知权限（Android 13+，前台服务通知可见性）→ 启动前台服务
 * BackendService（缺陷 B 锁屏保活，Python 后端在其 onCreate 内启动）→
 * 引导一次"电池优化白名单"授权 → waitAndLoad 轮询后端就绪后加载页面。
 * Activity 自身不启动 Python——后端生命周期完全归前台服务管。
 */
public class MainActivity extends Activity {
    private WebView web;

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        // USB 调试辅助：允许 chrome://inspect 检查 WebView（发布审计时可移除）
        android.webkit.WebView.setWebContentsDebuggingEnabled(true);
        web.setLayerType(android.view.View.LAYER_TYPE_SOFTWARE, null);   // 模拟器软渲染，避免硬件加速合成黑屏
        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        // 后端在本机 127.0.0.1:8000，缓存只会造成"升级后旧前端被钉死"（v1.1.3 真机教训：
        // WebView 启发式缓存 + OEM 数据恢复让旧 index/app.js 长期不回源），本地加载代价可忽略
        st.setCacheMode(WebSettings.LOAD_NO_CACHE);
        st.setDomStorageEnabled(true);
        setContentView(web);
        web.loadDataWithBaseURL(null,
            "<p style='font-family:sans-serif;text-align:center;margin-top:45%;color:#8b96a3'>启动中…</p>",
            "text/html", "utf-8", null);

        // 缺陷 B：后端以前台服务形式启动（锁屏保活），Activity 不再直接调 backend_main。
        // Android 13+ 需运行时申请通知权限，否则前台服务通知不可见、系统仍可能冻结。
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
            }
        }
        Intent backendService = new Intent(this, BackendService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(backendService);
        } else {
            startService(backendService);
        }
        requestBatteryOptimizationExemptionOnce();
        waitAndLoad();
    }

    /**
     * 引导用户把本 App 加入"电池优化白名单"（仅弹一次，SharedPreferences 记录）。
     *
     * vivo 等厂商对后台进程冻结激进，前台服务 + WakeLock 之外再拿电池白名单
     * 才能保证锁屏后行情持续接收（缺陷 B 的最后一层保障）。
     * 厂商未提供授权页时静默跳过（ActivityNotFoundException）。
     */
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

    /**
     * 后台线程轮询后端 /api/v1/status（每秒一次、最多 90 秒），
     * 返回 200 才让 WebView 加载真实页面；超时展示"启动失败"提示。
     * 就绪前先加载"启动中…"占位页，避免白屏。
     */
    private void waitAndLoad() {
        new Thread(() -> {
            for (int i = 0; i < 90; i++) {
                try {
                    Thread.sleep(1000);
                    HttpURLConnection c = (HttpURLConnection) new URL(
                        "http://127.0.0.1:8000/api/v1/status").openConnection();
                    c.setConnectTimeout(1500);
                    c.setReadTimeout(1500);
                    if (c.getResponseCode() == 200) {
                        runOnUiThread(() -> web.loadUrl("http://127.0.0.1:8000/"));
                        return;
                    }
                } catch (Exception ignored) {}
            }
            runOnUiThread(() -> web.loadDataWithBaseURL(null,
                "<p style='font-family:sans-serif;text-align:center;margin-top:40%;color:#e0a93c'>后端启动失败，请关闭应用重试</p>",
                "text/html", "utf-8", null));
        }).start();
    }
}
