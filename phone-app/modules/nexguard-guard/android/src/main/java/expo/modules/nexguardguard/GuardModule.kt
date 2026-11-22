package expo.modules.nexguardguard

import android.app.admin.DevicePolicyManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.net.VpnService
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.core.app.NotificationManagerCompat
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import org.json.JSONArray
import org.json.JSONObject

/**
 * What the app (modules/nexguard-guard/index.ts) asks of the native side: where each
 * permission stands, open the screen that grants it, hand over the pairing and settings,
 * and collect activity and tamper notices for the check-in.
 */
class GuardModule : Module() {
  private val context: Context
    get() = requireNotNull(appContext.reactContext).applicationContext

  override fun definition() = ModuleDefinition {
    Name("NexguardGuard")

    Function("status") {
      val ctx = context
      val dpm = ctx.getSystemService(DevicePolicyManager::class.java)
      val power = ctx.getSystemService(PowerManager::class.java)
      mapOf(
        "usage" to Activity.hasUsageAccess(ctx),
        "vpn" to (VpnService.prepare(ctx) == null),
        "notifications" to NotificationManagerCompat.from(ctx).areNotificationsEnabled(),
        "admin" to (dpm?.isAdminActive(GuardService.adminComponent(ctx)) == true),
        "battery" to (power?.isIgnoringBatteryOptimizations(ctx.packageName) == true),
        "privateDns" to (Settings.Global.getString(ctx.contentResolver, "private_dns_mode") == "hostname"),
        "running" to GuardService.running,
        "websites" to (Build.VERSION.SDK_INT >= 29),
        "sdk" to Build.VERSION.SDK_INT,
      )
    }

    /** Open the system screen that grants `name`; the app re-reads status() when it's back. */
    Function("open") { name: String ->
      val ctx = context
      val activity = appContext.currentActivity
      val intent: Intent? = when (name) {
        "usage" -> Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)
        "vpn" -> VpnService.prepare(ctx)
        "admin" -> Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN)
          .putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, GuardService.adminComponent(ctx))
          .putExtra(DevicePolicyManager.EXTRA_ADD_EXPLANATION,
            "Stops NexGuard being uninstalled without the account that set up this phone knowing.")
        "battery" -> Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${ctx.packageName}"))
        "notifications" -> Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
          .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName)
        "always_on" -> Intent(Settings.ACTION_VPN_SETTINGS)
        "network" -> Intent(Settings.ACTION_WIRELESS_SETTINGS) // Private DNS lives here
        else -> null
      }
      if (intent == null) return@Function false
      if (activity != null) {
        if (name == "vpn" || name == "admin") activity.startActivityForResult(intent, 7300)
        else activity.startActivity(intent)
      } else {
        ctx.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
      }
      true
    }

    /** Paired: keep the login and key (encrypted) and start the always-on service. */
    Function("start") { login: String ->
      val ctx = context
      val store = Store(ctx)
      store.login = JSONObject(login)
      store.putBoolean("releasing", false)
      store.putBoolean("unpaired", false)
      GuardService.start(ctx)
    }

    /** The dashboard's signed settings changed ("controls" JSON, or "null"). */
    Function("update") { controls: String ->
      val ctx = context
      Store(ctx).controls = if (controls == "null") null else JSONObject(controls)
      GuardService.start(ctx) // applies them now (a no-op start when it's running)
    }

    /** Unpaired, or removed on the dashboard: stop, forget, release removal protection. */
    Function("stop") {
      GuardService.unpaired(context)
    }

    /** Whether the service found it was removed on the dashboard while the app was closed. */
    Function("wasUnpaired") { Store(context).getBoolean("unpaired") }

    Function("takeActivity") { screenTime: Boolean, webActivity: Boolean ->
      Activity.take(context, screenTime, webActivity)?.toString()
    }

    Function("commitActivity") { batch: String -> Activity.commit(context, batch) }

    /** Tamper notices the service couldn't push itself; cleared once read. */
    Function("takeTamper") {
      val store = Store(context)
      val pending = runCatching { JSONArray(store.getString("tamper_pending") ?: "[]") }.getOrDefault(JSONArray())
      store.putString("tamper_pending", null)
      (0 until pending.length()).map { pending.optString(it) }.distinct()
    }

    Function("stopBeep") { Beeper.stop(context) }
  }
}
