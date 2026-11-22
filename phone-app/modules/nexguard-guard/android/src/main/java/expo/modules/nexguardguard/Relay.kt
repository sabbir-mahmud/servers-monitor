package expo.modules.nexguardguard

import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** The relay refused this phone's login: it was removed on the dashboard. */
class Unpaired : Exception("This phone was removed on the dashboard.")

/**
 * The same Upstash REST calls the JS side makes (src/relay.ts), for when the app isn't
 * running: GET the dashboard's command, RPUSH an immediate tamper notice. Everything is
 * signed with the key the phone made when it paired, and nothing unsigned is obeyed.
 */
class Relay(private val login: JSONObject) {
  private val url = login.getString("url")
  private val token = login.getString("token")
  private val key = hex(login.getString("key"))

  private fun call(vararg command: String): Any? {
    val connection = URL(url).openConnection() as HttpURLConnection
    try {
      connection.requestMethod = "POST"
      connection.connectTimeout = 15_000
      connection.readTimeout = 15_000
      connection.doOutput = true
      connection.setRequestProperty("Authorization", "Bearer $token")
      connection.setRequestProperty("Content-Type", "application/json")
      connection.outputStream.use { it.write(JSONArray(command.toList()).toString().toByteArray()) }
      val status = connection.responseCode
      if (status == 401) throw Unpaired()
      val stream = if (status in 200..299) connection.inputStream else connection.errorStream
      val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
      if (status !in 200..299) throw IllegalStateException("relay answered HTTP $status")
      val data = JSONObject(text)
      if (data.has("error")) throw IllegalStateException(data.optString("error"))
      return if (data.isNull("result")) null else data.get("result")
    } finally {
      connection.disconnect()
    }
  }

  /** A value the dashboard left and signed ({"body","sig"}), as its body; null if absent
   *  or not signed with this phone's key. */
  fun readSigned(redisKey: String): JSONObject? {
    if (redisKey.isEmpty()) return null
    val raw = call("GET", redisKey) as? String ?: return null
    val sealed = runCatching { JSONObject(raw) }.getOrNull() ?: return null
    val body = sealed.optString("body")
    if (!constantTimeEquals(sign(body), sealed.optString("sig"))) return null
    return runCatching { JSONObject(body) }.getOrNull()
  }

  /** {"t":"ci","body":…,"sig":…} with a report that only carries tamper codes. */
  fun pushTamper(codes: List<String>) {
    val report = JSONObject().put("partial", true).put("tamper", JSONArray(codes))
    val body = JSONObject().put("at", System.currentTimeMillis() / 1000).put("report", report).toString()
    val message = JSONObject().put("t", "ci").put("body", body).put("sig", sign(body))
    call("RPUSH", login.getString("inbox"), message.toString())
  }

  fun sign(body: String): String {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(key, "HmacSHA256"))
    return mac.doFinal(body.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
  }

  companion object {
    fun hex(text: String): ByteArray =
      ByteArray(text.length / 2) { i -> text.substring(2 * i, 2 * i + 2).toInt(16).toByte() }

    fun constantTimeEquals(a: String, b: String): Boolean {
      if (a.length != b.length) return false
      var diff = 0
      for (i in a.indices) diff = diff or (a[i].code xor b[i].code)
      return diff == 0
    }
  }
}
