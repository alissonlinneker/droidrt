package dev.droidrt.agent;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.util.SparseArray;
import android.view.Display;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** Implementation of every command in docs/agent-protocol.md. */
final class Commands {
    static final String PROTOCOL_VERSION = "2";
    private static final int FIND_LIMIT = 500;
    private static final long TAP_MS = 50;
    private static final long SWIPE_MS = 300;
    private static final long MAX_GESTURE_MS = GestureDescription.getMaxGestureDuration();

    private Commands() {
    }

    static Object dispatch(String cmd, JSONObject a) throws Exception {
        switch (cmd) {
            case "ping":
                return ping();
            case "displays":
                return displays();
            case "tree":
                return tree(a);
            case "find":
                return find(a);
            case "action":
                return action(a);
            case "tap":
                return tap(a);
            case "swipe":
                return swipe(a);
            case "global":
                return global(a);
            case "notifications":
                return NotifyService.list(a);
            case "notification_action":
                return NotifyService.act(a);
            case "wait_idle":
                return waitIdle(a);
            case "":
                throw new AgentException("cmd is required");
            default:
                throw new AgentException("unknown command: " + cmd);
        }
    }

    static AgentService service() throws AgentException {
        AgentService svc = AgentService.instance;
        if (svc == null) {
            throw new AgentException("accessibility service not connected");
        }
        return svc;
    }

    private static JSONObject performed(boolean value) throws JSONException {
        return new JSONObject().put("performed", value);
    }

    private static String optString(JSONObject a, String key) {
        return a.isNull(key) ? null : a.optString(key, null);
    }

    // ping

    private static JSONObject ping() throws JSONException {
        return new JSONObject().put("version", PROTOCOL_VERSION).put("sdk", Build.VERSION.SDK_INT);
    }

    // displays

    @SuppressWarnings("deprecation") // getRealMetrics is the only way to size a non default display.
    private static JSONArray displays() throws Exception {
        AgentService svc = service();
        DisplayManager dm = svc.getSystemService(DisplayManager.class);
        SparseArray<List<AccessibilityWindowInfo>> all = svc.getWindowsOnAllDisplays();
        JSONArray out = new JSONArray();
        for (int i = 0; i < all.size(); i++) {
            int displayId = all.keyAt(i);
            List<AccessibilityWindowInfo> windows = all.valueAt(i);
            Set<String> packages = new LinkedHashSet<>();
            Rect union = new Rect();
            Rect r = new Rect();
            for (AccessibilityWindowInfo w : windows) {
                w.getBoundsInScreen(r);
                union.union(r);
                AccessibilityNodeInfo root = w.getRoot();
                String pkg = root == null ? null : Nodes.str(root.getPackageName());
                if (pkg != null) {
                    packages.add(pkg);
                }
            }
            int width = union.width();
            int height = union.height();
            Display display = dm == null ? null : dm.getDisplay(displayId);
            if (display != null) {
                DisplayMetrics metrics = new DisplayMetrics();
                display.getRealMetrics(metrics);
                width = metrics.widthPixels;
                height = metrics.heightPixels;
            }
            out.put(new JSONObject()
                    .put("displayId", displayId)
                    .put("width", width)
                    .put("height", height)
                    .put("packages", new JSONArray(new ArrayList<>(packages))));
        }
        return out;
    }

    // Window selection shared by tree and find. When a package is given it
    // wins over displayId, as the contract says ("whatever the display").

    private interface WindowVisitor {
        void visit(AccessibilityWindowInfo window, AccessibilityNodeInfo root) throws Exception;
    }

    private static void forEachWindow(AgentService svc, JSONObject a, boolean visibleOnly,
                                      WindowVisitor visitor) throws Exception {
        String pkg = optString(a, "package");
        boolean byDisplay = pkg == null && a.has("displayId") && !a.isNull("displayId");
        int displayId = byDisplay ? a.getInt("displayId") : 0;
        SparseArray<List<AccessibilityWindowInfo>> all = svc.getWindowsOnAllDisplays();
        for (int i = 0; i < all.size(); i++) {
            if (byDisplay && all.keyAt(i) != displayId) {
                continue;
            }
            for (AccessibilityWindowInfo w : all.valueAt(i)) {
                AccessibilityNodeInfo root = w.getRoot();
                if (root == null) {
                    continue;
                }
                if (pkg != null && !pkg.equals(Nodes.str(root.getPackageName()))) {
                    continue;
                }
                if (visibleOnly && !root.isVisibleToUser()) {
                    continue;
                }
                visitor.visit(w, root);
            }
        }
    }

    // tree: a list with one Node per matching window, rooted at the window root.

    private static JSONArray tree(JSONObject a) throws Exception {
        AgentService svc = service();
        int maxDepth = a.isNull("maxDepth") ? -1 : a.optInt("maxDepth", -1);
        boolean visibleOnly = a.isNull("visibleOnly") || a.optBoolean("visibleOnly", true);
        JSONArray out = new JSONArray();
        forEachWindow(svc, a, visibleOnly, (w, root) ->
                out.put(Nodes.subtree(root, Nodes.rootId(w), w.getDisplayId(), 0, maxDepth, visibleOnly)));
        return out;
    }

    // find

    private static final class Query {
        String text;
        String textContains;
        String resourceId;
        String contentDesc;
        String className;
        Boolean clickable;

        boolean matches(AccessibilityNodeInfo n) {
            if (text != null && !text.equals(Nodes.str(n.getText()))) {
                return false;
            }
            if (textContains != null) {
                String t = Nodes.str(n.getText());
                if (t == null || !t.toLowerCase(Locale.ROOT).contains(textContains)) {
                    return false;
                }
            }
            if (resourceId != null) {
                String rid = Nodes.str(n.getViewIdResourceName());
                boolean ok = rid != null && (rid.equals(resourceId)
                        || (resourceId.indexOf(':') < 0 && rid.endsWith(":id/" + resourceId)));
                if (!ok) {
                    return false;
                }
            }
            if (contentDesc != null && !contentDesc.equals(Nodes.str(n.getContentDescription()))) {
                return false;
            }
            if (className != null) {
                String cls = Nodes.str(n.getClassName());
                if (cls == null || !(cls.equals(className) || cls.endsWith("." + className))) {
                    return false;
                }
            }
            return clickable == null || clickable == n.isClickable();
        }
    }

    private static JSONArray find(JSONObject a) throws Exception {
        AgentService svc = service();
        Query q = new Query();
        q.text = optString(a, "text");
        String contains = optString(a, "textContains");
        q.textContains = contains == null ? null : contains.toLowerCase(Locale.ROOT);
        q.resourceId = optString(a, "resourceId");
        q.contentDesc = optString(a, "contentDesc");
        q.className = optString(a, "className");
        q.clickable = a.isNull("clickable") ? null : a.getBoolean("clickable");
        boolean visibleOnly = a.isNull("visibleOnly") || a.optBoolean("visibleOnly", true);
        JSONArray out = new JSONArray();
        forEachWindow(svc, a, visibleOnly, (w, root) ->
                collect(root, Nodes.rootId(w), w.getDisplayId(), q, visibleOnly, out));
        return out;
    }

    private static void collect(AccessibilityNodeInfo n, String id, int displayId, Query q,
                                boolean visibleOnly, JSONArray out) throws JSONException {
        if (out.length() >= FIND_LIMIT) {
            return;
        }
        if (q.matches(n)) {
            out.put(Nodes.describe(n, id, displayId));
        }
        int count = n.getChildCount();
        for (int i = 0; i < count; i++) {
            AccessibilityNodeInfo child = n.getChild(i);
            if (child == null || (visibleOnly && !child.isVisibleToUser())) {
                continue;
            }
            collect(child, id + "." + i, displayId, q, visibleOnly, out);
        }
    }

    // action

    private static JSONObject action(JSONObject a) throws Exception {
        AgentService svc = service();
        String name = optString(a, "action");
        if (name == null) {
            throw new AgentException("action is required");
        }
        AccessibilityNodeInfo node = Nodes.resolve(svc, optString(a, "nodeId"));
        switch (name) {
            case "click":
                return performed(performOnSelfOrAncestor(node, AccessibilityNodeInfo.ACTION_CLICK, false));
            case "long_click":
                return performed(performOnSelfOrAncestor(node, AccessibilityNodeInfo.ACTION_LONG_CLICK, true));
            case "focus":
                return performed(node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
                        || node.performAction(AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS));
            case "set_text": {
                String text = optString(a, "text");
                if (text == null && !a.has("text")) {
                    throw new AgentException("text is required for set_text");
                }
                return performed(setText(node, text == null ? "" : text));
            }
            case "clear_text":
                return performed(setText(node, ""));
            case "scroll_forward":
                return performed(node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD));
            case "scroll_backward":
                return performed(node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD));
            case "select":
                return performed(node.performAction(AccessibilityNodeInfo.ACTION_SELECT));
            case "ime_enter":
                return performed(node.performAction(
                        AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId()));
            default:
                throw new AgentException("unknown action: " + name);
        }
    }

    /**
     * Labels inside list rows are often not clickable themselves. When the
     * target refuses the action and is not clickable, the nearest clickable
     * ancestor gets it instead, which is what a finger tap would hit.
     */
    private static boolean performOnSelfOrAncestor(AccessibilityNodeInfo node, int action, boolean longClick) {
        if (node.performAction(action)) {
            return true;
        }
        boolean able = longClick ? node.isLongClickable() : node.isClickable();
        if (able) {
            return false;
        }
        for (AccessibilityNodeInfo p = node.getParent(); p != null; p = p.getParent()) {
            if (longClick ? p.isLongClickable() : p.isClickable()) {
                return p.performAction(action);
            }
        }
        return false;
    }

    private static boolean setText(AccessibilityNodeInfo node, String text) {
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
    }

    // tap and swipe

    private static long duration(JSONObject a, long fallback) {
        long ms = a.isNull("durationMs") ? fallback : a.optLong("durationMs", fallback);
        return Math.max(1, Math.min(ms, MAX_GESTURE_MS));
    }

    private static JSONObject tap(JSONObject a) throws Exception {
        AgentService svc = service();
        Path path = new Path();
        path.moveTo(a.getInt("x"), a.getInt("y"));
        return performed(dispatch(svc, path, duration(a, TAP_MS), a.getInt("displayId")));
    }

    private static JSONObject swipe(JSONObject a) throws Exception {
        AgentService svc = service();
        Path path = new Path();
        path.moveTo(a.getInt("x1"), a.getInt("y1"));
        path.lineTo(a.getInt("x2"), a.getInt("y2"));
        return performed(dispatch(svc, path, duration(a, SWIPE_MS), a.getInt("displayId")));
    }

    private static boolean dispatch(AgentService svc, Path path, long durationMs, int displayId)
            throws InterruptedException, AgentException {
        GestureDescription gesture;
        try {
            gesture = new GestureDescription.Builder()
                    .addStroke(new GestureDescription.StrokeDescription(path, 0, durationMs))
                    .setDisplayId(displayId)
                    .build();
        } catch (IllegalArgumentException | IllegalStateException e) {
            throw new AgentException("invalid gesture: " + e.getMessage());
        }
        CountDownLatch done = new CountDownLatch(1);
        AtomicBoolean completed = new AtomicBoolean(false);
        boolean accepted = svc.dispatchGesture(gesture, new AccessibilityService.GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription description) {
                completed.set(true);
                done.countDown();
            }

            @Override
            public void onCancelled(GestureDescription description) {
                done.countDown();
            }
        }, null);
        if (!accepted) {
            return false;
        }
        done.await(durationMs + 5000, TimeUnit.MILLISECONDS);
        return completed.get();
    }

    // global

    private static JSONObject global(JSONObject a) throws Exception {
        AgentService svc = service();
        String name = optString(a, "action");
        int action;
        if ("back".equals(name)) {
            action = AccessibilityService.GLOBAL_ACTION_BACK;
        } else if ("home".equals(name)) {
            action = AccessibilityService.GLOBAL_ACTION_HOME;
        } else if ("recents".equals(name)) {
            action = AccessibilityService.GLOBAL_ACTION_RECENTS;
        } else if ("notifications".equals(name)) {
            action = AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS;
        } else {
            throw new AgentException("unknown global action: " + name);
        }
        return performed(svc.performGlobalAction(action));
    }

    // wait_idle

    private static JSONObject waitIdle(JSONObject a) throws Exception {
        service();
        long timeoutMs = a.isNull("timeoutMs") ? 5000 : a.optLong("timeoutMs", 5000);
        long idleMs = a.isNull("idleMs") ? 500 : a.optLong("idleMs", 500);
        long deadline = SystemClock.uptimeMillis() + Math.max(0, timeoutMs);
        while (true) {
            long now = SystemClock.uptimeMillis();
            long quiet = now - AgentService.lastEventUptime;
            if (quiet >= idleMs) {
                return new JSONObject().put("idle", true);
            }
            if (now >= deadline) {
                return new JSONObject().put("idle", false);
            }
            Thread.sleep(Math.max(1, Math.min(50, Math.min(idleMs - quiet, deadline - now))));
        }
    }
}
