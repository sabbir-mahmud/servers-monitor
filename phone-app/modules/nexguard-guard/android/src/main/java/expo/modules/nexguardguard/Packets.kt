package expo.modules.nexguardguard

import java.net.InetAddress

/**
 * IPv4/IPv6 + UDP, for the DNS packets the filter's tunnel carries (it routes nothing but
 * its own DNS server address, so that's all that arrives).
 */
class UdpPacket(
  val source: InetAddress,
  val destination: InetAddress,
  val sourcePort: Int,
  val destinationPort: Int,
  val payload: ByteArray,
) {
  /** The reply to this packet: addresses and ports swapped, with `data` as payload. */
  fun reply(data: ByteArray): ByteArray =
    if (source.address.size == 4) ipv4(destination, source, destinationPort, sourcePort, data)
    else ipv6(destination, source, destinationPort, sourcePort, data)

  companion object {
    private const val UDP = 17

    fun parse(packet: ByteArray, length: Int): UdpPacket? {
      if (length < 1) return null
      return when ((packet[0].toInt() and 0xf0) shr 4) {
        4 -> {
          val headerLength = (packet[0].toInt() and 0x0f) * 4
          if (length < headerLength + 8 || packet[9].toInt() != UDP) return null
          udp(packet, length, headerLength, packet.copyOfRange(12, 16), packet.copyOfRange(16, 20))
        }
        6 -> {
          if (length < 48 || packet[6].toInt() != UDP) return null // no extension headers expected
          udp(packet, length, 40, packet.copyOfRange(8, 24), packet.copyOfRange(24, 40))
        }
        else -> null
      }
    }

    private fun udp(p: ByteArray, length: Int, at: Int, src: ByteArray, dst: ByteArray): UdpPacket? {
      val udpLength = Dns.u16(p, at + 4)
      if (udpLength < 8 || at + udpLength > length) return null
      return UdpPacket(
        InetAddress.getByAddress(src), InetAddress.getByAddress(dst),
        Dns.u16(p, at), Dns.u16(p, at + 2), p.copyOfRange(at + 8, at + udpLength),
      )
    }

    private fun udpHeader(srcPort: Int, dstPort: Int, data: ByteArray): ByteArray {
      val length = 8 + data.size
      return byteArrayOf(
        (srcPort shr 8).toByte(), srcPort.toByte(), (dstPort shr 8).toByte(), dstPort.toByte(),
        (length shr 8).toByte(), length.toByte(), 0, 0,
      ) + data
    }

    private fun ipv4(src: InetAddress, dst: InetAddress, srcPort: Int, dstPort: Int, data: ByteArray): ByteArray {
      val udp = udpHeader(srcPort, dstPort, data) // a zero UDP checksum is allowed over IPv4
      val total = 20 + udp.size
      val header = byteArrayOf(
        0x45, 0, (total shr 8).toByte(), total.toByte(), 0, 0, 0x40, 0, 64, UDP.toByte(), 0, 0,
      ) + src.address + dst.address
      val sum = checksum(header, 0, 0)
      header[10] = (sum shr 8).toByte(); header[11] = sum.toByte()
      return header + udp
    }

    private fun ipv6(src: InetAddress, dst: InetAddress, srcPort: Int, dstPort: Int, data: ByteArray): ByteArray {
      val udp = udpHeader(srcPort, dstPort, data)
      // IPv6 requires the UDP checksum, over a pseudo-header of the addresses and length.
      val pseudo = src.address + dst.address +
        byteArrayOf(0, 0, (udp.size shr 8).toByte(), udp.size.toByte(), 0, 0, 0, UDP.toByte())
      var sum = checksum(pseudo + udp, 0, 0)
      if (sum == 0) sum = 0xffff
      udp[6] = (sum shr 8).toByte(); udp[7] = sum.toByte()
      val header = byteArrayOf(0x60, 0, 0, 0, (udp.size shr 8).toByte(), udp.size.toByte(), UDP.toByte(), 64) +
        src.address + dst.address
      return header + udp
    }

    private fun checksum(data: ByteArray, start: Int, initial: Int): Int {
      var sum = initial.toLong()
      var i = start
      while (i + 1 < data.size) {
        sum += ((data[i].toInt() and 0xff) shl 8) or (data[i + 1].toInt() and 0xff)
        i += 2
      }
      if (i < data.size) sum += (data[i].toInt() and 0xff) shl 8
      while (sum shr 16 != 0L) sum = (sum and 0xffff) + (sum shr 16)
      return (sum.inv() and 0xffff).toInt()
    }
  }
}
