package com.powerk.app

/** Synthetic fixture only; no API or live LAN address is used by visual tests. */
fun visualSnapshot(english: Boolean = false): Snapshot {
    val names = if (english) listOf("Fridge", "Heater", "Television", "Phone charger")
        else listOf("التلاجة", "السخان", "التلفزيون", "شاحن الموبايل")
    val outlets = names.mapIndexed { i, name -> Outlet(i + 1, i == 0 || i == 3,
        when (i) { 0 -> 86.4; 3 -> 4.2; else -> 0.0 }, 0.0, 30 + i, name) }
    return Snapshot("127.0.0.1", listOf(Strip("AA:BB:CC:DD:EE:FF", if (english) "Power strip" else "المشترك",
        "MTTL-W01", "0.1.54-1.0.66", true, true, 90.6, 12.4, 223.4, 0.41, -56, outlets,
        todayKwh = 1.42, monthKwh = 12.4, monthCost = 18.6, ip = "192.0.2.7")), "EGP", 1.5, 100.0, 12.4, 18.6)
}
