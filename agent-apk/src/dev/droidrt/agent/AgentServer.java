package dev.droidrt.agent;

import android.content.Context;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.regex.Pattern;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * JSON-lines server on the abstract local socket "droidrt_agent".
 *
 * Each connection gets its own thread that reads one request per line and
 * writes one response per line, in order, so requests can be pipelined.
 *
 * Security has two layers:
 *
 * 1. Peer credentials: an abstract socket can be reached by every app on the
 *    device, so only peers running as root (uid 0) or as the adb shell
 *    (uid 2000) are accepted. Connections forwarded by "adb forward" come
 *    from adbd, which runs as shell (or root after "adb root"). Any other
 *    peer is dropped before a single byte is read.
 * 2. Mutual authentication: the host TCP port created by "adb forward" can
 *    be reached by anything on the host, including web pages, so every
 *    connection must prove knowledge of the shared key (see AgentKey) before
 *    a request is processed, and the agent proves the same back:
 *
 *      agent:  {"hello":"droidrt-agent","v":2,"nonce":"<agent nonce>"}
 *      client: {"auth":"<HMAC(key, "client:" + agent nonce)>","nonce":"<client nonce>"}
 *      agent:  {"ok":true,"proof":"<HMAC(key, "agent:" + client nonce)>"}
 *
 *    HMAC is HMAC-SHA256 over the UTF-8 text, hex encoded. A first line that
 *    is not a well formed auth object closes the connection silently.
 */
final class AgentServer {
    static final String SOCKET_NAME = "droidrt_agent";
    private static final String TAG = "droidrt-agent";
    private static final int UID_ROOT = 0;
    private static final int UID_SHELL = 2000;
    static final int HANDSHAKE_VERSION = 2;
    static final int MAX_LINE_BYTES = 1024 * 1024;
    private static final int HANDSHAKE_TIMEOUT_MS = 10_000;
    private static final int NONCE_BYTES = 16;
    private static final Pattern NONCE_HEX = Pattern.compile("^[0-9a-fA-F]{32}$");
    private static final Pattern MAC_HEX = Pattern.compile("^[0-9a-fA-F]{64}$");
    private static final SecureRandom RANDOM = new SecureRandom();

    private static Thread acceptor;

    private AgentServer() {
    }

    /** Starts the accept loop once per process. Safe to call from both services. */
    static synchronized void start(Context context) {
        AgentKey.init(context);
        if (acceptor != null) {
            return;
        }
        acceptor = new Thread(AgentServer::acceptLoop, "droidrt-accept");
        acceptor.setDaemon(true);
        acceptor.start();
    }

    private static void acceptLoop() {
        while (true) {
            try (LocalServerSocket server = new LocalServerSocket(SOCKET_NAME)) {
                Log.i(TAG, "listening on @" + SOCKET_NAME);
                while (true) {
                    LocalSocket socket = server.accept();
                    if (!isTrustedPeer(socket)) {
                        closeQuietly(socket);
                        continue;
                    }
                    Thread worker = new Thread(() -> serve(socket), "droidrt-conn");
                    worker.setDaemon(true);
                    worker.start();
                }
            } catch (IOException e) {
                Log.w(TAG, "server socket failed, retrying", e);
                try {
                    Thread.sleep(2000);
                } catch (InterruptedException ie) {
                    return;
                }
            }
        }
    }

    private static boolean isTrustedPeer(LocalSocket socket) {
        try {
            int uid = socket.getPeerCredentials().getUid();
            if (uid == UID_ROOT || uid == UID_SHELL) {
                return true;
            }
            Log.w(TAG, "rejected connection from uid " + uid);
        } catch (IOException e) {
            Log.w(TAG, "could not read peer credentials", e);
        }
        return false;
    }

    private static void serve(LocalSocket socket) {
        try (LocalSocket s = socket;
             InputStream in = new BufferedInputStream(s.getInputStream());
             OutputStream out = s.getOutputStream()) {
            s.setSoTimeout(HANDSHAKE_TIMEOUT_MS);
            if (!handshake(in, out)) {
                return;
            }
            s.setSoTimeout(0);
            String line;
            while ((line = readLine(in)) != null) {
                if (line.trim().isEmpty()) {
                    continue;
                }
                writeLine(out, handle(line));
            }
        } catch (IOException e) {
            Log.d(TAG, "connection closed: " + e.getMessage());
        }
    }

    /**
     * Runs the mutual authentication handshake. Returns true once the client
     * proved the key and the proof was sent back; false means the caller must
     * close the connection without reading anything else.
     */
    private static boolean handshake(InputStream in, OutputStream out) throws IOException {
        String agentNonce = randomNonce();
        writeLine(out, jsonLine("hello", "droidrt-agent", "v", HANDSHAKE_VERSION, "nonce", agentNonce));

        String line = readLine(in);
        if (line == null) {
            return false;
        }
        String auth;
        String clientNonce;
        try {
            JSONObject msg = new JSONObject(line);
            auth = msg.optString("auth", "");
            clientNonce = msg.optString("nonce", "");
        } catch (JSONException e) {
            Log.w(TAG, "dropped connection: first line is not an auth message");
            return false;
        }
        if (!MAC_HEX.matcher(auth).matches() || !NONCE_HEX.matcher(clientNonce).matches()) {
            Log.w(TAG, "dropped connection: first line is not an auth message");
            return false;
        }

        byte[] key = AgentKey.load();
        if (key == null) {
            Log.w(TAG, "dropped connection: no key configured");
            writeLine(out, jsonLine("ok", false, "error", "no key"));
            return false;
        }
        byte[] expected = hmac(key, "client:" + agentNonce);
        if (!MessageDigest.isEqual(expected, Hex.decode(auth))) {
            Log.w(TAG, "dropped connection: auth failed");
            writeLine(out, jsonLine("ok", false, "error", "auth failed"));
            return false;
        }
        writeLine(out, jsonLine("ok", true, "proof", Hex.encode(hmac(key, "agent:" + clientNonce))));
        return true;
    }

    private static String randomNonce() {
        byte[] nonce = new byte[NONCE_BYTES];
        RANDOM.nextBytes(nonce);
        return Hex.encode(nonce);
    }

    static byte[] hmac(byte[] key, String message) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            return mac.doFinal(message.getBytes(StandardCharsets.UTF_8));
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException("HmacSHA256 unavailable", e);
        }
    }

    private static String jsonLine(Object... pairs) {
        JSONObject obj = new JSONObject();
        try {
            for (int i = 0; i < pairs.length; i += 2) {
                obj.put((String) pairs[i], pairs[i + 1]);
            }
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
        return obj.toString();
    }

    private static void writeLine(OutputStream out, String line) throws IOException {
        out.write((line + "\n").getBytes(StandardCharsets.UTF_8));
        out.flush();
    }

    /**
     * Reads one '\n' terminated UTF-8 line (a trailing '\r' is dropped).
     * Returns null at end of stream. A line longer than MAX_LINE_BYTES throws,
     * which closes the connection.
     */
    static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream(256);
        int b;
        while ((b = in.read()) != -1) {
            if (b == '\n') {
                return decodeLine(buf);
            }
            if (buf.size() >= MAX_LINE_BYTES) {
                throw new IOException("line exceeds " + MAX_LINE_BYTES + " bytes");
            }
            buf.write(b);
        }
        return buf.size() == 0 ? null : decodeLine(buf);
    }

    private static String decodeLine(ByteArrayOutputStream buf) {
        String line = new String(buf.toByteArray(), StandardCharsets.UTF_8);
        return line.endsWith("\r") ? line.substring(0, line.length() - 1) : line;
    }

    static String handle(String line) {
        JSONObject resp = new JSONObject();
        Object id = JSONObject.NULL;
        try {
            JSONObject req;
            try {
                req = new JSONObject(line);
            } catch (JSONException e) {
                throw new AgentException("invalid request: " + e.getMessage());
            }
            id = req.opt("id");
            if (id == null) {
                id = JSONObject.NULL;
            }
            String cmd = req.optString("cmd", "");
            JSONObject args = req.optJSONObject("args");
            if (args == null) {
                args = new JSONObject();
            }
            Object result = Commands.dispatch(cmd, args);
            resp.put("id", id);
            resp.put("ok", true);
            resp.put("result", result == null ? JSONObject.NULL : result);
        } catch (Throwable t) {
            String message = t instanceof AgentException
                    ? t.getMessage()
                    : t.getClass().getSimpleName() + ": " + t.getMessage();
            if (!(t instanceof AgentException)) {
                Log.w(TAG, "command failed", t);
            }
            resp = new JSONObject();
            try {
                resp.put("id", id);
                resp.put("ok", false);
                resp.put("error", message);
            } catch (JSONException ignored) {
                // Keys and values are always valid here.
            }
        }
        return resp.toString();
    }

    private static void closeQuietly(LocalSocket socket) {
        try {
            socket.close();
        } catch (IOException ignored) {
            // Nothing useful to do.
        }
    }
}
