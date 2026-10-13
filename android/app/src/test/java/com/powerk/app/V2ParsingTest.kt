package com.powerk.app

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class V2ParsingTest {
    @Test fun extendedStatePreservesNamesAlarmsTotalsAndLongTimes() {
        val s = Api.parseSnapshot(JSONObject("""{
          "api_version":2,"server_time":4102444800,"utc_offset_seconds":10800,
          "local_ip":"192.168.1.2","port":10086,"currency":"EGP","price_kwh":2.14,
          "budget":500,"month_kwh":12.3,"month_cost":26.32,"unread_alerts":2,
          "devices":[{"mac":"ABCDEF123456","name":"Default","label":"Desk","online":true,
          "ip":"192.168.1.3","reported_at":4102444799,"today_kwh":0.42,"month_kwh":12.3,"month_cost":26.32,
          "alarms":["temp_high","new_alarm"],"voltage":221.7,"current_a":0.28,"rssi":-56,
          "outlets":[{"n":4,"name":"Fridge","on":true,"power_w":61.2,"energy_kwh":3.2,
          "temp_c":61,"alarms":["temp_high"]}]}],"future_field":true} """))
        assertEquals(2, s.apiVersion)
        assertEquals(4102444800L, s.serverTime)
        assertEquals(10800, s.utcOffsetSeconds)
        assertEquals("EGP", s.currency)
        assertEquals(2.14, s.priceKwh, 0.001)
        assertEquals(500.0, s.budget!!, 0.001)
        assertEquals(12.3, s.monthKwh, 0.001)
        assertEquals(26.32, s.monthCost, 0.001)
        assertEquals(2, s.unreadAlerts)
        assertEquals(10086, s.port)
        val strip = s.strips.single()
        assertEquals("Desk", strip.displayName)
        assertEquals(4102444799L, strip.reportedAt)
        assertEquals(0.42, strip.todayKwh, 0.001)
        assertEquals(12.3, strip.monthKwh, 0.001)
        assertEquals(26.32, strip.monthCost, 0.001)
        assertEquals(listOf("temp_high", "new_alarm"), strip.alarms)
        assertEquals("Fridge", strip.outlet(4)!!.name)
        assertTrue(strip.outletOn(4))
        assertEquals(listOf("temp_high"), strip.outlet(4)!!.alarms)
    }

    @Test fun v1StateHasSafeDefaults() {
        val s = Api.parseSnapshot(JSONObject("""{"local_ip":"pc","devices":[{
            "mac":"A","name":"Old strip","online":true,"power_w":10,
            "outlets":[{"n":1,"on":false,"power_w":0,"energy_kwh":0.1,"temp_c":30}]}]}"""))
        assertEquals(1, s.apiVersion)
        assertNull(s.serverTime)
        assertEquals(0, s.utcOffsetSeconds)
        assertNull(s.budget)
        assertEquals(0, s.unreadAlerts)
        assertEquals(0.0, s.monthCost, 0.0)
        val strip = s.strips.single()
        assertEquals("Old strip", strip.displayName)
        assertNull(strip.reportedAt)
        assertNull(strip.voltage)
        assertNull(strip.currentA)
        assertNull(strip.rssi)
        assertTrue(strip.alarms.isEmpty())
        assertEquals("", strip.outlet(1)!!.name)
        assertTrue(strip.outlet(1)!!.alarms.isEmpty())
    }

    @Test fun nullsAndMalformedOptionalValuesDoNotBecomeNullTextOrNaN() {
        val s = Api.parseSnapshot(JSONObject("""{"budget":null,"month_cost":"bad","server_time":null,
            "devices":[null,{"name":"Strip","label":null,"reported_at":null,"voltage":"bad",
            "alarm":"power_high","outlets":[{"n":2,"name":null,"alarm":null}]}]}"""))
        assertEquals("Strip", s.strips.single().displayName)
        assertNull(s.strips.single().voltage)
        assertEquals(listOf("power_high"), s.strips.single().alarms)
        assertEquals("", s.strips.single().outlet(2)!!.name)
        assertEquals(0.0, s.monthCost, 0.0)
    }

    @Test fun rulesParseAllKindsAndNullableRuntimeFields() {
        val rules = Api.parseRules(JSONObject("""{"rules":[
            {"id":"s","kind":"schedule","mac":"A","outlets":[1,4],"enabled":false,"label":"Morning",
             "at":"07:30","days":[0,1],"on":true,"next_at":null,"phase":null},
            {"id":"t","kind":"timer","mac":"A","outlets":[2],"fire_at":4102444800,"next_at":4102444800,"on":false},
            {"id":"c","kind":"cycle","mac":"A","outlets":[3],"on_min":10,"off_min":50,"phase":"on"},
            {"id":"a","kind":"autooff","mac":"A","outlets":[4],"below_w":3.5,"for_min":10,"next_at":4102444801}
        ]}"""))
        assertEquals(4, rules.size)
        assertFalse(rules[0].enabled)
        assertEquals(listOf(1,4), rules[0].outlets)
        assertEquals(listOf(0,1), rules[0].days)
        assertNull(rules[0].nextAt)
        assertNull(rules[0].phase)
        assertEquals("07:30", rules[0].at)
        assertTrue(rules[0].on)
        assertEquals(4102444800L, rules[1].fireAt)
        assertEquals(4102444800L, rules[1].nextAt)
        assertEquals(10, rules[2].onMin)
        assertEquals(50, rules[2].offMin)
        assertEquals("on", rules[2].phase)
        assertEquals(3.5, rules[3].belowW, 0.0)
        assertEquals(10, rules[3].forMin)
    }

    @Test fun historyParsesBucketsAndBreakdown() {
        val h = Api.parseHistory(JSONObject("""{"range":"week","unit":"day","currency":"EGP",
            "buckets":[{"t":4102444800,"kwh":0.42,"cost":0.9},{"t":4102531200,"kwh":0,"cost":0}],
            "total_kwh":3.1,"total_cost":6.63,"per_outlet":[{"n":4,"kwh":1.9},{"n":1,"kwh":1.2}]}"""))
        assertEquals("week", h.range)
        assertEquals("day", h.unit)
        assertEquals("EGP", h.currency)
        assertEquals(4102444800L, h.buckets.first().t)
        assertEquals(0.42, h.buckets.first().kwh, 0.0)
        assertEquals(0.9, h.buckets.first().cost, 0.0)
        assertEquals(3.1, h.totalKwh, 0.0)
        assertEquals(6.63, h.totalCost, 0.0)
        assertEquals(4, h.perOutlet.first().n)
        assertEquals(1.9, h.perOutlet.first().kwh, 0.0)
    }

    @Test fun settingsAreParsedWithoutReadingTheWriteOnlyToken() {
        val s = Api.parseSettings(JSONObject("""{"currency":"EGP","price_kwh":2.14,"budget":500,
          "temp_max_c":60,"power_max_w":3500,"ntfy_url":"https://ntfy.sh/topic",
          "telegram_chat":"12345","telegram_configured":true,"telegram_token":"ignored"}"""))
        assertEquals("EGP", s.currency)
        assertEquals(2.14, s.priceKwh, 0.0)
        assertEquals(500.0, s.budget!!, 0.0)
        assertEquals(60.0, s.tempMaxC, 0.0)
        assertEquals(3500.0, s.powerMaxW, 0.0)
        assertEquals("https://ntfy.sh/topic", s.ntfyUrl)
        assertEquals("12345", s.telegramChat)
        assertTrue(s.telegramConfigured)
        assertNull(Api.parseSettings(JSONObject("""{"budget":null}""")).budget)
    }

    @Test fun eventsAreNewestFirstWithLongIdsTimesAndParameters() {
        val page = Api.parseEvents(JSONObject("""{"events":[
          {"id":4102444800,"ts":4102444801,"level":"alert","code":"temp_high","mac":"A","outlet":4,
           "text":"fallback","params":{"temp_c":61,"watts":12,"on":true,"pct":90}},
          {"id":4102444802,"ts":4102444803,"code":"future","text":"Server text","params":null}],"unread_alerts":1}"""))
        assertEquals(4102444802L, page.events.first().id)
        assertEquals("Server text", page.events.first().text)
        val e = page.events.last()
        assertEquals(4102444801L, e.ts)
        assertEquals("alert", e.level)
        assertEquals("temp_high", e.code)
        assertEquals(4, e.outlet)
        assertEquals(61, e.params.optInt("temp_c"))
        assertTrue(e.params.optBoolean("on"))
        assertEquals(1, page.unreadAlerts)
        assertEquals(0, page.events.first().params.length())
    }

    @Test fun emptyResponsesDefaultCollectionsAndSettingsSafely() {
        val empty = JSONObject()
        assertTrue(Api.parseSnapshot(empty).strips.isEmpty())
        assertTrue(Api.parseRules(empty).isEmpty())
        assertTrue(Api.parseHistory(empty).buckets.isEmpty())
        assertTrue(Api.parseEvents(empty).events.isEmpty())
        assertFalse(Api.parseSettings(empty).telegramConfigured)
    }

    @Test fun notificationResultIncludesSentFailedAndNoChannelError() {
        val result = Api.parseNotification(JSONObject("""{"ok":true,"sent":["ntfy"],
            "failed":[{"channel":"telegram","error":"HTTP 401"}]}"""))
        assertTrue(result.ok)
        assertEquals(listOf("ntfy"), result.sent)
        assertEquals(FailedChannel("telegram", "HTTP 401"), result.failed.single())
        val none = Api.parseNotification(JSONObject("""{"ok":false,"error":"no push channel configured"}"""))
        assertFalse(none.ok)
        assertEquals("no push channel configured", none.error)
    }

    @Test fun unconfirmedCommandsAreNotSuccessfulConfirmations() {
        assertFalse(Api.commandConfirmed(JSONObject("""{"ok":true,"confirmed":false}""")))
        assertTrue(Api.commandConfirmed(JSONObject("""{"ok":true,"confirmed":true}""")))
        assertTrue(Api.commandConfirmed(JSONObject("""{"ok":true}"""))) // v1 had no confirmation field
    }
}
