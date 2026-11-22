package expo.modules.nexguardguard

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * What the native side keeps between runs (it must work after a reboot without the app):
 * the relay login and signing key, encrypted with a key that never leaves the Android
 * Keystore, and the latest settings from the dashboard. Private to this app.
 */
class Store(context: Context) {
  private val prefs: SharedPreferences =
    context.applicationContext.getSharedPreferences("nexguard_guard", Context.MODE_PRIVATE)

  /** The pairing the JS side handed over: url, token, inbox, command, key. */
  var login: JSONObject?
    get() = secret("login")?.let { runCatching { JSONObject(it) }.getOrNull() }
    set(value) = putSecret("login", value?.toString())

  /** The "controls" object of the dashboard's signed settings; null: none. */
  var controls: JSONObject?
    get() = prefs.getString("controls", null)?.let { runCatching { JSONObject(it) }.getOrNull() }
    set(value) = prefs.edit().putString("controls", value?.toString()).apply()

  var lastCommand: String
    get() = prefs.getString("last_command", "") ?: ""
    set(value) = prefs.edit().putString("last_command", value).apply()

  fun getString(name: String): String? = prefs.getString(name, null)

  fun putString(name: String, value: String?) = prefs.edit().putString(name, value).apply()

  fun getBoolean(name: String): Boolean = prefs.getBoolean(name, false)

  fun putBoolean(name: String, value: Boolean) = prefs.edit().putBoolean(name, value).apply()

  fun clear() = prefs.edit().clear().apply()

  private fun secret(name: String): String? {
    val stored = prefs.getString("enc_$name", null) ?: return null
    return runCatching {
      val bytes = Base64.decode(stored, Base64.NO_WRAP)
      val cipher = Cipher.getInstance("AES/GCM/NoPadding")
      cipher.init(Cipher.DECRYPT_MODE, keystoreKey(), GCMParameterSpec(128, bytes, 0, 12))
      String(cipher.doFinal(bytes, 12, bytes.size - 12), Charsets.UTF_8)
    }.getOrNull()
  }

  private fun putSecret(name: String, value: String?) {
    if (value == null) {
      prefs.edit().remove("enc_$name").apply()
      return
    }
    val cipher = Cipher.getInstance("AES/GCM/NoPadding")
    cipher.init(Cipher.ENCRYPT_MODE, keystoreKey())
    val sealed = cipher.iv + cipher.doFinal(value.toByteArray(Charsets.UTF_8))
    prefs.edit().putString("enc_$name", Base64.encodeToString(sealed, Base64.NO_WRAP)).apply()
  }

  private fun keystoreKey(): SecretKey {
    val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
    (keyStore.getKey(ALIAS, null) as? SecretKey)?.let { return it }
    val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
    generator.init(
      KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
        .build()
    )
    return generator.generateKey()
  }

  companion object {
    private const val ALIAS = "nexguard.guard"
  }
}
