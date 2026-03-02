package com.mediacast.eqplayer;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.media.MediaPlayer;
import android.media.TimedText;
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

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

public class MainActivity extends Activity implements SurfaceHolder.Callback {

    private static final String TAG = "EQPlayer";
    private static final int EQ_PORT = 8081;
    private static final int CONTROLS_HIDE_DELAY = 4000;
    private static final int POSITION_UPDATE_INTERVAL = 1000;
    private static final int TRACK_POPUP_DURATION = 2000;
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
    private TextView trackInfoTv;
    private TextView subtitleTextView;
    private TextView trackPopupTv;

    private MediaPlayer player;
    private Equalizer equalizer;
    private LoudnessEnhancer loudnessEnhancer;
    private EqServer eqServer;
    private String pendingUrl;
    private boolean surfaceReady = false;

    private Handler handler = new Handler(Looper.getMainLooper());
    private boolean controlsVisible = false;
    private boolean seekBarTracking = false;

    // Track state
    private String[] subtitleUrls = new String[0];
    private String[] subtitleLabels = new String[0];
    private int[] audioTrackIndices = new int[0];
    private String[] audioTrackLabels = new String[0];
    private int currentAudioTrackIdx = 0;   // index into audioTrackIndices
    private int currentSubtitleIdx = -1;    // -1 = off, 0..N-1 = subtitle index
    private File subtitleTmpFile;

    // Duration and seek offset for streaming transcode
    private long realDurationMs = 0;    // Total duration from ffprobe (0 = use player's)
    private long seekOffsetMs = 0;      // Current transcode starts at this offset
    private String serverBaseUrl = null; // castweb server URL for seek-ahead requests

    // Debounced server seek — accumulates rapid key presses into one seek
    private Runnable pendingSeekRunnable = null;
    private long pendingSeekTarget = -1;

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

        // Subtitle text — always visible (even when controls hidden), above bottom bar area
        subtitleTextView = new TextView(this);
        subtitleTextView.setTextColor(Color.WHITE);
        subtitleTextView.setTextSize(20);
        subtitleTextView.setTypeface(Typeface.DEFAULT_BOLD);
        subtitleTextView.setShadowLayer(4f, 2f, 2f, Color.BLACK);
        subtitleTextView.setGravity(Gravity.CENTER_HORIZONTAL);
        subtitleTextView.setPadding(dp(24), 0, dp(24), dp(48));
        subtitleTextView.setVisibility(View.GONE);
        FrameLayout.LayoutParams subLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL);
        rootLayout.addView(subtitleTextView, subLp);

        // Track popup — centered, semi-transparent background, auto-hides
        trackPopupTv = new TextView(this);
        trackPopupTv.setTextColor(Color.WHITE);
        trackPopupTv.setTextSize(18);
        trackPopupTv.setTypeface(Typeface.DEFAULT_BOLD);
        trackPopupTv.setBackgroundColor(0xAA000000);
        trackPopupTv.setPadding(dp(24), dp(12), dp(24), dp(12));
        trackPopupTv.setGravity(Gravity.CENTER);
        trackPopupTv.setVisibility(View.GONE);
        FrameLayout.LayoutParams popupLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER);
        rootLayout.addView(trackPopupTv, popupLp);

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

        // Track info row — shows current audio/subtitle selection
        trackInfoTv = new TextView(this);
        trackInfoTv.setTextColor(0xFF53A8B6);
        trackInfoTv.setTextSize(12);
        trackInfoTv.setPadding(0, 0, 0, dp(4));
        trackInfoTv.setVisibility(View.GONE);
        bottomBar.addView(trackInfoTv);

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
                    long targetMs = sb.getProgress();
                    long localTarget = targetMs - seekOffsetMs;
                    int playerDur = player.getDuration();
                    if (localTarget >= 0 && (playerDur <= 0 || localTarget <= playerDur)) {
                        player.seekTo((int) localTarget);
                    } else {
                        requestServerSeek(targetMs);
                    }
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

        int playerDur = player.getDuration();
        long totalDur = realDurationMs > 0 ? realDurationMs : playerDur;
        seekBar.setMax((int) totalDur);
        timeDurationTv.setText(formatTime((int) totalDur));

        if (!seekBarTracking) {
            long displayPos;
            if (pendingSeekTarget >= 0) {
                displayPos = pendingSeekTarget;
            } else {
                displayPos = seekOffsetMs + player.getCurrentPosition();
            }
            seekBar.setProgress((int) displayPos);
            timeCurrentTv.setText(formatTime((int) displayPos));
        }

        updateTrackInfoDisplay();
    }

    // --- Track info display ---

    private void updateTrackInfoDisplay() {
        String audioLabel = "Audio: ?";
        if (audioTrackLabels.length > 0 && currentAudioTrackIdx < audioTrackLabels.length) {
            audioLabel = "Audio: " + audioTrackLabels[currentAudioTrackIdx];
        }
        String subLabel = "Sub: Off";
        if (currentSubtitleIdx >= 0 && currentSubtitleIdx < subtitleLabels.length) {
            subLabel = "Sub: " + subtitleLabels[currentSubtitleIdx];
        }
        boolean hasTracks = audioTrackLabels.length > 1 || subtitleLabels.length > 0;
        if (hasTracks) {
            trackInfoTv.setText(audioLabel + "  |  " + subLabel);
            trackInfoTv.setVisibility(View.VISIBLE);
        } else {
            trackInfoTv.setVisibility(View.GONE);
        }
    }

    // --- Track popup ---

    private Runnable hidePopupRunnable = new Runnable() {
        @Override
        public void run() {
            trackPopupTv.setVisibility(View.GONE);
        }
    };

    private void showTrackPopup(String text) {
        handler.removeCallbacks(hidePopupRunnable);
        trackPopupTv.setText(text);
        trackPopupTv.setVisibility(View.VISIBLE);
        handler.postDelayed(hidePopupRunnable, TRACK_POPUP_DURATION);
    }

    // --- Track parsing from intent ---

    private void parseTrackExtras(Intent intent) {
        String subsJson = intent.getStringExtra("subtitles");
        if (subsJson != null && !subsJson.isEmpty()) {
            try {
                // Minimal JSON array parsing: [{"url":"...","label":"..."},...]
                java.util.ArrayList<String> urls = new java.util.ArrayList<>();
                java.util.ArrayList<String> labels = new java.util.ArrayList<>();
                // Split by objects
                int pos = 0;
                while (pos < subsJson.length()) {
                    int objStart = subsJson.indexOf('{', pos);
                    if (objStart < 0) break;
                    int objEnd = subsJson.indexOf('}', objStart);
                    if (objEnd < 0) break;
                    String obj = subsJson.substring(objStart, objEnd + 1);
                    String url = extractJsonString(obj, "url");
                    String label = extractJsonString(obj, "label");
                    if (url != null) {
                        urls.add(url);
                        labels.add(label != null ? label : "Sub " + urls.size());
                    }
                    pos = objEnd + 1;
                }
                subtitleUrls = urls.toArray(new String[0]);
                subtitleLabels = labels.toArray(new String[0]);
                Log.i(TAG, "Parsed " + subtitleUrls.length + " subtitle tracks");
            } catch (Exception e) {
                Log.e(TAG, "Error parsing subtitles JSON", e);
                subtitleUrls = new String[0];
                subtitleLabels = new String[0];
            }
        } else {
            subtitleUrls = new String[0];
            subtitleLabels = new String[0];
        }
        currentSubtitleIdx = -1;
    }

    private static String extractJsonString(String json, String key) {
        String search = "\"" + key + "\"";
        int idx = json.indexOf(search);
        if (idx < 0) return null;
        int colon = json.indexOf(':', idx + search.length());
        if (colon < 0) return null;
        int qStart = json.indexOf('"', colon + 1);
        if (qStart < 0) return null;
        int qEnd = json.indexOf('"', qStart + 1);
        if (qEnd < 0) return null;
        return json.substring(qStart + 1, qEnd);
    }

    // --- Audio track enumeration ---

    private void enumerateAudioTracks() {
        if (player == null) return;
        try {
            MediaPlayer.TrackInfo[] tracks = player.getTrackInfo();
            java.util.ArrayList<Integer> indices = new java.util.ArrayList<>();
            java.util.ArrayList<String> labels = new java.util.ArrayList<>();
            for (int i = 0; i < tracks.length; i++) {
                if (tracks[i].getTrackType() == MediaPlayer.TrackInfo.MEDIA_TRACK_TYPE_AUDIO) {
                    indices.add(i);
                    String lang = tracks[i].getLanguage();
                    if (lang == null || lang.equals("und") || lang.isEmpty()) {
                        lang = "Track " + (indices.size());
                    }
                    labels.add(lang);
                }
            }
            audioTrackIndices = new int[indices.size()];
            audioTrackLabels = new String[labels.size()];
            for (int i = 0; i < indices.size(); i++) {
                audioTrackIndices[i] = indices.get(i);
                audioTrackLabels[i] = labels.get(i);
            }
            currentAudioTrackIdx = 0;
            Log.i(TAG, "Found " + audioTrackIndices.length + " audio tracks");
        } catch (Exception e) {
            Log.e(TAG, "Error enumerating audio tracks", e);
            audioTrackIndices = new int[0];
            audioTrackLabels = new String[0];
        }
    }

    // --- Audio track cycling ---

    public void cycleAudioTrack() {
        if (audioTrackIndices.length <= 1) {
            showTrackPopup("Audio: only one track");
            return;
        }
        currentAudioTrackIdx = (currentAudioTrackIdx + 1) % audioTrackIndices.length;
        selectAudioTrackInternal(currentAudioTrackIdx);
    }

    private void selectAudioTrackInternal(int idx) {
        if (player == null || idx < 0 || idx >= audioTrackIndices.length) return;
        try {
            player.selectTrack(audioTrackIndices[idx]);
            Log.i(TAG, "Selected audio track " + idx + ": " + audioTrackLabels[idx]);
        } catch (Exception e) {
            Log.e(TAG, "Error selecting audio track", e);
        }
        showTrackPopup("Audio: " + audioTrackLabels[idx]);
        updateTrackInfoDisplay();
    }

    public void selectAudioTrack(int idx) {
        if (idx < 0 || idx >= audioTrackIndices.length) return;
        currentAudioTrackIdx = idx;
        selectAudioTrackInternal(idx);
    }

    // --- Subtitle cycling ---

    public void cycleSubtitleTrack() {
        if (subtitleUrls.length == 0) {
            showTrackPopup("Sub: none available");
            return;
        }
        // Cycle: -1 (off) → 0 → 1 → ... → N-1 → -1 (off)
        int next = currentSubtitleIdx + 1;
        if (next >= subtitleUrls.length) next = -1;
        selectSubtitleTrack(next);
    }

    public void selectSubtitleTrack(final int index) {
        if (index < 0) {
            // Turn off subtitles
            currentSubtitleIdx = -1;
            subtitleTextView.setVisibility(View.GONE);
            subtitleTextView.setText("");
            showTrackPopup("Sub: Off");
            updateTrackInfoDisplay();
            // Deselect any timed text track
            deselectTimedTextTracks();
            return;
        }
        if (index >= subtitleUrls.length) return;

        currentSubtitleIdx = index;
        showTrackPopup("Sub: " + subtitleLabels[index]);
        updateTrackInfoDisplay();

        // Download subtitle file in background thread, then load
        final String url = subtitleUrls[index];
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    File tmpFile = downloadToCache(url, "sub_" + index + ".srt");
                    if (tmpFile != null) {
                        final File f = tmpFile;
                        handler.post(new Runnable() {
                            @Override
                            public void run() {
                                loadSubtitleFile(f);
                            }
                        });
                    }
                } catch (Exception e) {
                    Log.e(TAG, "Error downloading subtitle", e);
                }
            }
        }).start();
    }

    private File downloadToCache(String urlStr, String filename) {
        try {
            URL url = new URL(urlStr);
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(5000);
            conn.setReadTimeout(10000);
            InputStream is = new BufferedInputStream(conn.getInputStream());
            File outFile = new File(getCacheDir(), filename);
            FileOutputStream fos = new FileOutputStream(outFile);
            byte[] buf = new byte[4096];
            int n;
            while ((n = is.read(buf)) != -1) {
                fos.write(buf, 0, n);
            }
            fos.close();
            is.close();
            conn.disconnect();
            return outFile;
        } catch (Exception e) {
            Log.e(TAG, "Download failed: " + urlStr, e);
            return null;
        }
    }

    private void loadSubtitleFile(File file) {
        if (player == null) return;
        // Clean up previous temp file
        if (subtitleTmpFile != null && subtitleTmpFile != file) {
            subtitleTmpFile.delete();
        }
        subtitleTmpFile = file;

        try {
            // Deselect any current timed text tracks first
            deselectTimedTextTracks();

            player.addTimedTextSource(file.getAbsolutePath(), MediaPlayer.MEDIA_MIMETYPE_TEXT_SUBRIP);

            // Find and select the newly added timed text track
            MediaPlayer.TrackInfo[] tracks = player.getTrackInfo();
            for (int i = tracks.length - 1; i >= 0; i--) {
                if (tracks[i].getTrackType() == MediaPlayer.TrackInfo.MEDIA_TRACK_TYPE_TIMEDTEXT) {
                    player.selectTrack(i);
                    Log.i(TAG, "Selected timed text track " + i);
                    break;
                }
            }
            subtitleTextView.setVisibility(View.VISIBLE);
        } catch (Exception e) {
            Log.e(TAG, "Error loading subtitle file", e);
        }
    }

    private void deselectTimedTextTracks() {
        if (player == null) return;
        try {
            MediaPlayer.TrackInfo[] tracks = player.getTrackInfo();
            for (int i = 0; i < tracks.length; i++) {
                if (tracks[i].getTrackType() == MediaPlayer.TrackInfo.MEDIA_TRACK_TYPE_TIMEDTEXT) {
                    try { player.deselectTrack(i); } catch (Exception e) {}
                }
            }
        } catch (Exception e) {}
    }

    // --- Position saving/restoring ---

    private static final String RESUME_FILE = "/data/local/tmp/eqplayer_resume";

    private void savePosition() {
        if (player == null || pendingUrl == null) return;
        try {
            int pos = player.getCurrentPosition();
            // Save to SharedPreferences (for our own resume)
            SharedPreferences.Editor ed = getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit();
            ed.putString(PREF_URL, pendingUrl);
            ed.putInt(PREF_POSITION, pos);
            ed.apply();
        } catch (Exception e) {
            Log.e(TAG, "savePosition prefs failed: " + e.getMessage());
        }
        try {
            // Write shared file the launcher can read
            java.io.FileWriter fw = new java.io.FileWriter(RESUME_FILE);
            fw.write(pendingUrl + "\n" + player.getCurrentPosition() + "\n");
            fw.close();
        } catch (Exception e) {
            Log.e(TAG, "savePosition file failed: " + e.getMessage());
        }
    }

    private void clearSavedPosition() {
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE).edit().clear().apply();
        try { new java.io.File(RESUME_FILE).delete(); } catch (Exception e) {}
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
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
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
        cancelPendingSeek();
        Log.i(TAG, "New URL: " + pendingUrl);

        // Parse duration and seek offset for streaming transcode
        realDurationMs = intent.getLongExtra("duration", 0);
        seekOffsetMs = intent.getLongExtra("seek_offset", 0);
        Log.i(TAG, "Duration: " + realDurationMs + "ms, seekOffset: " + seekOffsetMs + "ms");

        // Derive server base URL from media URL for seek-ahead requests
        try {
            URL u = new URL(pendingUrl);
            serverBaseUrl = u.getProtocol() + "://" + u.getHost() + ":" + u.getPort();
        } catch (Exception e) {
            serverBaseUrl = null;
        }

        parseTrackExtras(intent);
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
                    enumerateAudioTracks();
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
            player.setOnTimedTextListener(new MediaPlayer.OnTimedTextListener() {
                @Override
                public void onTimedText(MediaPlayer mp, TimedText text) {
                    if (text != null && text.getText() != null) {
                        subtitleTextView.setText(text.getText());
                        subtitleTextView.setVisibility(View.VISIBLE);
                    } else {
                        subtitleTextView.setText("");
                    }
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
        // Duration override info
        sb.append(",\"realDurationMs\":").append(realDurationMs);
        sb.append(",\"seekOffsetMs\":").append(seekOffsetMs);
        // Track info
        sb.append(",\"audioTrackCount\":").append(audioTrackIndices.length);
        sb.append(",\"currentAudioTrack\":").append(currentAudioTrackIdx);
        sb.append(",\"audioTrackLabels\":[");
        for (int i = 0; i < audioTrackLabels.length; i++) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(audioTrackLabels[i].replace("\"", "\\\"")).append("\"");
        }
        sb.append("]");
        sb.append(",\"subtitleCount\":").append(subtitleUrls.length);
        sb.append(",\"currentSubtitle\":").append(currentSubtitleIdx);
        sb.append(",\"subtitleLabels\":[");
        for (int i = 0; i < subtitleLabels.length; i++) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(subtitleLabels[i].replace("\"", "\\\"")).append("\"");
        }
        sb.append("]");
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

            case KeyEvent.KEYCODE_DPAD_UP:
                cycleSubtitleTrack();
                return true;

            case KeyEvent.KEYCODE_DPAD_DOWN:
                cycleAudioTrack();
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

        // Use pending target as base so rapid presses accumulate
        long basePos;
        if (pendingSeekTarget >= 0) {
            basePos = pendingSeekTarget;
        } else {
            basePos = seekOffsetMs + player.getCurrentPosition();
        }

        long targetRealPos = basePos + ms;
        long totalDur = realDurationMs > 0 ? realDurationMs : player.getDuration();
        if (targetRealPos < 0) targetRealPos = 0;
        if (targetRealPos > totalDur) targetRealPos = totalDur;

        long localTarget = targetRealPos - seekOffsetMs;
        int playerDur = player.getDuration();

        if (localTarget >= 0 && (playerDur <= 0 || localTarget <= playerDur)) {
            // Within available content — seek locally, cancel any pending server seek
            cancelPendingSeek();
            player.seekTo((int) localTarget);
        } else {
            // Beyond available content — clamp locally, schedule debounced server seek
            if (localTarget < 0) localTarget = 0;
            if (playerDur > 0 && localTarget > playerDur) localTarget = playerDur;
            player.seekTo((int) localTarget);
            scheduleServerSeek(targetRealPos);
        }
        updateControlsState();
    }

    private void scheduleServerSeek(final long targetMs) {
        if (pendingSeekRunnable != null) {
            handler.removeCallbacks(pendingSeekRunnable);
        }
        pendingSeekTarget = targetMs;
        pendingSeekRunnable = new Runnable() {
            @Override
            public void run() {
                pendingSeekRunnable = null;
                pendingSeekTarget = -1;
                requestServerSeek(targetMs);
            }
        };
        // Wait 1.5s after last key press before triggering server seek
        handler.postDelayed(pendingSeekRunnable, 1500);
    }

    private void cancelPendingSeek() {
        if (pendingSeekRunnable != null) {
            handler.removeCallbacks(pendingSeekRunnable);
            pendingSeekRunnable = null;
        }
        pendingSeekTarget = -1;
    }

    private volatile boolean seekPending = false;

    private void requestServerSeek(final long positionMs) {
        if (serverBaseUrl == null || seekPending) return;
        seekPending = true;
        showTrackPopup("Seeking...");
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    URL url = new URL(serverBaseUrl + "/api/seek");
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setRequestMethod("POST");
                    conn.setRequestProperty("Content-Type", "application/json");
                    conn.setDoOutput(true);
                    conn.setConnectTimeout(5000);
                    conn.setReadTimeout(30000);
                    String body = "{\"position_ms\":" + positionMs + "}";
                    conn.getOutputStream().write(body.getBytes());
                    conn.getOutputStream().flush();
                    int code = conn.getResponseCode();
                    conn.disconnect();
                    Log.i(TAG, "Server seek to " + positionMs + "ms, response: " + code);
                } catch (Exception e) {
                    Log.e(TAG, "Server seek failed", e);
                    handler.post(new Runnable() {
                        @Override
                        public void run() {
                            showTrackPopup("Seek failed");
                        }
                    });
                } finally {
                    seekPending = false;
                }
            }
        }).start();
    }

    // --- Cleanup ---

    private void releasePlayer() {
        cancelPendingSeek();
        handler.removeCallbacks(positionUpdater);
        handler.removeCallbacks(hideControlsRunnable);
        handler.removeCallbacks(hidePopupRunnable);
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
        if (subtitleTmpFile != null) {
            subtitleTmpFile.delete();
            subtitleTmpFile = null;
        }
        subtitleTextView.setVisibility(View.GONE);
        subtitleTextView.setText("");
        trackPopupTv.setVisibility(View.GONE);
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
