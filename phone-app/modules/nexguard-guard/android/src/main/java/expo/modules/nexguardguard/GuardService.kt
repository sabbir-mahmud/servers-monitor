package expo.modules.nexguardguard

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import android.provider.Settings
import android.system.OsConstants
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import java.io.FileInputStream
import java.io.FileOutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/**
 * NexGuard's always-on part, a foreground service with a notification the person always
 * sees. Every 30 s it reads the dashboard's command (beep) from the relay and checks that
 * its protections are still on, telling the dashboard at once when one is turned off.
 * When the dashboard turned on websites or blocking, it is also the website filter: a
 * local VPN that routes only its own DNS server address, so it sees name lookups and
 * nothing else. Blocked names get "no such site"; the rest are asked of Cloudflare
 * (1.1.1.3, its family filter, when adult sites are blocked; 1.1.1.1 otherwise).
 */
class GuardService : VpnService() {
  private var tun: ParcelFileDescriptor? = null
  private var reader: Thread? = null
  private var filter = Filter(null)
  private val lookups = Executors.newFixedThreadPool(4)
  private var ticker: ScheduledExecutorService? = null
  private lateinit var store: Store

  override fun onCreate() {
    super.onCreate()
    store = Store(this)
  }

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    if (intent?.action == ACTION_STOP || store.login == null) {
      shutDown()
      return START_NOT_STICKY
    }
    goForeground()
    configure()
    if (ticker == null) {
      ticker = Executors.newSingleThreadScheduledExecutor().also {
        it.scheduleWithFixedDelay({ runCatching { tick() }.onFailure { e -> Log.w(TAG, "tick", e) } },
          2, TICK_SECONDS, TimeUnit.SECONDS)
      }
    }
    running = true
    return START_STICKY
  }

  override fun onRevoke() {
    // Another VPN took over, or the person turned NexGuard's off in Settings.
    closeTun()
    if (filter.active) Thread { tamper(this, "vpn_off") }.start()
    updateNotification()
  }

  override fun onDestroy() {
    running = false
    ticker?.shutdownNow()
    ticker = null
    closeTun()
    lookups.shutdownNow()
    super.onDestroy()
  }

  private fun shutDown() {
    closeTun()
    ticker?.shutdownNow()
    ticker = null
    running = false
    stopForeground(STOP_FOREGROUND_REMOVE)
    stopSelf()
  }

  // ------------------------------------------------------------------------- filter

  /** Bring the filter in line with the latest settings: on, off, or new lists. */
  @Synchronized
  private fun configure() {
    filter = Filter(store.controls)
    if (filter.active && VpnService.prepare(this) == null) {
      if (tun == null) establish()
    } else {
      closeTun()
    }
    updateNotification()
  }

  private fun establish() {
    val builder = Builder()
      .setSession("NexGuard website filter")
      .addAddress(TUN_V4, 32)
      .addDnsServer(DNS_V4)
      .addRoute(DNS_V4, 32)
      .setBlocking(true)
    runCatching { builder.addAddress(TUN_V6, 128).addDnsServer(DNS_V6).addRoute(DNS_V6, 128) }
    runCatching { builder.addDisallowedApplication(packageName) } // its own relay calls go direct
    val fd = runCatching { builder.establish() }.getOrNull() ?: return
    tun = fd
    reader = Thread({ readLoop(fd) }, "nexguard-dns").also { it.start() }
  }

  @Synchronized
  private fun closeTun() {
    runCatching { tun?.close() }
    tun = null
    reader?.interrupt()
    reader = null
  }

  private fun readLoop(fd: ParcelFileDescriptor) {
    val input = FileInputStream(fd.fileDescriptor)
    val output = FileOutputStream(fd.fileDescriptor)
    val buffer = ByteArray(32767)
    while (!Thread.currentThread().isInterrupted) {
      val length = try {
        input.read(buffer)
      } catch (e: Exception) {
        break // the tunnel was closed
      }
      if (length <= 0) continue
      val packet = UdpPacket.parse(buffer, length) ?: continue
      if (packet.destinationPort != 53) continue
      lookups.execute {
        val answer = runCatching { answer(packet) }.getOrNull() ?: return@execute
        synchronized(output) { runCatching { output.write(packet.reply(answer)) } }
      }
    }
  }

  private fun answer(packet: UdpPacket): ByteArray? {
    val query = packet.payload
    val q = Dns.question(query) ?: return forward(query, filter.blockAdult)
    val rules = filter
    if (rules.webActivity && (q.type == Dns.TYPE_A || q.type == Dns.TYPE_AAAA)) {
      browserOf(packet)?.let { Activity.recordLookup(this, it, q.name) }
    }
    return when (val decision = rules.decide(q.name)) {
      is Filter.Decision.Block -> Dns.blocked(query, q)
      is Filter.Decision.SafeSearch ->
        forward(Dns.queryFor(query, q, decision.target), false)?.let { Dns.rewritten(query, q, it) }
          ?: Dns.blocked(query, q)
      is Filter.Decision.Forward -> forward(query, decision.family)
    }
  }

  /** Which browser asked (Android 10+); null for other apps, whose lookups aren't sites. */
  private fun browserOf(packet: UdpPacket): String? {
    if (Build.VERSION.SDK_INT < 29) return null
    val cm = getSystemService(ConnectivityManager::class.java) ?: return null
    val uid = runCatching {
      cm.getConnectionOwnerUid(OsConstants.IPPROTO_UDP,
        InetSocketAddress(packet.source, packet.sourcePort),
        InetSocketAddress(packet.destination, packet.destinationPort))
    }.getOrDefault(-1)
    if (uid < 0) return null
    val packages = packageManager.getPackagesForUid(uid) ?: return null
    return packages.firstNotNullOfOrNull { Filter.BROWSERS[it] }
  }

  private fun forward(query: ByteArray, family: Boolean): ByteArray? {
    val servers = if (family) FAMILY else PLAIN
    for (server in servers) {
      val answer = runCatching {
        DatagramSocket().use { socket ->
          protect(socket) // straight out, not back into this tunnel
          socket.soTimeout = 4000
          socket.send(DatagramPacket(query, query.size, InetAddress.getByName(server), 53))
          val buffer = ByteArray(4096)
          val reply = DatagramPacket(buffer, buffer.size)
          socket.receive(reply)
          buffer.copyOf(reply.length)
        }
      }.getOrNull()
      if (answer != null) return answer
    }
    return null
  }

  // ------------------------------------------------------------------ every 30 seconds

  private fun tick() {
    val login = store.login ?: return shutDown()
    val relay = Relay(login)
    try {
      val command = relay.readSigned(login.optString("command"))
      val id = command?.optString("id").orEmpty()
      val fresh = command != null && System.currentTimeMillis() / 1000 - command.optLong("at") < 600
      if (fresh && id.isNotEmpty() && id != store.lastCommand) {
        store.lastCommand = id
        if (command!!.optString("kind") == "beep") Beeper.start(this)
      }
    } catch (e: Unpaired) {
      unpaired(this)
      return
    } catch (e: Exception) {
      Log.i(TAG, "command check: ${e.message}")
    }
    checkProtections()
    configure() // the app may have saved new settings
  }

  /** Tell the dashboard the moment a protection goes from on to off. */
  private fun checkProtections() {
    val dpm = getSystemService(DevicePolicyManager::class.java)
    val now = mapOf(
      "usage_access_off" to Activity.hasUsageAccess(this),
      "notifications_off" to NotificationManagerCompat.from(this).areNotificationsEnabled(),
      "private_dns" to (Settings.Global.getString(contentResolver, "private_dns_mode") != "hostname"),
      "admin_off" to (dpm?.isAdminActive(adminComponent(this)) == true),
    )
    for ((code, ok) in now) {
      val was = store.getString("ok_$code")
      // Removal protection tells by itself (AdminReceiver), the moment it's turned off.
      if (was == "true" && !ok && code != "admin_off") Thread { tamper(this, code) }.start()
      store.putString("ok_$code", ok.toString())
    }
  }

  // ----------------------------------------------------------------- notification

  private fun goForeground() {
    val notification = notification()
    if (Build.VERSION.SDK_INT >= 34) {
      startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
    } else {
      startForeground(NOTIFICATION, notification)
    }
  }

  private fun updateNotification() {
    runCatching { NotificationManagerCompat.from(this).notify(NOTIFICATION, notification()) }
  }

  private fun notification(): Notification {
    val manager = getSystemService(NotificationManager::class.java)
    if (Build.VERSION.SDK_INT >= 26) {
      manager.createNotificationChannel(NotificationChannel(CHANNEL, "NexGuard is on", NotificationManager.IMPORTANCE_LOW))
    }
    val open = packageManager.getLaunchIntentForPackage(packageName)?.let {
      PendingIntent.getActivity(this, 0, it, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }
    val text = if (tun != null) "Filtering websites, as set on the dashboard" else "Sharing this phone with the account that set it up"
    return NotificationCompat.Builder(this, CHANNEL)
      .setSmallIcon(android.R.drawable.ic_lock_lock)
      .setContentTitle("NexGuard is on")
      .setContentText(text)
      .setOngoing(true)
      .setPriority(NotificationCompat.PRIORITY_LOW)
      .apply { if (open != null) setContentIntent(open) }
      .build()
  }

  companion object {
    private const val TAG = "NexGuard"
    private const val ACTION_STOP = "io.nexguard.STOP"
    private const val CHANNEL = "nexguard.guard"
    private const val NOTIFICATION = 7301
    private const val TICK_SECONDS = 30L
    private const val TUN_V4 = "10.111.222.1"
    private const val DNS_V4 = "10.111.222.2"
    private const val TUN_V6 = "fd00:6e67::1"
    private const val DNS_V6 = "fd00:6e67::2"
    private val PLAIN = listOf("1.1.1.1", "1.0.0.1")
    private val FAMILY = listOf("1.1.1.3", "1.0.0.3")

    @Volatile var running = false

    fun adminComponent(context: Context) = ComponentName(context, AdminReceiver::class.java)

    fun start(context: Context) {
      if (Store(context).login == null) return
      runCatching { ContextCompat.startForegroundService(context, Intent(context, GuardService::class.java)) }
        .onFailure { Log.w(TAG, "couldn't start", it) }
    }

    fun stop(context: Context) {
      runCatching {
        context.startService(Intent(context, GuardService::class.java).setAction(ACTION_STOP))
      }
    }

    /** Push a tamper notice now; if the relay can't be reached, the next check-in carries
     *  it instead. Call off the main thread. */
    fun tamper(context: Context, code: String) {
      val store = Store(context)
      val login = store.login ?: return
      try {
        Relay(login).pushTamper(listOf(code))
      } catch (e: Exception) {
        val pending = runCatching { JSONArray(store.getString("tamper_pending") ?: "[]") }.getOrDefault(JSONArray())
        pending.put(code)
        store.putString("tamper_pending", pending.toString())
      }
    }

    /** Removed on the dashboard: let go of everything, including removal protection, so
     *  the phone can be uninstalled. */
    fun unpaired(context: Context) {
      val store = Store(context)
      store.putBoolean("releasing", true)
      runCatching {
        context.getSystemService(DevicePolicyManager::class.java)?.removeActiveAdmin(adminComponent(context))
      }
      Activity.reset(context)
      store.clear()
      store.putBoolean("unpaired", true)
      stop(context)
    }
  }
}
