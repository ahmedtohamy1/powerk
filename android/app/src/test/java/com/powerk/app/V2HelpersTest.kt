package com.powerk.app

import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import java.util.Locale
import javax.xml.parsers.DocumentBuilderFactory

class V2HelpersTest {
    private val strings = DocumentBuilderFactory.newInstance().newDocumentBuilder()
        .parse(File("src/main/res/values/strings.xml")).getElementsByTagName("string").let { nodes ->
            (0 until nodes.length).associate {
                val e = nodes.item(it) as Element
                e.getAttribute("name") to e.textContent
            }
        }
    private fun text(id: Int, args: List<Any>): String {
        val key = when (id) {
            R.string.state_on -> "state_on"; R.string.state_off -> "state_off"
            R.string.state_pending -> "state_pending"
            R.string.desc_schedule -> "desc_schedule"; R.string.desc_timer -> "desc_timer"
            R.string.desc_cycle -> "desc_cycle"; R.string.desc_autooff -> "desc_autooff"
            else -> listOf("mon", "tue", "wed", "thu", "fri", "sat", "sun")[weekdayStrings.indexOf(id)]
        }
        return String.format(Locale.ENGLISH, strings.getValue(key), *args.toTypedArray())
    }

    @Test fun budgetThresholdsAreNotRoundedEarly() {
        assertEquals(74.9, budgetPercentage(74.9, 100.0)!!, 0.0001)
        assertEquals(75.0, budgetPercentage(150.0, 200.0)!!, 0.0001)
        assertEquals(90.0, budgetPercentage(180.0, 200.0)!!, 0.0001)
        assertEquals(125.0, budgetPercentage(250.0, 200.0)!!, 0.0001)
    }
    @Test fun disabledOrInvalidBudgetsHaveNoPercentage() {
        assertNull(budgetPercentage(10.0, null))
        assertNull(budgetPercentage(10.0, 0.0))
        assertNull(budgetPercentage(10.0, -1.0))
        assertNull(budgetPercentage(Double.NaN, 100.0))
        assertEquals(0.0, budgetPercentage(-1.0, 100.0)!!, 0.0)
    }
    @Test fun serverTimeUsesItsOffsetNotThePhoneZone() {
        assertEquals("1970-01-01 03:00", serverLocalTime(0L, 10800, "yyyy-MM-dd HH:mm", Locale.ENGLISH))
        assertEquals("1969-12-31 19:00", serverLocalTime(0L, -18000, "yyyy-MM-dd HH:mm", Locale.ENGLISH))
        assertEquals("2100-01-01 05:30", serverLocalTime(4102444800L, 19800, "yyyy-MM-dd HH:mm", Locale.ENGLISH))
        assertEquals("", serverLocalTime(null, 10800))
    }
    @Test fun scheduleDescriptionUsesLocalizedDaysActionAndNames() {
        val rule = Rule("r", "schedule", "A", listOf(1), at = "07:30", days = listOf(0,1), on = true)
        assertEquals("07:30 ON · Mon Tue · Fridge", ruleDescription(rule, 10800, "Fridge", ::text))
    }
    @Test fun timerDescriptionUsesServerLocalDeadline() {
        val rule = Rule("r", "timer", "A", listOf(1), fireAt = 69000L)
        assertEquals("Timer OFF at 22:10", ruleDescription(rule, 10800, "", ::text))
    }
    @Test fun cycleDescriptionUsesIntendedPhase() {
        val rule = Rule("r", "cycle", "A", listOf(1), onMin = 10, offMin = 50, phase = "on")
        assertEquals("Cycle 10 min on / 50 min off · intended phase ON", ruleDescription(rule, 0, "", ::text))
    }
    @Test fun autooffDescriptionUsesThresholdAndDuration() {
        val rule = Rule("r", "autooff", "A", listOf(1), belowW = 3.0, forMin = 10)
        assertEquals("Auto-off below 3.0 W for 10 min", ruleDescription(rule, 0, "", ::text))
    }
    @Test fun knownAlarmAndRuleCodesResolveToTranslatedStrings() {
        assertEquals(R.string.alarm_temp, alarmString("temp_high"))
        assertEquals(R.string.alarm_power, alarmString("power_high"))
        assertEquals(R.string.alarm_generic, alarmString("future"))
        assertEquals(R.string.kind_autooff, kindString("autooff"))
    }
    private fun draft() = RuleDraft("schedule", "A", setOf(1), "", "07:30", setOf(0), true,
        "1", "1", "1440", "3", "10")
    @Test fun scheduleValidationRejectsInvalidTimeDaysAndOutlets() {
        assertTrue(draft().valid())
        assertFalse(draft().copy(at = "24:00").valid())
        assertFalse(draft().copy(at = "7:30").valid())
        assertFalse(draft().copy(days = emptySet()).valid())
        assertFalse(draft().copy(days = setOf(7)).valid())
        assertFalse(draft().copy(outlets = emptySet()).valid())
        assertFalse(draft().copy(outlets = setOf(0)).valid())
        assertFalse(draft().copy(label = "x".repeat(41)).valid())
    }
    @Test fun timerValidationMatchesContractBounds() {
        val d = draft().copy(kind = "timer")
        assertTrue(d.copy(minutes = "1").valid())
        assertTrue(d.copy(minutes = "10080").valid())
        assertFalse(d.copy(minutes = "0").valid())
        assertFalse(d.copy(minutes = "10081").valid())
        assertFalse(d.copy(minutes = "1.5").valid())
    }
    @Test fun cycleAndAutooffValidationMatchContractBounds() {
        assertTrue(draft().copy(kind = "cycle").valid())
        assertFalse(draft().copy(kind = "cycle", onMin = "0").valid())
        assertFalse(draft().copy(kind = "cycle", offMin = "1441").valid())
        val d = draft().copy(kind = "autooff")
        assertTrue(d.copy(belowW = "3500", forMin = "1440").valid())
        assertFalse(d.copy(belowW = "0").valid())
        assertFalse(d.copy(belowW = "3500.1").valid())
        assertFalse(d.copy(belowW = "NaN").valid())
        assertFalse(d.copy(forMin = "1441").valid())
    }
    @Test fun rulePayloadNeverIncludesComputedRuntimeFields() {
        ruleKinds.forEach { kind ->
            val body = draft().copy(kind = kind).body()
            assertFalse(body.has("id"))
            assertFalse(body.has("next_at"))
            assertFalse(body.has("phase"))
            assertEquals(kind, body.getString("kind"))
            assertTrue(body.getJSONArray("outlets").length() > 0)
        }
        assertTrue(draft().body().get("on") is Boolean)
    }
    @Test fun settingsPayloadOmissionReplacementClearAndNullBudget() {
        val s = ServerSettings("EGP", 2.0, null)
        assertFalse(settingsBody(s, "", false).has("telegram_token"))
        assertEquals("replacement", settingsBody(s, "replacement", false).getString("telegram_token"))
        assertEquals("", settingsBody(s, "replacement", true).getString("telegram_token"))
        assertTrue(settingsBody(s, "", false).isNull("budget"))
        assertFalse(settingsBody(s, "", false).has("telegram_configured"))
    }
    private fun state(on: Boolean = true, online: Boolean = true) = Api.parseSnapshot(JSONObject("""{
        "devices":[{"mac":"OTHER","online":true,"outlets":[{"n":4,"on":false}]},
        {"mac":"TARGET","online":$online,"outlets":[{"n":4,"on":$on,"name":"Renamed"},{"n":1,"on":false}]}]}"""))
    private val target = OutletTarget("TARGET", 4, "Old name")
    @Test fun targetIdentitySurvivesReorderingAndRenaming() {
        val (strip, outlet) = state().resolve(target)!!
        assertEquals("TARGET", strip.mac)
        assertEquals(4, outlet.n)
        assertEquals("Renamed", outlet.name)
        assertNull(state().resolve(target.copy(n = 3)))
        assertNull(state().resolve(target.copy(mac = "MISSING")))
    }
    @Test fun safeToggleReadsFreshStateAndSendsExplicitOppositeOnlyOnce() = runBlocking {
        val actions = mutableListOf<String>()
        val result = toggleFresh(target, { actions += "fetch"; state() }) { mac, n, on ->
            actions += "$mac:$n:$on"; false
        }
        assertEquals(listOf("fetch", "TARGET:4:false", "fetch"), actions)
        assertFalse(result.confirmed)
    }
    @Test fun safeToggleNeverWritesForMissingOrOfflineTargets() = runBlocking {
        listOf(state(online = false), Snapshot("", emptyList())).forEach { s ->
            var writes = 0
            try {
                toggleFresh(target, { s }) { _, _, _ -> writes++; true }
                fail("expected unavailable target")
            } catch (_: ServerException) { assertEquals(0, writes) }
        }
    }
    @Test fun unknownCommandFailureNeverRetries() = runBlocking {
        var writes = 0
        var reads = 0
        try {
            toggleFresh(target, { reads++; state() }) { _, _, _ -> writes++; throw ServerException("unknown", 502) }
            fail("expected error")
        } catch (e: ServerException) {
            assertEquals(502, e.status)
            assertEquals(1, writes)
            assertEquals(1, reads)
        }
    }
    @Test fun arabicAndPersianNumbersNormalizeWithoutChangingValues() {
        assertEquals("07:30", normalizeNumber("٠٧:٣٠"))
        assertEquals(10080, "١٠٠٨٠".apiInt())
        assertEquals(12.5, "١٢٫٥".apiDouble()!!, 0.0)
        assertEquals(1234.5, "١٬٢٣٤٫٥".apiDouble()!!, 0.0)
        assertEquals(12.5, "۱۲٫۵".apiDouble()!!, 0.0)
        assertNull("12,5".apiDouble()) // Ambiguous ASCII commas must not silently change a value.
        assertNull("1,000".apiDouble())
        assertNull("1,500".apiDouble())
        assertEquals("12.5", normalizeNumber("\u061c١٢٫٥\u200f"))
        assertNull("NaN".apiDouble())
    }
    @Test fun arabicRuleInputsValidateAndSerializeAsApiValues() {
        val schedule = draft().copy(at = "٠٧:٣٠")
        assertTrue(schedule.valid())
        assertEquals("07:30", schedule.body().getString("at"))
        val timer = draft().copy(kind = "timer", minutes = "١٠٠٨٠")
        assertTrue(timer.valid())
        assertEquals(10080, timer.body().getInt("minutes"))
        val autooff = draft().copy(kind = "autooff", belowW = "٣٫٥", forMin = "١٠")
        assertTrue(autooff.valid())
        assertEquals(3.5, autooff.body().getDouble("below_w"), 0.0)
        assertEquals(10, autooff.body().getInt("for_min"))
    }
    @Test fun unsetCyclePhaseIsPendingNotAssumedOff() {
        val rule = Rule("r", "cycle", "A", listOf(1), phase = null)
        assertEquals("Cycle 1 min on / 1 min off · intended phase Pending", ruleDescription(rule, 0, "", ::text))
    }
    @Test fun newRuleSelectionRecoversWhenFirstDeviceArrives() {
        assertEquals("", newRuleMac("", emptyList()))
        assertEquals("MAC", newRuleMac("", listOf("MAC")))
        assertEquals("SECOND", newRuleMac("SECOND", listOf("FIRST", "SECOND")))
        assertEquals("FIRST", newRuleMac("REMOVED", listOf("FIRST")))
    }
}
