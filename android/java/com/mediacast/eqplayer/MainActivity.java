package com.mediacast.eqplayer;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.media.MediaPlayer;
import android.media.audiofx.Equalizer;
import android.media.audiofx.LoudnessEnhancer;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.TextView;

public class MainActivity extends Activity implements SurfaceHolder.Callback {

    private static final String TAG = "EQPlayer";
    private static final int EQ_PORT = 8081;
    private static final int CONTROLS_HIDE_DELAY = 4000;
    private static final int POSITION_UPDATE_INTERVAL = 1000;
    private static final String PREFS_NAME = "eqplayer";
    private static final String PREF_URL = "last_url";
    private static final String PREF_POSITION = "last_position";

    private FrameLayout rootLayout;
    private SurfaceView surfaceView;
    private View controlsOverlay;
    private TextView playPauseBtn;
    private SeekBar seekBar;
    private TextView timeCurrentTv;
    private TextView timeDurationTv;

    private MediaPlayer player;
    private Equalizer equalizer;
    private LoudnessEnhancer loudnessEnhancer;
    private EqServer eqServer;
    private String pendingUrl;
    private boolean surfaceReady = false;

    private Handler handler = new Handler(Looper.getMainLooper());
    private boolean controlsVisible = false;
    private boolean seekBarTracking = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN
                | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        buildLayout();
        setContentView(rootLayout);
        surfaceView.getHolder().addCallback(this);

        eqServer = new EqServer(this, EQ_PORT);
        eqServer.start();

        handleIntent(getIntent());
    }

    private void buildLayout() {
        rootLayout = new FrameLayout(this);
        rootLayout.setBackgroundColor(Color.BLACK);

        // SurfaceView for video — centered, will be resized on prepare
        surfaceView = new SurfaceView(this);
        FrameLayout.LayoutParams svLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
                Gravity.CENTER);
        rootLayout.addView(surfaceView, svLp);

        // Controls overlay — transparent, fills screen, catches taps
        FrameLayout overlay = new FrameLayout(this);
        overlay.setOnTouchListener(new View.OnTouchListener() {
            @Override
            public boolean onTouch(View v, MotionEvent event) {
                if (event.getAction() == MotionEvent.ACTION_UP) {
                    toggleControls();
                }
                return true;
            }
        });

        // Bottom bar with controls
        LinearLayout bottomBar = new LinearLayout(this);
        bottomBar.setOrientation(LinearLayout.VERTICAL);
        bottomBar.setBackgroundColor(0xCC000000);
        bottomBar.setPadding(dp(16), dp(8), dp(16), dp(12));

        // Seek bar row
        LinearLayout seekRow = new LinearLayout(this);
        seekRow.setOrientation(LinearLayout.HORIZONTAL);
        seekRow.setGravity(Gravity.CENTER_VERTICAL);

        timeCurrentTv = new TextView(this);
        timeCurrentTv.setTextColor(Color.WHITE);
        timeCurrentTv.setTextSize(13);
        timeCurrentTv.setTypeface(Typeface.MONOSPACE);
        timeCurrentTv.setText("0:00:00");
        seekRow.addView(timeCurrentTv);

        seekBar = new SeekBar(this);
        LinearLayout.LayoutParams seekLp = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        seekLp.setMargins(dp(8), 0, dp(8), 0);
        seekBar.setLayoutParams(seekLp);
        seekBar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar sb, int progress, boolean fromUser) {
                if (fromUser && player != null) {
                    timeCurrentTv.setText(formatTime(progress));
                }
            }
            @Override
            public void onStartTrackingTouch(SeekBar sb) {
                seekBarTracking = true;
                // Keep controls visible while seeking
                handler.removeCallbacks(hideControlsRunnable);
            }
            @Override
            public void onStopTrackingTouch(SeekBar sb) {
                seekBarTracking = false;
                if (player != null) {
                    player.seekTo(sb.getProgress());
                }
                scheduleHideControls();
            }
        });
        seekRow.addView(seekBar);

        timeDurationTv = new TextView(this);
        timeDurationTv.setTextColor(0xFFAAAAAA);
        timeDurationTv.setTextSize(13);
        timeDurationTv.setTypeface(Typeface.MONOSPACE);
        timeDurationTv.setText("0:00:00");
        seekRow.addView(timeDurationTv);

        bottomBar.addView(seekRow);

        // Button row
        LinearLayout btnRow = new LinearLayout(this);
        btnRow.setOrientation(LinearLayout.HORIZONTAL);
        btnRow.setGravity(Gravity.CENTER);
        btnRow.setPadding(0, dp(6), 0, 0);

        playPauseBtn = makeButton("\u23EF");
        playPauseBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                togglePlayPause();
                scheduleHideControls();
            }
        });
        btnRow.addView(playPauseBtn);

        bottomBar.addView(btnRow);

        FrameLayout.LayoutParams barLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM);
        overlay.addView(bottomBar, barLp);

        controlsOverlay = overlay;
        controlsOverlay.setVisibility(View.GONE);

        rootLayout.addView(overlay, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
    }

    private TextView makeButton(String text) {
        TextView btn = new TextView(this);
        btn.setText(text);
        btn.setTextColor(Color.WHITE);
        btn.setTextSize(22);
        btn.setGravity(Gravity.CENTER);
        btn.setPadding(dp(20), dp(4), dp(20), dp(4));
        return btn;
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density);
    }

    // --- Controls visibility ---

    private void toggleControls() {
        if (controlsVisible) {
            hideControls();
        } else {
            showControls();
        }
    }

    private void showControls() {
        controlsVisible = true;
        controlsOverlay.setVisibility(View.VISIBLE);
        updateControlsState();
        scheduleHideControls();
    }

    private void hideControls() {
        controlsVisible = false;
        controlsOverlay.setVisibility(View.GONE);
        handler.removeCallbacks(hideControlsRunnable);
    }

    private void scheduleHideControls() {
        handler.removeCallbacks(hideControlsRunnable);
        handler.postDelayed(hideControlsRunnable, CONTROLS_HIDE_DELAY);
    }

    private Runnable hideControlsRunnable = new Runnable() {
        @Override
        public void run() {
            hideControls();
        }
    };

    private void updateControlsState() {
        if (player == null) return;
        boolean playing = player.isPlaying();
        playPauseBtn.setText(playing ? "\u23F8" : "\u25B6");

        int dur = player.getDuration();
        seekBar.setMax(dur);
        timeDurationTv.setText(formatTime(dur));

        if (!seekBarTracking) {
            int pos = player.getCurrentPosition();
            seekBar.setProgress(pos);
            timeCurrentTv.setText(formatTime(pos));
        }
    }

    // --- Position saving/restoring ---

    private void savePosition() {
        if (player == null || pendingUrl == null) return;
        try {
            int pos = player.getCurrentPosition();
            SharedPreferences.Editor ed = getSharedPreferences(PREFS_NAME, 0).edit();
            ed.putString(PREF_URL, pendingUrl);
            ed.putInt(PREF_POSITION, pos);
            ed.apply();
        } catch (Exception e) {}
    }

    private void clearSavedPosition() {
        getSharedPreferences(PREFS_NAME, 0).edit().clear().apply();
    }

    // --- Position updater ---

    private Runnable positionUpdater = new Runnable() {
        @Override
        public void run() {
            if (player != null) {
                savePosition();
                if (controlsVisible) {
                    updateControlsState();
                }
            }
            handler.postDelayed(this, POSITION_UPDATE_INTERVAL);
        }
    };

    // --- Aspect ratio ---

    private void fitSurfaceToVideo() {
        if (player == null) return;
        int vw = player.getVideoWidth();
        int vh = player.getVideoHeight();
        if (vw == 0 || vh == 0) return;

        int sw = rootLayout.getWidth();
        int sh = rootLayout.getHeight();
        if (sw == 0 || sh == 0) return;

        float videoAspect = (float) vw / vh;
        float screenAspect = (float) sw / sh;
        int fitW, fitH;
        if (videoAspect > screenAspect) {
            fitW = sw;
            fitH = (int) (sw / videoAspect);
        } else {
            fitH = sh;
            fitW = (int) (sh * videoAspect);
        }

        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(fitW, fitH, Gravity.CENTER);
        surfaceView.setLayoutParams(lp);
        Log.i(TAG, "Aspect fit: " + vw + "x" + vh + " -> " + fitW + "x" + fitH
                + " (screen " + sw + "x" + sh + ")");
    }

    // --- Playback ---

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntent(intent);
    }

    private int resumePosition = 0;

    private void handleIntent(Intent intent) {
        Uri uri = intent.getData();
        if (uri == null) {
            // No URL — check if we have a saved session to resume
            if (player != null) {
                Log.i(TAG, "Resume (player still alive)");
                return;
            }
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, 0);
            String savedUrl = prefs.getString(PREF_URL, null);
            int savedPos = prefs.getInt(PREF_POSITION, 0);
            if (savedUrl != null) {
                Log.i(TAG, "Resuming saved session: " + savedUrl + " at " + savedPos + "ms");
                pendingUrl = savedUrl;
                resumePosition = savedPos;
                if (surfaceReady) {
                    startPlayback(pendingUrl);
                }
            } else {
                Log.i(TAG, "Resume: nothing saved");
            }
            return;
        }
        String url = uri.toString();
        // Only restart playback if the URL actually changed
        if (url.equals(pendingUrl) && player != null) {
            Log.i(TAG, "Same URL, resuming");
            return;
        }
        pendingUrl = url;
        resumePosition = 0;
        Log.i(TAG, "New URL: " + pendingUrl);
        if (surfaceReady) {
            startPlayback(pendingUrl);
        }
    }

    private void startPlayback(String url) {
        releasePlayer();
        hideControls();

        try {
            player = new MediaPlayer();
            player.setDisplay(surfaceView.getHolder());
            player.setDataSource(url);
            player.setOnPreparedListener(new MediaPlayer.OnPreparedListener() {
                @Override
                public void onPrepared(MediaPlayer mp) {
                    attachEqualizer();
                    fitSurfaceToVideo();
                    if (resumePosition > 0) {
                        mp.seekTo(resumePosition);
                        Log.i(TAG, "Seeking to saved position: " + resumePosition + "ms");
                        resumePosition = 0;
                    }
                    mp.start();
                    handler.post(positionUpdater);
                    Log.i(TAG, "Playback started");
                }
            });
            player.setOnVideoSizeChangedListener(new MediaPlayer.OnVideoSizeChangedListener() {
                @Override
                public void onVideoSizeChanged(MediaPlayer mp, int width, int height) {
                    fitSurfaceToVideo();
                }
            });
            player.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                @Override
                public boolean onError(MediaPlayer mp, int what, int extra) {
                    Log.e(TAG, "MediaPlayer error: " + what + "/" + extra);
                    return true;
                }
            });
            player.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                @Override
                public void onCompletion(MediaPlayer mp) {
                    Log.i(TAG, "Playback completed");
                    clearSavedPosition();
                    updateControlsState();
                }
            });
            player.prepareAsync();
        } catch (Exception e) {
            Log.e(TAG, "Error starting playback", e);
        }
    }

    private void togglePlayPause() {
        if (player == null) return;
        if (player.isPlaying()) {
            player.pause();
        } else {
            player.start();
        }
        updateControlsState();
    }

    private void attachEqualizer() {
        if (player == null) return;
        int sessionId = player.getAudioSessionId();
        Log.i(TAG, "Audio session ID: " + sessionId);

        try {
            equalizer = new Equalizer(0, sessionId);
            equalizer.setEnabled(true);

            short numBands = equalizer.getNumberOfBands();
            Log.i(TAG, "EQ bands: " + numBands);
            short[] bandRange = equalizer.getBandLevelRange();
            Log.i(TAG, "EQ range: " + bandRange[0] + " to " + bandRange[1] + " mB");
            for (short i = 0; i < numBands; i++) {
                int freq = equalizer.getCenterFreq(i);
                Log.i(TAG, "Band " + i + ": " + freq + " mHz (center)");
            }
        } catch (Exception e) {
            Log.e(TAG, "Error creating Equalizer", e);
        }

        try {
            loudnessEnhancer = new LoudnessEnhancer(sessionId);
            loudnessEnhancer.setEnabled(false);
        } catch (Exception e) {
            Log.e(TAG, "Error creating LoudnessEnhancer", e);
        }
    }

    // Called by EqServer on the UI thread
    public void updateEq(short[] bandLevels) {
        if (equalizer == null) return;
        short numBands = equalizer.getNumberOfBands();
        for (short i = 0; i < numBands && i < bandLevels.length; i++) {
            short[] range = equalizer.getBandLevelRange();
            short level = bandLevels[i];
            if (level < range[0]) level = range[0];
            if (level > range[1]) level = range[1];
            equalizer.setBandLevel(i, level);
        }
        Log.i(TAG, "EQ updated");
    }

    public void setLoudnessEnhancerEnabled(boolean enabled, int gainMb) {
        if (loudnessEnhancer == null) return;
        if (enabled) {
            loudnessEnhancer.setTargetGain(gainMb);
            loudnessEnhancer.setEnabled(true);
        } else {
            loudnessEnhancer.setEnabled(false);
        }
        Log.i(TAG, "LoudnessEnhancer: " + (enabled ? "ON (" + gainMb + " mB)" : "OFF"));
    }

    public String getInfoJson() {
        StringBuilder sb = new StringBuilder();
        sb.append("{");
        sb.append("\"playing\":");
        sb.append(player != null && player.isPlaying());
        if (player != null) {
            try {
                sb.append(",\"position\":").append(player.getCurrentPosition());
                sb.append(",\"duration\":").append(player.getDuration());
            } catch (Exception e) {}
        }
        if (equalizer != null) {
            short numBands = equalizer.getNumberOfBands();
            sb.append(",\"bands\":").append(numBands);
            short[] range = equalizer.getBandLevelRange();
            sb.append(",\"minLevel\":").append(range[0]);
            sb.append(",\"maxLevel\":").append(range[1]);
            sb.append(",\"frequencies\":[");
            for (short i = 0; i < numBands; i++) {
                if (i > 0) sb.append(",");
                sb.append(equalizer.getCenterFreq(i));
            }
            sb.append("],\"levels\":[");
            for (short i = 0; i < numBands; i++) {
                if (i > 0) sb.append(",");
                sb.append(equalizer.getBandLevel(i));
            }
            sb.append("]");
        }
        if (loudnessEnhancer != null) {
            sb.append(",\"loudnessEnhancer\":").append(loudnessEnhancer.getEnabled());
        }
        sb.append("}");
        return sb.toString();
    }

    // --- Utility ---

    private static String formatTime(int ms) {
        int totalSec = ms / 1000;
        int h = totalSec / 3600;
        int m = (totalSec % 3600) / 60;
        int s = totalSec % 60;
        return String.format("%d:%02d:%02d", h, m, s);
    }

    // --- Surface callbacks ---

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        surfaceReady = true;
        if (player != null) {
            // Returning from background — re-attach display, don't restart
            player.setDisplay(holder);
            Log.i(TAG, "Surface re-attached to existing player");
        } else if (pendingUrl != null) {
            startPlayback(pendingUrl);
        }
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        fitSurfaceToVideo();
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        surfaceReady = false;
    }

    // --- Key handling ---

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (player == null) return super.onKeyDown(keyCode, event);

        switch (keyCode) {
            case KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE:
            case KeyEvent.KEYCODE_HEADSETHOOK:
                togglePlayPause();
                showControls();
                return true;

            case KeyEvent.KEYCODE_DPAD_RIGHT:
                seekBy(10000);
                showControls();
                return true;

            case KeyEvent.KEYCODE_DPAD_LEFT:
                seekBy(-10000);
                showControls();
                return true;

            case KeyEvent.KEYCODE_MEDIA_FAST_FORWARD:
                seekBy(30000);
                showControls();
                return true;

            case KeyEvent.KEYCODE_MEDIA_REWIND:
                seekBy(-30000);
                showControls();
                return true;

            case KeyEvent.KEYCODE_DPAD_CENTER:
            case KeyEvent.KEYCODE_ENTER:
                toggleControls();
                return true;

            case KeyEvent.KEYCODE_BACK:
                clearSavedPosition();
                releasePlayer();
                finish();
                return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private void seekBy(int ms) {
        if (player == null) return;
        int pos = player.getCurrentPosition() + ms;
        int dur = player.getDuration();
        if (pos < 0) pos = 0;
        if (pos > dur) pos = dur;
        player.seekTo(pos);
        updateControlsState();
    }

    // --- Cleanup ---

    private void releasePlayer() {
        handler.removeCallbacks(positionUpdater);
        handler.removeCallbacks(hideControlsRunnable);
        if (equalizer != null) {
            try { equalizer.release(); } catch (Exception e) {}
            equalizer = null;
        }
        if (loudnessEnhancer != null) {
            try { loudnessEnhancer.release(); } catch (Exception e) {}
            loudnessEnhancer = null;
        }
        if (player != null) {
            try {
                player.stop();
                player.release();
            } catch (Exception e) {}
            player = null;
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        releasePlayer();
        if (eqServer != null) {
            eqServer.stopServer();
        }
    }
}
