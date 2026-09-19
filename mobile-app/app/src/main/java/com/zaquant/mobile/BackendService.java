package com.zaquant.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * 缺陷 B 修复：前台服务保活。
 *
 * 真机实测（vivo V2528A · Android 16）：锁屏后系统冻结进程，后端 HTTP 12s 无响应。
 * 将后端线程挂在前台服务下（低优先级常驻通知），系统对前台服务的后台冻结
 * 阈值远宽松于普通进程，锁屏期间后端仍能响应行情推送与 HTTP 轮询。
 *
 * 该服务只负责"在通知栏挂一个常驻通知 + 启动后端"，后端逻辑仍在 backend_main.py。
 */
public class BackendService extends Service {
    private static final String CHANNEL_ID = "zaquant_backend";
    private static final int NOTIFICATION_ID = 1;
    private PowerManager.WakeLock wakeLock;

    /** 不提供绑定接口：纯前台服务，Activity 只负责 startService。 */
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    /**
     * 服务创建（进程内仅一次）：挂常驻通知 → 持 PARTIAL_WAKE_LOCK（锁屏期间
     * CPU 不休眠，行情推送与 HTTP 不中断）→ 启动 Chaquopy Python 并调
     * backend_main.start() 拉起后端线程。Python 启动失败则释放资源自停。
     */
    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("ZA量化")
                .setContentText("行情连接保持中")
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setPriority(Notification.PRIORITY_LOW)
                .setOngoing(true)
                .build();
        startForeground(NOTIFICATION_ID, notification);
        acquireWakeLock();

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
    }

    /** START_STICKY：进程被系统回收后自动重启服务（重新走 onCreate 拉起后端）。 */
    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    /** 持锁 PARTIAL_WAKE_LOCK（不计数模式，持有一份即可，锁屏后 CPU 维持运行）。 */
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

    /** 创建低优先级通知渠道（Android 8+ 必须；低优先级=不发声不震动，仅常驻）。 */
    private void createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "ZA量化后台", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("保持行情连接，锁屏时不被系统冻结");
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
    }
}
