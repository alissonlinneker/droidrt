package dev.droidrt.agent;

import android.graphics.Rect;
import android.util.SparseArray;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Node serialization and node id resolution.
 *
 * A node id is "w&lt;windowId&gt;:&lt;path&gt;", where path is the dot separated list
 * of child indexes from the window root, starting with "0" for the root
 * itself. Indexes are the real child indexes, so hiding invisible siblings
 * never shifts the id of a visible node.
 */
final class Nodes {
    private static final Pattern NODE_ID = Pattern.compile("^w(-?\\d+):(\\d+(?:\\.\\d+)*)$");

    private Nodes() {
    }

    static String rootId(AccessibilityWindowInfo window) {
        return "w" + window.getId() + ":0";
    }

    static String str(CharSequence cs) {
        if (cs == null) {
            return null;
        }
        String s = cs.toString();
        return s.isEmpty() ? null : s;
    }

    private static Object orNull(Object value) {
        return value == null ? JSONObject.NULL : value;
    }

    /** One node without children, in the contract's Node format. */
    static JSONObject describe(AccessibilityNodeInfo n, String id, int displayId) throws JSONException {
        JSONObject o = new JSONObject();
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        o.put("id", id);
        o.put("cls", orNull(str(n.getClassName())));
        o.put("pkg", orNull(str(n.getPackageName())));
        o.put("text", orNull(str(n.getText())));
        o.put("desc", orNull(str(n.getContentDescription())));
        o.put("rid", orNull(str(n.getViewIdResourceName())));
        o.put("bounds", new JSONArray().put(r.left).put(r.top).put(r.right).put(r.bottom));
        o.put("displayId", displayId);
        o.put("clickable", n.isClickable());
        o.put("editable", n.isEditable());
        o.put("scrollable", n.isScrollable());
        o.put("checked", n.isCheckable() ? (Object) n.isChecked() : JSONObject.NULL);
        o.put("enabled", n.isEnabled());
        o.put("focused", n.isFocused());
        o.put("selected", n.isSelected());
        o.put("password", n.isPassword());
        return o;
    }

    /** A node with its subtree. maxDepth &lt; 0 means unlimited; 0 returns the node alone. */
    static JSONObject subtree(AccessibilityNodeInfo n, String id, int displayId, int depth,
                              int maxDepth, boolean visibleOnly) throws JSONException {
        JSONObject o = describe(n, id, displayId);
        if (maxDepth >= 0 && depth >= maxDepth) {
            return o;
        }
        JSONArray kids = new JSONArray();
        int count = n.getChildCount();
        for (int i = 0; i < count; i++) {
            AccessibilityNodeInfo child = n.getChild(i);
            if (child == null || (visibleOnly && !child.isVisibleToUser())) {
                continue;
            }
            kids.put(subtree(child, id + "." + i, displayId, depth + 1, maxDepth, visibleOnly));
        }
        if (kids.length() > 0) {
            o.put("children", kids);
        }
        return o;
    }

    static AccessibilityWindowInfo findWindow(AgentService svc, int windowId) {
        SparseArray<List<AccessibilityWindowInfo>> all = svc.getWindowsOnAllDisplays();
        for (int i = 0; i < all.size(); i++) {
            for (AccessibilityWindowInfo w : all.valueAt(i)) {
                if (w.getId() == windowId) {
                    return w;
                }
            }
        }
        return null;
    }

    /** Resolves a node id, or fails with "stale node" when the path no longer exists. */
    static AccessibilityNodeInfo resolve(AgentService svc, String nodeId) throws AgentException {
        if (nodeId == null) {
            throw new AgentException("nodeId is required");
        }
        Matcher m = NODE_ID.matcher(nodeId);
        if (!m.matches()) {
            throw new AgentException("invalid nodeId: " + nodeId);
        }
        String[] path = m.group(2).split("\\.");
        if (!"0".equals(path[0])) {
            throw new AgentException("invalid nodeId: " + nodeId);
        }
        int windowId;
        int[] indexes = new int[path.length];
        try {
            windowId = Integer.parseInt(m.group(1));
            for (int i = 1; i < path.length; i++) {
                indexes[i] = Integer.parseInt(path[i]);
            }
        } catch (NumberFormatException e) {
            // Digits that overflow an int cannot name a real window or child.
            throw new AgentException("invalid nodeId: " + nodeId);
        }
        AccessibilityWindowInfo window = findWindow(svc, windowId);
        if (window == null) {
            throw new AgentException("stale node");
        }
        AccessibilityNodeInfo node = window.getRoot();
        for (int i = 1; i < path.length && node != null; i++) {
            int index = indexes[i];
            node = index < node.getChildCount() ? node.getChild(index) : null;
        }
        if (node == null || !node.refresh()) {
            throw new AgentException("stale node");
        }
        return node;
    }
}
