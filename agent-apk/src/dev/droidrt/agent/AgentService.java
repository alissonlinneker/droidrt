package dev.droidrt.agent;

import android.accessibilityservice.AccessibilityService;
import android.content.Intent;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityEvent;

/**
 * Accessibility half of the agent. It holds no state of its own besides the
 * time of the last accessibility event, which backs the wait_idle command.
 * All requests are served by {@link AgentServer}.
 */
public final class AgentService extends AccessibilityService {
    static volatile AgentService instance;
    static volatile long lastEventUptime = SystemClock.uptimeMillis();

    @Override
    protected void onServiceConnected() {
        instance = this;
        lastEventUptime = SystemClock.uptimeMillis();
        AgentServer.start(this);
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        lastEventUptime = SystemClock.uptimeMillis();
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public boolean onUnbind(Intent intent) {
        instance = null;
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        instance = null;
        super.onDestroy();
    }
}
