package expo.modules.nexguardguard

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import androidx.core.app.NotificationCompat

/** "Beep" from the dashboard: ring loudly for 30 s on the alarm stream (which plays even
 *  when the phone is on silent), vibrate, and show a notification with Stop. */
object Beeper {
  private const val CHANNEL = "nexguard.beep"
  private const val NOTIFICATION = 7302
  private const val SECONDS = 30
  private var player: MediaPlayer? = null
  private var savedVolume = -1
  private val main = Handler(Looper.getMainLooper())

  fun start(context: Context) = main.post {
    val app = context.applicationContext
    stop(app)
    val audio = app.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    savedVolume = audio.getStreamVolume(AudioManager.STREAM_ALARM)
    runCatching { audio.setStreamVolume(AudioManager.STREAM_ALARM, audio.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0) }
    val tone = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
      ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
    player = runCatching {
      MediaPlayer().apply {
        setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
          .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
        setDataSource(app, tone)
        isLooping = true
        prepare()
        start()
      }
    }.getOrNull()
    vibrator(app)?.let { v ->
      val pattern = longArrayOf(0, 800, 400)
      if (Build.VERSION.SDK_INT >= 26) {
        v.vibrate(VibrationEffect.createWaveform(pattern, 0))
      } else {
        @Suppress("DEPRECATION")
        v.vibrate(pattern, 0)
      }
    }
    notify(app)
    main.postDelayed({ stop(app) }, SECONDS * 1000L)
  }

  fun stop(context: Context) {
    main.removeCallbacksAndMessages(null)
    player?.runCatching { stop(); release() }
    player = null
    vibrator(context)?.cancel()
    if (savedVolume >= 0) {
      val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
      runCatching { audio.setStreamVolume(AudioManager.STREAM_ALARM, savedVolume, 0) }
      savedVolume = -1
    }
    (context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancel(NOTIFICATION)
  }

  private fun notify(context: Context) {
    val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    if (Build.VERSION.SDK_INT >= 26) {
      manager.createNotificationChannel(NotificationChannel(CHANNEL, "Find my phone", NotificationManager.IMPORTANCE_HIGH))
    }
    val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
    val stop = PendingIntent.getBroadcast(context, 0, Intent(context, BeepStopReceiver::class.java), flags)
    val open = context.packageManager.getLaunchIntentForPackage(context.packageName)
      ?.let { PendingIntent.getActivity(context, 0, it, flags) }
    val notification = NotificationCompat.Builder(context, CHANNEL)
      .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
      .setContentTitle("Ringing so you can find this phone")
      .setContentText("Sent from NexGuard's dashboard. Tap Stop to silence it.")
      .setPriority(NotificationCompat.PRIORITY_MAX)
      .setCategory(NotificationCompat.CATEGORY_ALARM)
      .setOngoing(true)
      .addAction(0, "Stop", stop)
      .setDeleteIntent(stop)
      .apply { if (open != null) setContentIntent(open).setFullScreenIntent(open, true) }
      .build()
    runCatching { manager.notify(NOTIFICATION, notification) }
  }

  @Suppress("DEPRECATION")
  private fun vibrator(context: Context): Vibrator? = context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
}

class BeepStopReceiver : BroadcastReceiver() {
  override fun onReceive(context: Context, intent: Intent) = Beeper.stop(context.applicationContext)
}
