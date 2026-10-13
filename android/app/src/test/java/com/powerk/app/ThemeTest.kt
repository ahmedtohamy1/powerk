package com.powerk.app

import android.content.Context
import android.content.res.Configuration
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Outline
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.LayoutDirection
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ThemeTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    @Before fun reset() { ThemePrefs.prefs(context).edit().clear().commit() }
    @Test fun preferenceDefaultsToKraft() { assertEquals(ThemeChoice(), ThemePrefs.read(context)) }
    @Test fun preferencesRoundTrip() {
        AppTheme.entries.forEach { theme -> CleanMode.entries.forEach { mode ->
            val value = ThemeChoice(theme, mode)
            ThemePrefs.save(context, value)
            assertEquals(value, ThemePrefs.read(context))
        } }
    }
    @Test fun onlyCleanCanFollowTheSystemOrForceAMode() {
        assertFalse(ThemeChoice().isDark(true))
        assertTrue(ThemeChoice(AppTheme.Night, CleanMode.Light).isDark(false))
        assertFalse(ThemeChoice(AppTheme.Clean, CleanMode.Light).isDark(true))
        assertTrue(ThemeChoice(AppTheme.Clean, CleanMode.Dark).isDark(false))
        assertTrue(ThemeChoice(AppTheme.Clean).isDark(true))
        assertFalse(ThemeChoice(AppTheme.Clean).isDark(false))
    }
    @Test fun widgetAndComposeTokensAgree() {
        ThemePrefs.save(context, ThemeChoice(AppTheme.Clean))
        val dark = context.createConfigurationContext(Configuration(context.resources.configuration).apply {
            uiMode = Configuration.UI_MODE_NIGHT_YES
        })
        assertEquals(powerkColors(ThemeChoice(AppTheme.Clean), true), widgetColors(dark))
    }
    @Test fun wifiBoundaries() {
        mapOf(-20 to 0, -60 to 0, -61 to 1, -70 to 1, -71 to 2, -80 to 2, -81 to 3, -100 to 3)
            .forEach { (dbm, level) -> assertEquals(level, wifiLevel(dbm)) }
    }
    @Test fun cssUsesOneFactorForAllEllipticalRadii() {
        val r = EllipticalRadii(listOf(255f, 15f, 225f, 15f), listOf(15f, 225f, 15f, 255f))
        assertEquals(1f, r.scale(600f, 600f), .0001f)
        assertEquals(100f / 270f, r.scale(400f, 100f), .0001f)
        assertEquals(120f / 270f, r.scale(120f, 500f), .0001f)
        assertEquals(0f, r.scale(0f, 100f), .0001f)
        val flipped = EllipticalRadii(r.y, r.x)
        assertEquals(r.scale(400f, 100f), flipped.scale(100f, 400f), .0001f)
    }
    @Test fun outlinesRetainBoundsAndAbsoluteCssCornersInRtl() {
        listOf(Wobble, Wobble2).forEach { shape ->
            val ltr = shape.createOutline(Size(360f, 140f), LayoutDirection.Ltr, Density(1f)) as Outline.Generic
            val rtl = shape.createOutline(Size(360f, 140f), LayoutDirection.Rtl, Density(1f)) as Outline.Generic
            assertEquals(ltr.path.getBounds(), rtl.path.getBounds())
            assertEquals(360f, ltr.path.getBounds().width, .001f)
            assertEquals(140f, ltr.path.getBounds().height, .001f)
        }
    }
}
