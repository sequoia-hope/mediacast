package com.mediacast.eqplayer;

import android.util.Log;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;

/**
 * Minimal HTTP server for receiving EQ commands from castweb.
 *
 * POST /eq       — body: {"bands": [300, 0, -200, 0, 200]} (millibels per band)
 * POST /loudnorm — body: {"enabled": true, "gain": 600}
 * GET  /info     — return band count, frequency ranges, current levels, player status
 */
public class EqServer {

    private static final String TAG = "EQServer";
    private final MainActivity activity;
    private final int port;
    private ServerSocket serverSocket;
    private volatile boolean running = false;

    public EqServer(MainActivity activity, int port) {
        this.activity = activity;
        this.port = port;
    }

    public void start() {
        running = true;
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    serverSocket = new ServerSocket(port);
                    Log.i(TAG, "Listening on port " + port);
                    while (running) {
                        try {
                            Socket client = serverSocket.accept();
                            handleClient(client);
                        } catch (IOException e) {
                            if (running) Log.e(TAG, "Accept error", e);
                        }
                    }
                } catch (IOException e) {
                    Log.e(TAG, "Server start error", e);
                }
            }
        }, "EqServer").start();
    }

    public void stopServer() {
        running = false;
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (IOException e) {}
    }

    private void handleClient(Socket client) {
        try {
            client.setSoTimeout(5000);
            InputStream is = client.getInputStream();
            BufferedReader reader = new BufferedReader(new InputStreamReader(is));
            OutputStream os = client.getOutputStream();

            // Read request line
            String requestLine = reader.readLine();
            if (requestLine == null) { client.close(); return; }

            String[] parts = requestLine.split(" ");
            if (parts.length < 2) { client.close(); return; }
            String method = parts[0];
            String path = parts[1];

            // Read headers
            int contentLength = 0;
            String line;
            while ((line = reader.readLine()) != null && !line.isEmpty()) {
                if (line.toLowerCase().startsWith("content-length:")) {
                    contentLength = Integer.parseInt(line.substring(15).trim());
                }
            }

            // Read body
            String body = "";
            if (contentLength > 0) {
                char[] buf = new char[contentLength];
                int read = 0;
                while (read < contentLength) {
                    int n = reader.read(buf, read, contentLength - read);
                    if (n == -1) break;
                    read += n;
                }
                body = new String(buf, 0, read);
            }

            // Route
            String response;
            if ("GET".equals(method) && "/info".equals(path)) {
                response = handleInfo();
            } else if ("POST".equals(method) && "/eq".equals(path)) {
                response = handleEq(body);
            } else if ("POST".equals(method) && "/loudnorm".equals(path)) {
                response = handleLoudnorm(body);
            } else {
                sendResponse(os, 404, "{\"error\":\"not found\"}");
                client.close();
                return;
            }

            sendResponse(os, 200, response);
            client.close();
        } catch (Exception e) {
            Log.e(TAG, "Client error", e);
            try { client.close(); } catch (IOException ex) {}
        }
    }

    private void sendResponse(OutputStream os, int status, String body) throws IOException {
        String statusText = status == 200 ? "OK" : "Not Found";
        String headers = "HTTP/1.1 " + status + " " + statusText + "\r\n"
                + "Content-Type: application/json\r\n"
                + "Access-Control-Allow-Origin: *\r\n"
                + "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                + "Access-Control-Allow-Headers: Content-Type\r\n"
                + "Content-Length: " + body.length() + "\r\n"
                + "\r\n";
        os.write(headers.getBytes());
        os.write(body.getBytes());
        os.flush();
    }

    private String handleInfo() {
        return activity.getInfoJson();
    }

    private String handleEq(String body) {
        // Parse {"bands": [300, 0, -200, 0, 200]}
        try {
            // Minimal JSON parsing — extract the bands array
            int idx = body.indexOf("[");
            int end = body.indexOf("]");
            if (idx < 0 || end < 0) return "{\"error\":\"no bands array\"}";

            String arr = body.substring(idx + 1, end).trim();
            if (arr.isEmpty()) return "{\"error\":\"empty bands\"}";

            String[] vals = arr.split(",");
            final short[] bands = new short[vals.length];
            for (int i = 0; i < vals.length; i++) {
                bands[i] = Short.parseShort(vals[i].trim());
            }

            activity.runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    activity.updateEq(bands);
                }
            });

            return "{\"ok\":true}";
        } catch (Exception e) {
            return "{\"error\":\"" + e.getMessage() + "\"}";
        }
    }

    private String handleLoudnorm(String body) {
        // Parse {"enabled": true, "gain": 600}
        try {
            final boolean enabled = body.contains("true");
            int gain = 600; // default gain in millibels
            int gIdx = body.indexOf("\"gain\"");
            if (gIdx >= 0) {
                String sub = body.substring(gIdx + 6);
                // Find the number after the colon
                int cIdx = sub.indexOf(":");
                if (cIdx >= 0) {
                    String numPart = sub.substring(cIdx + 1).replaceAll("[^0-9\\-]", " ").trim().split("\\s+")[0];
                    gain = Integer.parseInt(numPart);
                }
            }
            final int finalGain = gain;

            activity.runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    activity.setLoudnessEnhancerEnabled(enabled, finalGain);
                }
            });

            return "{\"ok\":true}";
        } catch (Exception e) {
            return "{\"error\":\"" + e.getMessage() + "\"}";
        }
    }
}
