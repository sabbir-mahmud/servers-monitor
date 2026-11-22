package expo.modules.nexguardguard

import android.app.admin.DeviceAdminReceiver
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Removal protection. While NexGuard is a device admin, Android won't uninstall it; turning
 * that off (Settings → Security → Device admin apps) is allowed, but the account that set
 * NexGuard up is told at once.
 */
class AdminReceiver : DeviceAdminReceiver() {
  override fun onDisableRequested(context: Context, intent: Intent): CharSequence =
    "Turning this off lets NexGuard be uninstalled. The account that set up this phone " +
      "will be told straight away."

  override fun onDisabled(context: Context, intent: Intent) {
    val app = context.applicationContext
    if (Store(app).getBoolean("releasing")) return // the dashboard removed it: not tampering
    val pending = goAsync()
    Thread {
      try {
        GuardService.tamper(app, "admin_off")
      } finally {
        pending.finish()
      }
    }.start()
  }
}

/** After a reboot or an app update: back on, if the phone is paired. */
class BootReceiver : BroadcastReceiver() {
  override fun onReceive(context: Context, intent: Intent) {
    if (intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {
      GuardService.start(context.applicationContext)
    }
  }
}
