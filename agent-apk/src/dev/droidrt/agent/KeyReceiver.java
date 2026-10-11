package dev.droidrt.agent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * Receives the connection key from the host:
 *
 *   adb shell am broadcast -a dev.droidrt.agent.SET_KEY \
 *       -n dev.droidrt.agent/.KeyReceiver --es key <64 hex chars>
 *
 * The receiver is exported but guarded by android.permission.DUMP in the
 * manifest. The adb shell holds that permission; regular apps cannot get it,
 * so they can neither set nor rotate the key. A new key replaces the old one.
 * Result code 1 means stored, 0 means rejected.
 */
public final class KeyReceiver extends BroadcastReceiver {
    static final String ACTION_SET_KEY = "dev.droidrt.agent.SET_KEY";
    private static final String TAG = "droidrt-agent";

    @Override
    public void onReceive(Context context, Intent intent) {
        boolean ok = intent != null
                && ACTION_SET_KEY.equals(intent.getAction())
                && AgentKey.store(context, intent.getStringExtra("key"));
        if (ok) {
            Log.i(TAG, "connection key updated");
        } else {
            Log.w(TAG, "rejected SET_KEY broadcast");
        }
        setResultCode(ok ? 1 : 0);
    }
}
