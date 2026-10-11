package dev.droidrt.agent;

import android.app.ActivityOptions;
import android.app.Notification;
import android.app.PendingIntent;
import android.app.RemoteInput;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * Notification half of the agent: reports active notifications plus a ring
 * buffer of the last 200 posted ones, and fires notification actions,
 * filling RemoteInput (quick reply) when needed.
 */
public final class NotifyService extends NotificationListenerService {
    private static final int RING_SIZE = 200;
    private static final ArrayDeque<StatusBarNotification> RING = new ArrayDeque<>();

    static volatile NotifyService instance;

    @Override
    public void onListenerConnected() {
        instance = this;
        AgentServer.start(this);
    }

    @Override
    public void onListenerDisconnected() {
        instance = null;
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        synchronized (RING) {
            if (RING.size() >= RING_SIZE) {
                RING.removeFirst();
            }
            RING.addLast(sbn);
        }
    }

    private static NotifyService service() throws AgentException {
        NotifyService svc = instance;
        if (svc == null) {
            throw new AgentException("notification listener not connected");
        }
        return svc;
    }

    private static List<StatusBarNotification> active(NotifyService svc) {
        List<StatusBarNotification> out = new ArrayList<>();
        try {
            StatusBarNotification[] current = svc.getActiveNotifications();
            if (current != null) {
                for (StatusBarNotification sbn : current) {
                    out.add(sbn);
                }
            }
        } catch (SecurityException ignored) {
            // The listener was revoked between the check and the call.
        }
        return out;
    }

    private static List<StatusBarNotification> ringSnapshot() {
        synchronized (RING) {
            return new ArrayList<>(RING);
        }
    }

    static JSONArray list(JSONObject a) throws Exception {
        NotifyService svc = service();
        String pkg = a.isNull("package") ? null : a.optString("package", null);
        long since = a.isNull("sinceMs") ? Long.MIN_VALUE : a.optLong("sinceMs", Long.MIN_VALUE);

        List<StatusBarNotification> merged = new ArrayList<>(active(svc));
        Set<String> seen = new HashSet<>();
        for (StatusBarNotification sbn : merged) {
            seen.add(sbn.getKey() + "@" + sbn.getPostTime());
        }
        for (StatusBarNotification sbn : ringSnapshot()) {
            if (seen.add(sbn.getKey() + "@" + sbn.getPostTime())) {
                merged.add(sbn);
            }
        }
        merged.sort((x, y) -> Long.compare(x.getPostTime(), y.getPostTime()));

        JSONArray out = new JSONArray();
        for (StatusBarNotification sbn : merged) {
            if (pkg != null && !pkg.equals(sbn.getPackageName())) {
                continue;
            }
            if (sbn.getPostTime() < since) {
                continue;
            }
            out.put(describe(sbn));
        }
        return out;
    }

    private static Object text(Bundle extras, String key) {
        CharSequence cs = extras == null ? null : extras.getCharSequence(key);
        String s = Nodes.str(cs);
        return s == null ? JSONObject.NULL : s;
    }

    private static JSONObject describe(StatusBarNotification sbn) throws JSONException {
        Notification n = sbn.getNotification();
        Bundle extras = n.extras;
        Object title = text(extras, Notification.EXTRA_TITLE);
        if (title == JSONObject.NULL) {
            title = text(extras, Notification.EXTRA_TITLE_BIG);
        }
        Object body = text(extras, Notification.EXTRA_TEXT);
        if (body == JSONObject.NULL) {
            body = text(extras, Notification.EXTRA_BIG_TEXT);
        }
        JSONArray actions = new JSONArray();
        if (n.actions != null) {
            for (int i = 0; i < n.actions.length; i++) {
                Notification.Action act = n.actions[i];
                RemoteInput[] inputs = act.getRemoteInputs();
                String label = Nodes.str(act.title);
                actions.put(new JSONObject()
                        .put("index", i)
                        .put("title", label == null ? JSONObject.NULL : label)
                        .put("remoteInput", inputs != null && inputs.length > 0));
            }
        }
        return new JSONObject()
                .put("key", sbn.getKey())
                .put("package", sbn.getPackageName())
                .put("postTime", sbn.getPostTime())
                .put("title", title)
                .put("text", body)
                .put("subText", text(extras, Notification.EXTRA_SUB_TEXT))
                .put("actions", actions);
    }

    /** The active notification with this key, or the newest buffered one. */
    private static StatusBarNotification lookup(NotifyService svc, String key) {
        for (StatusBarNotification sbn : active(svc)) {
            if (key.equals(sbn.getKey())) {
                return sbn;
            }
        }
        List<StatusBarNotification> ring = ringSnapshot();
        for (int i = ring.size() - 1; i >= 0; i--) {
            if (key.equals(ring.get(i).getKey())) {
                return ring.get(i);
            }
        }
        return null;
    }

    static JSONObject act(JSONObject a) throws Exception {
        NotifyService svc = service();
        String key = a.isNull("key") ? null : a.optString("key", null);
        if (key == null) {
            throw new AgentException("key is required");
        }
        if (a.isNull("index")) {
            throw new AgentException("index is required");
        }
        int index = a.getInt("index");
        String reply = a.isNull("text") ? null : a.optString("text", null);

        StatusBarNotification sbn = lookup(svc, key);
        if (sbn == null) {
            throw new AgentException("no such notification: " + key);
        }
        Notification.Action[] actions = sbn.getNotification().actions;
        if (actions == null || index < 0 || index >= actions.length) {
            throw new AgentException("no such action: " + index);
        }
        Notification.Action action = actions[index];
        if (action.actionIntent == null) {
            return new JSONObject().put("performed", false);
        }

        Intent fill = null;
        RemoteInput[] inputs = action.getRemoteInputs();
        if (inputs != null && inputs.length > 0) {
            if (reply == null) {
                throw new AgentException("text is required for an action with remote input");
            }
            Bundle results = new Bundle();
            for (RemoteInput input : inputs) {
                results.putCharSequence(input.getResultKey(), reply);
            }
            fill = new Intent();
            RemoteInput.addResultsToIntent(inputs, fill, results);
            RemoteInput.setResultsSource(fill, RemoteInput.SOURCE_FREE_FORM_INPUT);
        }
        return new JSONObject().put("performed", send(svc, action.actionIntent, fill));
    }

    private static boolean send(Context ctx, PendingIntent intent, Intent fill) {
        Bundle options = null;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            ActivityOptions opts = ActivityOptions.makeBasic();
            opts.setPendingIntentBackgroundActivityStartMode(
                    ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOWED);
            options = opts.toBundle();
        }
        try {
            intent.send(ctx, 0, fill, null, null, null, options);
            return true;
        } catch (PendingIntent.CanceledException e) {
            return false;
        }
    }
}
