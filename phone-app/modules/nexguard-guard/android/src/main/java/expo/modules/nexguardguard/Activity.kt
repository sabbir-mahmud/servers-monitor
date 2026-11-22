package expo.modules.nexguardguard

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Process
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Locale
import java.util.UUID

/**
 * Screen time and websites, as batches for the check-in (`report.activity`, the same shape
 * the computer agent sends: main/apps/devices/activity.py). The dashboard adds each batch
 * to what it has, so a batch holds only what's new since the last one it received, and is
 * kept (same id) until a check-in carrying it was pushed: a failed push can't lose or
 * double-count anything.
 */
object Activity {
  private const val USER = "Phone"
  private const val RESUMED = 1 // UsageEvents.Event.ACTIVITY_RESUMED (MOVE_TO_FOREGROUND before 29)
  private const val PAUSED = 2 // UsageEvents.Event.ACTIVITY_PAUSED
  private const val SCREEN_OFF = 16 // UsageEvents.Event.SCREEN_NON_INTERACTIVE (API 28)
  private const val SHUTDOWN = 26 // UsageEvents.Event.DEVICE_SHUTDOWN (API 28) // the dashboard's "user" column: a phone has one
  private val lock = Any()
  private val lastLookup = HashMap<String, Long>() // "browser|site" -> ms, in memory only

  // ------------------------------------------------------------------ websites (filter)

  /** A browser looked `name` up: one visit per site per browser per minute of use. */
  fun recordLookup(context: Context, browser: String, name: String) {
    val site = Filter.site(name) ?: return
    val now = System.currentTimeMillis()
    val key = "$browser|$site"
    synchronized(lock) {
      val previous = lastLookup[key]
      if (previous != null && now - previous < 60_000) return
      lastLookup[key] = now
      val store = Store(context)
      val web = json(store.getString("web_counts"))
      val row = web.optJSONObject("${day(now)}|$key") ?: JSONObject()
      row.put("visits", row.optInt("visits") + 1)
      // Time on a site isn't in DNS: each minute with a lookup counts as a minute there.
      row.put("seconds", row.optInt("seconds") + 60)
      web.put("${day(now)}|$key", row)
      store.putString("web_counts", web.toString())
    }
  }

  // --------------------------------------------------------------------- the batches

  /** The batch to send with this check-in, or null when there's nothing new. */
  fun take(context: Context, screenTime: Boolean, webActivity: Boolean): JSONObject? {
    synchronized(lock) {
      val store = Store(context)
      store.getString("pending")?.let { return JSONObject(it) }
      val screen = JSONArray()
      if (screenTime && hasUsageAccess(context)) {
        val sent = json(store.getString("screen_sent"))
        for ((day, apps) in usageByDay(context)) {
          val already = sent.optJSONObject(day) ?: JSONObject()
          for ((app, seconds) in apps) {
            val delta = seconds - already.optLong(app)
            if (delta >= 60) {
              screen.put(JSONObject().put("day", day).put("user", USER).put("app", app).put("seconds", delta))
            }
          }
        }
      }
      val web = JSONArray()
      if (webActivity) {
        val counts = json(store.getString("web_counts"))
        for (key in counts.keys()) {
          val (day, browser, site) = key.split("|", limit = 3).takeIf { it.size == 3 } ?: continue
          val row = counts.getJSONObject(key)
          web.put(JSONObject().put("day", day).put("user", USER).put("browser", browser)
            .put("domain", site).put("visits", row.optInt("visits")).put("seconds", row.optInt("seconds")))
        }
      }
      store.putString("web_counts", null) // they're in the batch now
      if (screen.length() == 0 && web.length() == 0) return null
      val batch = JSONObject().put("batch", UUID.randomUUID().toString()).put("screen", screen).put("web", web)
      store.putString("pending", batch.toString())
      return batch
    }
  }

  /** The check-in carrying `batch` was pushed: count its screen time as sent. */
  fun commit(context: Context, batch: String) {
    synchronized(lock) {
      val store = Store(context)
      val pending = store.getString("pending")?.let { JSONObject(it) } ?: return
      if (pending.optString("batch") != batch) return
      val sent = json(store.getString("screen_sent"))
      val rows = pending.optJSONArray("screen") ?: JSONArray()
      for (i in 0 until rows.length()) {
        val row = rows.getJSONObject(i)
        val day = sent.optJSONObject(row.getString("day")) ?: JSONObject()
        day.put(row.getString("app"), day.optLong(row.getString("app")) + row.getLong("seconds"))
        sent.put(row.getString("day"), day)
      }
      // Only the last few days matter: older ones can't change any more.
      val keep = (0..2).map { day(System.currentTimeMillis() - it * 86_400_000L) }.toSet()
      for (old in sent.keys().asSequence().toList()) if (old !in keep) sent.remove(old)
      store.putString("screen_sent", sent.toString())
      store.putString("pending", null)
    }
  }

  fun reset(context: Context) {
    synchronized(lock) {
      val store = Store(context)
      for (name in listOf("pending", "screen_sent", "web_counts")) store.putString(name, null)
      lastLookup.clear()
    }
  }

  // ------------------------------------------------------------------- screen time

  fun hasUsageAccess(context: Context): Boolean {
    val ops = context.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
    @Suppress("DEPRECATION")
    val mode = if (Build.VERSION.SDK_INT >= 29) {
      ops.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
    } else {
      ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
    }
    return mode == AppOpsManager.MODE_ALLOWED
  }

  /** Seconds in front, per app label, for today and yesterday. */
  private fun usageByDay(context: Context): Map<String, Map<String, Long>> {
    val manager = context.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager
    val ignored = ignoredPackages(context)
    val out = HashMap<String, HashMap<String, Long>>()
    val now = System.currentTimeMillis()
    for (back in 1 downTo 0) {
      val start = startOfDay(now - back * 86_400_000L)
      val end = if (back == 0) now else startOfDay(now)
      val totals = HashMap<String, Long>()
      val events = manager.queryEvents(start, end)
      val event = UsageEvents.Event()
      var current: String? = null
      var since = 0L
      fun close(at: Long) {
        current?.let { pkg -> totals[pkg] = (totals[pkg] ?: 0L) + (at - since).coerceAtLeast(0) }
        current = null
      }
      while (events.hasNextEvent()) {
        events.getNextEvent(event)
        when (event.eventType) {
          RESUMED -> {
            if (event.packageName != current) {
              close(event.timeStamp)
              current = event.packageName
              since = event.timeStamp
            }
          }
          PAUSED -> if (event.packageName == current) close(event.timeStamp)
          SCREEN_OFF, SHUTDOWN -> close(event.timeStamp)
        }
      }
      close(end)
      val labelled = HashMap<String, Long>()
      for ((pkg, ms) in totals) {
        if (pkg in ignored || ms < 1000) continue
        val label = label(context, pkg)
        labelled[label] = (labelled[label] ?: 0L) + ms / 1000
      }
      out[day(start)] = labelled
    }
    return out
  }

  private fun ignoredPackages(context: Context): Set<String> {
    val home = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME)
    @Suppress("DEPRECATION")
    val launchers = context.packageManager.queryIntentActivities(home, PackageManager.MATCH_DEFAULT_ONLY)
      .map { it.activityInfo.packageName }
    return launchers.toSet() + setOf(context.packageName, "com.android.systemui")
  }

  private fun label(context: Context, pkg: String): String = runCatching {
    val pm = context.packageManager
    @Suppress("DEPRECATION")
    pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
  }.getOrDefault(pkg).take(100)

  private fun startOfDay(ms: Long): Long = Calendar.getInstance().apply {
    timeInMillis = ms
    set(Calendar.HOUR_OF_DAY, 0); set(Calendar.MINUTE, 0); set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0)
  }.timeInMillis

  fun day(ms: Long): String = SimpleDateFormat("yyyy-MM-dd", Locale.US).format(ms)

  private fun json(text: String?): JSONObject = text?.let { runCatching { JSONObject(it) }.getOrNull() } ?: JSONObject()
}
