package com.zaquant.mobile;

import android.app.Activity;
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

        if (!Python.isStarted()) {
            // Chaquopy 16.x 新 API：Python.start(Platform)，不再用 Python.init(context, platform)
            Python.start(new AndroidPlatform(this));
        }
        Python.getInstance().getModule("backend_main").callAttr("start");
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
