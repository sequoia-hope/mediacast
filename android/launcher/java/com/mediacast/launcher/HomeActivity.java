package com.mediacast.launcher;

import android.app.Activity;
import android.app.ActivityManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.net.Socket;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

public class HomeActivity extends Activity {

    private TextView clockTv;
    private TextView ipTv;
    private View resumeCard;
    private Handler handler = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN
                | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        setContentView(buildLayout());

        handler.post(clockUpdater);
    }

    private View buildLayout() {
        // Root: dark background, centered content
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFF0A0A1A);
        root.setGravity(Gravity.CENTER);
        root.setPadding(dp(40), dp(24), dp(40), dp(24));

        // Title
        TextView title = new TextView(this);
        title.setText("Mediacast");
        title.setTextColor(0xFFE94560);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 42);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setGravity(Gravity.CENTER);
        root.addView(title, matchWrap());

        // Clock
        clockTv = new TextView(this);
        clockTv.setTextColor(0xFFAAAAAA);
        clockTv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        clockTv.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams clockLp = matchWrap();
        clockLp.topMargin = dp(4);
        clockLp.bottomMargin = dp(32);
        root.addView(clockTv, clockLp);

        // Button grid
        LinearLayout grid = new LinearLayout(this);
        grid.setOrientation(LinearLayout.HORIZONTAL);
        grid.setGravity(Gravity.CENTER);

        resumeCard = makeCard("Resume\nPlayback", 0xFF1A6030, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                resumePlayer();
            }
        });
        grid.addView(resumeCard);

        grid.addView(makeCard("Original\nLauncher", 0xFF0F3460, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                launchOriginalLauncher();
            }
        }));

        grid.addView(makeCard("Settings", 0xFF0F3460, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                launchSettings();
            }
        }));

        grid.addView(makeCard("File\nManager", 0xFF0F3460, new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                launchFileManager();
            }
        }));

        root.addView(grid, matchWrap());

        // IP address + info at bottom
        ipTv = new TextView(this);
        ipTv.setTextColor(0xFF53A8B6);
        ipTv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        ipTv.setTypeface(Typeface.MONOSPACE);
        ipTv.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams ipLp = matchWrap();
        ipLp.topMargin = dp(36);
        root.addView(ipTv, ipLp);

        TextView hint = new TextView(this);
        hint.setText("Cast from castweb on your phone or laptop");
        hint.setTextColor(0xFF666666);
        hint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14);
        hint.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams hintLp = matchWrap();
        hintLp.topMargin = dp(8);
        root.addView(hint, hintLp);

        return root;
    }

    private View makeCard(String label, int bgColor, View.OnClickListener listener) {
        TextView card = new TextView(this);
        card.setText(label);
        card.setTextColor(Color.WHITE);
        card.setTextSize(TypedValue.COMPLEX_UNIT_SP, 18);
        card.setTypeface(Typeface.DEFAULT_BOLD);
        card.setGravity(Gravity.CENTER);
        card.setBackgroundColor(bgColor);
        card.setPadding(dp(24), dp(28), dp(24), dp(28));
        card.setFocusable(true);
        card.setClickable(true);
        card.setOnClickListener(listener);
        card.setOnFocusChangeListener(new View.OnFocusChangeListener() {
            @Override
            public void onFocusChange(View v, boolean hasFocus) {
                v.setBackgroundColor(hasFocus ? 0xFFE94560 : bgColor);
            }
        });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(dp(160), dp(100));
        lp.setMargins(dp(12), 0, dp(12), 0);
        card.setLayoutParams(lp);
        return card;
    }

    private void resumePlayer() {
        // Bring the existing EQ Player task to front without restarting it.
        // FLAG_ACTIVITY_NEW_TASK is required when launching from another app.
        // FLAG_ACTIVITY_REORDER_TO_FRONT moves the existing task to the foreground.
        // No intent data = handleIntent() in the player will be a no-op.
        Intent intent = new Intent();
        intent.setComponent(new ComponentName(
                "com.mediacast.eqplayer",
                "com.mediacast.eqplayer.MainActivity"));
        intent.setAction("com.mediacast.eqplayer.RESUME");
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        try {
            startActivity(intent);
        } catch (Exception e) {}
    }

    private boolean isPlayerRunning() {
        // EQ Player runs an HTTP server on port 8081 — if we can connect, it's alive
        try {
            Socket sock = new Socket();
            sock.connect(new java.net.InetSocketAddress("127.0.0.1", 8081), 200);
            sock.close();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private void updateResumeButton() {
        if (resumeCard == null) return;
        new Thread(new Runnable() {
            @Override
            public void run() {
                final boolean active = isPlayerRunning();
                handler.post(new Runnable() {
                    @Override
                    public void run() {
                        TextView tv = (TextView) resumeCard;
                        if (active) {
                            tv.setBackgroundColor(0xFF1A6030);
                            tv.setTextColor(Color.WHITE);
                            resumeCard.setFocusable(true);
                            resumeCard.requestFocus();
                        } else {
                            tv.setBackgroundColor(0xFF222222);
                            tv.setTextColor(0xFF555555);
                            resumeCard.setFocusable(false);
                        }
                    }
                });
            }
        }).start();
    }

    private void launchOriginalLauncher() {
        try {
            Intent intent = new Intent();
            intent.setComponent(new ComponentName(
                    "com.siviton.blcastlauncher",
                    "com.siviton.blcastlauncher.MainActivity"));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (Exception e) {
            // Fallback: try launching it as a regular app
            Intent intent = getPackageManager()
                    .getLaunchIntentForPackage("com.siviton.blcastlauncher");
            if (intent != null) {
                startActivity(intent);
            }
        }
    }

    private void launchSettings() {
        try {
            // Try the projector's custom settings first
            Intent intent = new Intent();
            intent.setComponent(new ComponentName(
                    "com.siviton.settings",
                    "com.siviton.settings.MainActivity"));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (Exception e) {
            // Fall back to Android TV settings
            try {
                Intent intent = new Intent();
                intent.setComponent(new ComponentName(
                        "com.android.tv.settings",
                        "com.android.tv.settings.MainSettings"));
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(intent);
            } catch (Exception e2) {
                startActivity(new Intent(android.provider.Settings.ACTION_SETTINGS));
            }
        }
    }

    private void launchFileManager() {
        try {
            Intent intent = new Intent();
            intent.setComponent(new ComponentName(
                    "com.jrm.localmm",
                    "com.jrm.localmm.ui.main.MainActivity"));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (Exception e) {
            // silently fail if not available
        }
    }

    private void updateInfo() {
        SimpleDateFormat fmt = new SimpleDateFormat("EEE, MMM d  h:mm a", Locale.US);
        clockTv.setText(fmt.format(new Date()));

        String ip = getWifiIp();
        if (ip != null && !ip.equals("0.0.0.0")) {
            ipTv.setText(ip);
        } else {
            ipTv.setText("No network");
        }
    }

    private String getWifiIp() {
        try {
            WifiManager wm = (WifiManager) getApplicationContext()
                    .getSystemService(WIFI_SERVICE);
            WifiInfo wi = wm.getConnectionInfo();
            int ip = wi.getIpAddress();
            return String.format(Locale.US, "%d.%d.%d.%d",
                    ip & 0xff, (ip >> 8) & 0xff, (ip >> 16) & 0xff, (ip >> 24) & 0xff);
        } catch (Exception e) {
            return null;
        }
    }

    private Runnable clockUpdater = new Runnable() {
        @Override
        public void run() {
            updateInfo();
            handler.postDelayed(this, 30000);
        }
    };

    @Override
    protected void onResume() {
        super.onResume();
        updateInfo();
        updateResumeButton();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        handler.removeCallbacks(clockUpdater);
    }

    // --- Helpers ---

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density);
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
    }
}
