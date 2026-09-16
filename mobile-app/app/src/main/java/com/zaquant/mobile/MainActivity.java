package com.zaquant.mobile;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.Manifest;
import android.os.Build;
import android.os.Bundle;
import android.webkit.WebSettings;
import android.webkit.WebView;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

public class MainActivity extends Activity {
    private WebView web;

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        web.setLayerType(android.view.View.LAYER_TYPE_SOFTWARE, null);   // 模拟器软渲染，避免硬件加速合成黑屏
        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
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
        startService(new Intent(this, BackendService.class));
        waitAndLoad();
    }

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
