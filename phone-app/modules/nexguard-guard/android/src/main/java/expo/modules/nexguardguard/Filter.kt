package expo.modules.nexguardguard

import org.json.JSONArray
import org.json.JSONObject

/**
 * What the dashboard asked for, as the DNS filter applies it (settings "controls", see
 * main/apps/devices/services.py `_phone_controls`). A name matches a listed site and every
 * name under it: "tiktok.com" covers "www.tiktok.com" and "m.tiktok.com".
 */
class Filter(controls: JSONObject?) {
  val webActivity = controls?.optBoolean("web_activity") == true
  val blockAdult = controls?.optBoolean("block_adult") == true
  private val blocked = names(controls?.optJSONArray("blocked"))
  private val allowed = names(controls?.optJSONArray("allowed"))
  private val safeSearch = HashMap<String, String>() // www.google.com -> forcesafesearch.google.com

  init {
    controls?.optJSONObject("safe_search")?.let { targets ->
      for (target in targets.keys()) {
        val list = targets.optJSONArray(target) ?: continue
        for (i in 0 until list.length()) safeSearch[list.optString(i).lowercase()] = target
      }
    }
  }

  /** Whether the phone needs its filter (the local VPN) at all. */
  val active: Boolean
    get() = webActivity || blockAdult || blocked.isNotEmpty() || safeSearch.isNotEmpty()

  sealed class Decision {
    /** Ask the normal resolver, or the family one when adult sites are blocked. */
    data class Forward(val family: Boolean) : Decision()
    object Block : Decision()
    data class SafeSearch(val target: String) : Decision()
  }

  fun decide(name: String): Decision {
    if (matches(name, allowed)) return Decision.Forward(family = false)
    if (matches(name, blocked)) return Decision.Block
    safeSearch[name]?.let { return Decision.SafeSearch(it) }
    return Decision.Forward(family = blockAdult)
  }

  private fun matches(name: String, set: Set<String>): Boolean {
    var candidate = name
    while (true) {
      if (candidate in set) return true
      val dot = candidate.indexOf('.')
      if (dot < 0) return false
      candidate = candidate.substring(dot + 1)
    }
  }

  companion object {
    private fun names(array: JSONArray?): Set<String> {
      if (array == null) return emptySet()
      return (0 until array.length()).map { array.optString(it).lowercase().trimEnd('.') }
        .filter { it.isNotEmpty() }.toSet()
    }

    // Second-level labels under a country code that are part of the suffix: bbc.co.uk.
    private val SECOND_LEVEL = setOf("co", "com", "net", "org", "ac", "gov", "edu", "or", "ne", "go")

    /** The site a looked-up name belongs to: "m.youtube.com" -> "youtube.com". Close enough
     *  for a list of sites visited without shipping the public suffix list. */
    fun site(name: String): String? {
      val labels = name.lowercase().trimEnd('.').split(".").filter { it.isNotEmpty() }
      if (labels.size < 2 || labels.last().all { it.isDigit() }) return null
      val take = if (labels.size >= 3 && labels.last().length == 2 && labels[labels.size - 2] in SECOND_LEVEL) 3 else 2
      return labels.takeLast(take).joinToString(".")
    }

    /** Browsers, by package: whose lookups count as websites visited (not every app's). */
    val BROWSERS = mapOf(
      "com.android.chrome" to "Chrome",
      "com.chrome.beta" to "Chrome",
      "org.mozilla.firefox" to "Firefox",
      "org.mozilla.focus" to "Firefox Focus",
      "com.microsoft.emmx" to "Edge",
      "com.brave.browser" to "Brave",
      "com.opera.browser" to "Opera",
      "com.opera.mini.native" to "Opera Mini",
      "com.sec.android.app.sbrowser" to "Samsung Internet",
      "com.duckduckgo.mobile.android" to "DuckDuckGo",
      "com.vivaldi.browser" to "Vivaldi",
      "com.kiwibrowser.browser" to "Kiwi",
      "com.UCMobile.intl" to "UC Browser",
      "com.mi.globalbrowser" to "Mi Browser",
      "com.android.browser" to "Browser",
    )
  }
}
