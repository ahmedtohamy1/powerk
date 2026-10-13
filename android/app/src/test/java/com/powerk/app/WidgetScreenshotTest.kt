package com.powerk.app

import android.content.Context
import android.view.View
import android.widget.RemoteViews
import android.widget.TextView
import android.widget.ImageView
import android.view.ViewGroup
import android.graphics.Rect
import android.text.TextUtils
import androidx.compose.ui.graphics.toArgb
import androidx.test.core.app.ApplicationProvider
import androidx.core.view.drawToBitmap
import com.github.takahirom.roborazzi.captureRoboImage
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [35], qualifiers = "ar-rEG-w412dp-h1000dp-mdpi")
class WidgetScreenshotTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private fun capture(name: String, theme: AppTheme, width: Int, height: Int, kind: String, wide: Boolean = false,
        outletNumber: Int = 1, offline: Boolean = false) {
        ThemePrefs.save(context, ThemeChoice(theme))
        val fixture = visualSnapshot()
        val snapshot = if (offline) fixture.copy(strips = fixture.strips.map { it.copy(online = false) }) else fixture
        val strip = snapshot.strips.single()
        TargetPrefs.setWidget(context, 42, OutletTarget(strip.mac, outletNumber, strip.outlet(outletNumber)!!.name))
        StripWidgetPrefs.save(context, 42, strip.mac)
        val views = when (kind) {
            "outlet" -> outletViews(context, 42, snapshot, compact = width < 110)
            "strip" -> stripViews(context, 42, snapshot, wide = wide, compact = height < 110 || width < 180)
            else -> usageViews(context, 42, snapshot, wide = wide, compact = height < 110 || width < 180, minimum = height < 140 && width < 140)
        }
        val view = views.apply(context.localized(), null)
        view.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY), View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY))
        view.layout(0, 0, width, height)
        assertTrue(view.findViewById<TextView>(R.id.widget_name).text.isNotBlank())
        val colors = widgetColors(context)
        assertEquals((if (kind != "outlet") colors.ink else if (offline) colors.ink.copy(alpha = .6f)
            else if (strip.outlet(outletNumber)!!.on) colors.onAccent else colors.ink).toArgb(),
            view.findViewById<TextView>(R.id.widget_name).currentTextColor)
        if (kind == "outlet") {
            val state = view.findViewById<TextView>(R.id.widget_state)
            assertRendered(state)
            assertEquals(1, state.layout.lineCount)
            assertNoEllipsis(state)
            if (width < 110 && !offline) assertEquals(isolateFigures("${decimal(strip.outlet(outletNumber)!!.powerW, 1)} W"), state.text.toString())
            assertEquals(2, view.findViewById<TextView>(R.id.widget_name).maxLines)
            assertEquals(if (offline) 140 else 255, view.findViewById<ImageView>(R.id.widget_power).imageAlpha)
        }
        if (kind == "strip") {
            val short = height < 110 || width < 180
            val pad = if (short) 8 else 12
            listOf(view.paddingLeft, view.paddingTop, view.paddingRight, view.paddingBottom).forEach { assertEquals(pad, it) }
            val bounds = mutableListOf<Rect>()
            (1..4).forEach { n ->
            val state = view.findViewById<TextView>(listOf(R.id.strip_state_1, R.id.strip_state_2, R.id.strip_state_3, R.id.strip_state_4)[n - 1])
            val cell = state.parent as View
            val title = view.findViewById<TextView>(listOf(R.id.strip_name_1, R.id.strip_name_2, R.id.strip_name_3, R.id.strip_name_4)[n - 1])
            assertRendered(state)
            assertEquals(if (short) 10f else 11f, state.textSize, .01f)
            assertNoEllipsis(state)
            assertEquals(2, state.maxLines)
            assertEquals(TextUtils.TruncateAt.END, state.ellipsize)
            assertEquals(8, cell.paddingLeft)
            assertEquals(8, cell.paddingRight)
            assertEquals(cell.width - cell.paddingLeft - cell.paddingRight, state.width)
            val rect = Rect().also { cell.getDrawingRect(it); (view as ViewGroup).offsetDescendantRectToMyCoords(cell, it) }
            assertTrue("Cell $n enters frame padding: $rect", rect.left >= pad && rect.top >= pad && rect.right <= width - pad && rect.bottom <= height - pad)
            bounds.add(rect)
            assertRendered(title)
            assertTrue("Name/state overlap for outlet $n", title.bottom <= state.top)
            assertTrue("Name clipped for outlet $n", title.height >= title.layout.getLineBottom(title.layout.lineCount - 1))
            assertEquals("State must be pinned to cell bottom", cell.height - cell.paddingBottom, state.bottom)
            val value = strip.outlet(n)!!
            val expected = if (value.on) context.getString(R.string.widget_on_watts, decimal(value.powerW, 1)) else context.getString(R.string.state_off)
            assertEquals(expected, state.text.toString().replace("\u2066", "").replace("\u2069", "").replace('\u00A0', ' '))
            if (value.on) {
                val digits = state.text.indexOf(decimal(value.powerW, 1))
                val unit = state.text.indexOf('W')
                assertEquals("Watts and their unit must remain on the same line", state.layout.getLineForOffset(digits), state.layout.getLineForOffset(unit))
            }
            }
            val gap = if (short) 4 else 6
            bounds.groupBy { it.top }.values.forEach { row ->
                row.sortedBy { it.left }.zipWithNext().forEach { (a, b) -> assertEquals(gap, b.left - a.right) }
            }
            if (!wide) assertEquals(gap, bounds[2].top - bounds[0].bottom)
        }
        if (kind == "usage") intArrayOf(R.id.widget_name, R.id.usage_watts, R.id.usage_today,
            R.id.usage_today_cost, R.id.usage_month_cost, R.id.usage_budget).forEach { textId ->
            val text = view.findViewById<TextView>(textId)
            var bottom = text.bottom
            var parent = text.parent
            while (parent is View && parent != view) { bottom += parent.top; parent = parent.parent }
            assertTrue("Usage ${context.resources.getResourceEntryName(textId)} height=${text.height}, lineHeight=${text.lineHeight}, bottom=$bottom in ${height}px widget",
                text.height >= text.lineHeight && text.layout != null && text.layout.lineCount > 0)
            assertTrue("Usage field $textId extends below ${height}px widget", bottom <= height)
            assertNoEllipsis(text)
        }
        // Draw the actual inflated RemoteViews at launcher bounds. No Activity or live app is started.
        view.drawToBitmap().captureRoboImage("build/outputs/roborazzi/$name.png")
    }
    private fun assertRendered(text: TextView) {
        assertNotNull(text.layout)
        assertTrue("${context.resources.getResourceEntryName(text.id)} has no rendered lines", text.layout.lineCount > 0)
        assertTrue("${context.resources.getResourceEntryName(text.id)} clipped: height=${text.height}, needed=${text.layout.getLineBottom(text.layout.lineCount - 1)}",
            text.height >= text.layout.getLineBottom(text.layout.lineCount - 1))
    }
    private fun assertNoEllipsis(text: TextView) {
        (0 until text.layout.lineCount).forEach { line ->
            assertEquals("${context.resources.getResourceEntryName(text.id)} ellipsizes its state/measurement", 0, text.layout.getEllipsisCount(line))
        }
    }
    @Test fun outletKraft() = capture("widget_outlet_kraft", AppTheme.Kraft, 200, 100, "outlet")
    @Test fun outletNight() = capture("widget_outlet_night", AppTheme.Night, 200, 100, "outlet")
    @Test fun stripKraft() = capture("widget_strip_kraft", AppTheme.Kraft, 400, 110, "strip", true)
    @Test fun stripNight() = capture("widget_strip_night", AppTheme.Night, 400, 110, "strip", true)
    @Test fun usageKraft() = capture("widget_usage_kraft", AppTheme.Kraft, 200, 200, "usage")
    @Test fun usageNight() = capture("widget_usage_night", AppTheme.Night, 200, 200, "usage")
    @Test fun stripNarrowKraft() = capture("widget_strip_narrow_kraft", AppTheme.Kraft, 200, 200, "strip")
    @Test fun usageWideKraft() = capture("widget_usage_wide_kraft", AppTheme.Kraft, 400, 110, "usage", true)
    @Test fun outletCompactKraft() = capture("widget_outlet_compact_kraft", AppTheme.Kraft, 100, 100, "outlet")
    @Test fun outletSquareKraft() = capture("widget_outlet_square_kraft", AppTheme.Kraft, 200, 200, "outlet", outletNumber = 4)
    @Test fun outletOffKraft() = capture("widget_outlet_off_kraft", AppTheme.Kraft, 200, 100, "outlet", outletNumber = 2)
    @Test fun outletOfflineKraft() = capture("widget_outlet_offline_kraft", AppTheme.Kraft, 200, 100, "outlet", offline = true)
    @Test fun outletCompactWrappedKraft() = capture("widget_outlet_compact_wrapped_kraft", AppTheme.Kraft, 100, 100, "outlet", outletNumber = 4)
    @Test fun stripWrappedKraft() = capture("widget_strip_wrapped_kraft", AppTheme.Kraft, 360, 110, "strip", true)
    @Test fun stripShortKraft() = capture("widget_strip_short_kraft", AppTheme.Kraft, 300, 70, "strip", true)
    @Test fun usageShortKraft() = capture("widget_usage_short_kraft", AppTheme.Kraft, 300, 70, "usage", true)
    @Test fun usageMinimumKraft() = capture("widget_usage_minimum_kraft", AppTheme.Kraft, 110, 110, "usage")
    @Test fun legacySizeSelection() {
        assertTrue(widgetWide(220, 70))
        assertTrue(widgetWide(300, 70))
        assertFalse(widgetWide(200, 200))
        assertFalse(widgetWide(240, 200))
    }
    @Test fun budgetIsNumberFirst() {
        val view = usageViews(context, 42, visualSnapshot()).apply(context, null)
        val text = view.findViewById<TextView>(R.id.usage_budget).text.toString()
        assertEquals("${isolateFigures("18%")} من الميزانية", text)
    }
    @Test @Config(qualifiers = "en-rUS-w412dp-h1000dp-mdpi")
    fun englishBudgetIsNumberFirst() {
        val view = usageViews(context, 42, visualSnapshot(english = true)).apply(context, null)
        assertEquals("${isolateFigures("18%")} of budget", view.findViewById<TextView>(R.id.usage_budget).text.toString())
    }
    @Test @Config(sdk = [26]) @GraphicsMode(GraphicsMode.Mode.LEGACY)
    fun bundledFontsInflateOnApi26() {
        val s = visualSnapshot()
        TargetPrefs.setWidget(context, 42, OutletTarget(s.strips.single().mac, 1, "التلاجة"))
        val view = outletViews(context, 42, s).apply(context, null)
        assertNotNull(view.findViewById<TextView>(R.id.widget_name).typeface)
        val style = context.obtainStyledAttributes(R.style.WidgetName, intArrayOf(android.R.attr.textStyle))
        try { assertEquals(android.graphics.Typeface.BOLD, style.getInt(0, 0)) } finally { style.recycle() }
    }
    @Test fun outletPendingAndOffline() {
        val s = visualSnapshot()
        val strip = s.strips.single()
        TargetPrefs.setWidget(context, 42, OutletTarget(strip.mac, 1, "التلاجة"))
        WidgetUpdates.pending.add("${strip.mac}:1")
        try {
            val pending = outletViews(context, 42, s).apply(context, null)
            assertEquals(context.getString(R.string.switching), pending.findViewById<TextView>(R.id.widget_state).text.toString())
        } finally { WidgetUpdates.pending.clear() }
        val offline = outletViews(context, 42, null).apply(context, null)
        assertEquals(context.getString(R.string.state_offline), offline.findViewById<TextView>(R.id.widget_state).text.toString())
    }
}
