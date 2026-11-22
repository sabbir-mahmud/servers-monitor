package expo.modules.nexguardguard

import java.io.ByteArrayOutputStream

/**
 * Just enough DNS for the website filter: read the question of a query, answer "no such
 * site", and turn an upstream answer for one name (forcesafesearch.google.com) into the
 * answer for another (www.google.com).
 */
object Dns {
  const val TYPE_A = 1
  const val TYPE_AAAA = 28

  class Question(val name: String, val type: Int, val end: Int)

  /** The first question of a query, or null for anything that isn't a plain query. */
  fun question(packet: ByteArray): Question? {
    if (packet.size < 12) return null
    val flags = u16(packet, 2)
    if (flags and 0x8000 != 0 || u16(packet, 4) < 1) return null // a response, or no question
    val labels = mutableListOf<String>()
    var i = 12
    while (true) {
      if (i >= packet.size) return null
      val length = packet[i].toInt() and 0xff
      if (length == 0) {
        i += 1
        break
      }
      if (length and 0xc0 != 0 || i + 1 + length > packet.size) return null // no compression in a question
      labels.add(String(packet, i + 1, length, Charsets.US_ASCII).lowercase())
      i += 1 + length
    }
    if (i + 4 > packet.size) return null
    return Question(labels.joinToString("."), u16(packet, i), i + 4)
  }

  /** NXDOMAIN for the query: the site doesn't exist, as far as this phone knows. */
  fun blocked(query: ByteArray, q: Question): ByteArray {
    val out = query.copyOf(q.end)
    out[2] = (0x80 or (query[2].toInt() and 0x01) or 0x04).toByte() // QR, RD kept, AA
    out[3] = 0x83.toByte() // RA, NXDOMAIN
    for (k in 6 until 12) out[k] = 0 // no answers, authority or additional records
    out[4] = 0; out[5] = 1
    return out
  }

  /** A query for `name` with the same id and type, to ask upstream on another name's behalf. */
  fun queryFor(original: ByteArray, q: Question, name: String): ByteArray {
    val out = ByteArrayOutputStream()
    out.write(original, 0, 4)
    out.write(byteArrayOf(0, 1, 0, 0, 0, 0, 0, 0))
    for (label in name.split(".")) {
      out.write(label.length)
      out.write(label.toByteArray(Charsets.US_ASCII))
    }
    out.write(0)
    out.write(q.type shr 8); out.write(q.type and 0xff)
    out.write(0); out.write(1)
    return out.toByteArray()
  }

  /** The upstream answer's A/AAAA records, re-addressed to the original question. */
  fun rewritten(original: ByteArray, q: Question, upstream: ByteArray): ByteArray? {
    val uq = question(withQueryFlag(upstream)) ?: return null
    var i = uq.end
    val answers = u16(upstream, 6)
    val records = ByteArrayOutputStream()
    var count = 0
    repeat(answers) {
      i = skipName(upstream, i) ?: return null
      if (i + 10 > upstream.size) return null
      val type = u16(upstream, i)
      val rdLength = u16(upstream, i + 8)
      if (i + 10 + rdLength > upstream.size) return null
      if (type == TYPE_A || type == TYPE_AAAA) {
        records.write(0xc0); records.write(12) // the name of the question
        records.write(upstream, i, 10 + rdLength)
        count++
      }
      i += 10 + rdLength
    }
    val out = ByteArrayOutputStream()
    out.write(original, 0, 2)
    out.write(0x81 or (original[2].toInt() and 0x01)); out.write(0x80)
    out.write(byteArrayOf(0, 1, (count shr 8).toByte(), count.toByte(), 0, 0, 0, 0))
    out.write(original, 12, q.end - 12)
    records.writeTo(out)
    return out.toByteArray()
  }

  private fun withQueryFlag(packet: ByteArray): ByteArray =
    packet.copyOf().also { it[2] = (it[2].toInt() and 0x7f).toByte() }

  private fun skipName(packet: ByteArray, start: Int): Int? {
    var i = start
    while (i < packet.size) {
      val length = packet[i].toInt() and 0xff
      when {
        length == 0 -> return i + 1
        length and 0xc0 == 0xc0 -> return i + 2
        else -> i += 1 + length
      }
    }
    return null
  }

  fun u16(packet: ByteArray, at: Int): Int =
    ((packet[at].toInt() and 0xff) shl 8) or (packet[at + 1].toInt() and 0xff)
}
