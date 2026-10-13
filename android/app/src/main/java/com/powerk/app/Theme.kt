package com.powerk.app

import android.content.Context
import android.content.SharedPreferences
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.core.view.WindowCompat
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.ExperimentalTextApi
import androidx.compose.ui.text.font.*
import androidx.compose.ui.text.style.TextDirection
import androidx.compose.ui.unit.sp

enum class AppTheme(val key: String) { Kraft("kraft"), Clean("clean"), Night("night") }
enum class CleanMode(val key: String) { System("system"), Light("light"), Dark("dark") }
data class ThemeChoice(val theme: AppTheme = AppTheme.Kraft, val mode: CleanMode = CleanMode.System) {
    fun isDark(system: Boolean) = theme == AppTheme.Night || theme == AppTheme.Clean &&
        (mode == CleanMode.Dark || mode == CleanMode.System && system)
}
object ThemePrefs {
    fun prefs(context: Context): SharedPreferences = context.applicationContext.getSharedPreferences("appearance", Context.MODE_PRIVATE)
    fun read(context: Context): ThemeChoice {
        val p = prefs(context)
        return ThemeChoice(AppTheme.entries.find { it.key == p.getString("theme", null) } ?: AppTheme.Kraft,
            CleanMode.entries.find { it.key == p.getString("clean_mode", null) } ?: CleanMode.System)
    }
    fun save(context: Context, choice: ThemeChoice) {
        prefs(context).edit().putString("theme", choice.theme.key).putString("clean_mode", choice.mode.key).apply()
        WidgetUpdates.appearanceChanged(context)
    }
}

/** PAGE's CSS tokens. Background surfaces have no Material tonal tint. */
data class PowerkColors(val bg: Color, val card: Color, val fill: Color, val ink: Color, val ink2: Color,
    val line: Color, val accent: Color, val onAccent: Color, val onAccentSecondary: Color,
    val accentSoft: Color, val accentText: Color, val highlighter: Color, val onHighlighter: Color,
    val tape: Color, val chart: Color, val switchOn: Color, val focus: Color,
    val ok: Color, val okSoft: Color, val warn: Color, val warnSoft: Color, val bad: Color,
    val badSoft: Color, val scrim: Color, val bar: Color, val dark: Boolean, val fullHighlight: Boolean,
    val switchThumb: Color = Color.White)

fun powerkColors(choice: ThemeChoice = ThemeChoice(), systemDark: Boolean = false): PowerkColors {
    fun c(hex: Long) = Color(hex or 0xFF000000)
    val dark = choice.isDark(systemDark)
    return when (choice.theme) {
        AppTheme.Kraft -> PowerkColors(c(0xc9a46e), c(0xece0b8), c(0xd8b983), c(0x17130f), c(0x3a2e20),
            c(0xb48c55), c(0xf7e33b), c(0x17130f), c(0x3a2e20), c(0xf3e690), c(0x083f3a),
            c(0xf7e33b), c(0x17130f), c(0xece0b8).copy(alpha = .8f), c(0x1c9a90), c(0x1c9a90), c(0x17130f),
            c(0x0b6b62), c(0xcfe0c8), c(0x7d4306), c(0xf2dca0), c(0x9c2318), c(0xefcbb5),
            c(0x17130f).copy(alpha = .45f), c(0xd8b983).copy(alpha = .95f), false, false)
        AppTheme.Night -> PowerkColors(c(0x0B0B0F), c(0x1C1C21), c(0x2A2A31), c(0xf3eee2), c(0xcfc8b8),
            c(0x33333B), c(0xf7e33b), c(0x17130f), c(0x3a2e20), c(0x3a3816), c(0xf7e33b),
            c(0xf7e33b), c(0x17130f), c(0xece0b8).copy(alpha = .85f), c(0x1c9a90), c(0x1c9a90), c(0xf7e33b),
            c(0x5fd3c6), c(0x163a36), c(0xf0b54a), c(0x3a2f14), c(0xff8f7f), c(0x3e1e19),
            Color.Black.copy(alpha = .6f), c(0x0b0b0f).copy(alpha = .94f), true, true)
        AppTheme.Clean -> PowerkColors(if (dark) c(0x0B0B0F) else c(0xF2F2F7), if (dark) c(0x1C1C21) else c(0xFFFFFF),
            if (dark) c(0x2A2A31) else c(0xE9E9EF), if (dark) c(0xF4F4F6) else c(0x111827),
            if (dark) c(0xA3A3AE) else c(0x5B6170), if (dark) c(0x2E2E36) else c(0xE3E4E9),
            c(0x2563EB), Color.White, Color.White.copy(alpha = .9f), if (dark) c(0x1D2947) else c(0xE2EAFD),
            if (dark) c(0x93B6FF) else c(0x1D4ED8), if (dark) c(0x1D2947) else c(0xE2EAFD),
            if (dark) c(0xF4F4F6) else c(0x111827), if (dark) c(0x93B6FF).copy(alpha = .22f) else c(0x2563EB).copy(alpha = .18f),
            c(0x2563EB), if (dark) c(0x30A852) else c(0x15803D), if (dark) c(0x93B6FF) else c(0x2563EB),
            if (dark) c(0x4ADE80) else c(0x15803D), if (dark) c(0x12301D) else c(0xDCF5E4),
            if (dark) c(0xFBBF24) else c(0xB45309), if (dark) c(0x352A0E) else c(0xFDF0D5),
            if (dark) c(0xF87171) else c(0xDC2626), if (dark) c(0x3A1818) else c(0xFDE4E4),
            if (dark) Color.Black.copy(alpha = .6f) else c(0x111827).copy(alpha = .38f),
            if (dark) c(0x16161b).copy(alpha = .86f) else Color.White.copy(alpha = .86f), dark, false)
    }
}
val LocalPowerkColors = staticCompositionLocalOf { powerkColors() }
val LocalThemeChoice = staticCompositionLocalOf { ThemeChoice() }

fun PowerkColors.scheme(): ColorScheme {
    val base = if (dark) darkColorScheme() else lightColorScheme()
    return base.copy(primary = accent, onPrimary = onAccent, primaryContainer = highlighter, onPrimaryContainer = onHighlighter,
        secondary = ink2, onSecondary = card, secondaryContainer = highlighter, onSecondaryContainer = onHighlighter,
        tertiary = chart, onTertiary = switchThumb, tertiaryContainer = okSoft, onTertiaryContainer = ok,
        background = bg, onBackground = ink, surface = bg, onSurface = ink, surfaceVariant = fill, onSurfaceVariant = ink2,
        surfaceContainerLowest = card, surfaceContainerLow = card, surfaceContainer = card, surfaceContainerHigh = fill,
        surfaceContainerHighest = fill, surfaceBright = card, surfaceDim = bg, surfaceTint = Color.Transparent,
        outline = ink, outlineVariant = line, error = bad, onError = bg, errorContainer = badSoft, onErrorContainer = bad,
        inverseSurface = ink, inverseOnSurface = bg, inversePrimary = accent, scrim = scrim)
}

data class PowerkType(val marker: TextStyle, val hand: TextStyle, val body: TextStyle, val figures: TextStyle,
    val brand: TextStyle, val arabic: Boolean)
val LocalPowerkType = staticCompositionLocalOf { PowerkType(TextStyle.Default, TextStyle.Default, TextStyle.Default,
    TextStyle.Default, TextStyle.Default, false) }

@OptIn(ExperimentalTextApi::class)
fun powerkType(arabic: Boolean): PowerkType {
    val marhey = FontFamily(Font(R.font.marhey, FontWeight.Bold, variationSettings = FontVariation.Settings(FontVariation.weight(700))))
    val marker = FontFamily(Font(R.font.permanent_marker, FontWeight.Bold))
    val hand = if (arabic) marhey else FontFamily(Font(R.font.kalam_bold, FontWeight.Bold))
    val body = if (arabic) FontFamily(Font(R.font.plex_arabic_regular), Font(R.font.plex_arabic_semibold, FontWeight.SemiBold))
        else FontFamily(Font(R.font.atkinson_regular), Font(R.font.atkinson_bold, FontWeight.Bold))
    val mono = FontFamily(Font(R.font.atkinson_mono, variationSettings = FontVariation.Settings(FontVariation.weight(400))),
        Font(R.font.atkinson_mono, FontWeight.Bold, variationSettings = FontVariation.Settings(FontVariation.weight(700))))
    return PowerkType(TextStyle(fontFamily = if (arabic) marhey else marker, fontWeight = FontWeight.Bold, fontSize = 28.sp, lineHeight = 40.sp),
        TextStyle(fontFamily = hand, fontWeight = FontWeight.Bold, fontSize = 15.sp, lineHeight = 24.sp),
        TextStyle(fontFamily = body, fontSize = 16.sp, lineHeight = 24.sp),
        TextStyle(fontFamily = mono, fontSize = 16.sp, lineHeight = 24.sp, textDirection = TextDirection.Ltr),
        TextStyle(fontFamily = marker, fontWeight = FontWeight.Bold, fontSize = 28.sp, lineHeight = 36.sp), arabic)
}
private fun PowerkType.typography() = Typography(
    displayLarge = marker.copy(fontSize = 52.sp, lineHeight = 64.sp), displayMedium = marker.copy(fontSize = 44.sp, lineHeight = 56.sp),
    displaySmall = marker.copy(fontSize = 36.sp, lineHeight = 48.sp), headlineLarge = marker.copy(fontSize = 32.sp, lineHeight = 44.sp),
    headlineMedium = marker, headlineSmall = marker.copy(fontSize = 24.sp, lineHeight = 36.sp),
    titleLarge = marker.copy(fontSize = 22.sp, lineHeight = 32.sp), titleMedium = marker.copy(fontSize = 18.sp, lineHeight = 28.sp),
    titleSmall = hand, bodyLarge = body, bodyMedium = body.copy(fontSize = 14.sp, lineHeight = 22.sp),
    bodySmall = body.copy(fontSize = 12.sp, lineHeight = 20.sp), labelLarge = hand,
    labelMedium = hand.copy(fontSize = 13.sp, lineHeight = 22.sp), labelSmall = hand.copy(fontSize = 12.sp, lineHeight = 20.sp))

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
fun PowerkTheme(choice: ThemeChoice? = null, dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    val context = LocalContext.current
    var stored by remember(context) { mutableStateOf(ThemePrefs.read(context)) }
    DisposableEffect(context) {
        val p = ThemePrefs.prefs(context)
        val listener = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> stored = ThemePrefs.read(context) }
        p.registerOnSharedPreferenceChangeListener(listener)
        onDispose { p.unregisterOnSharedPreferenceChangeListener(listener) }
    }
    val selected = choice ?: stored
    val colors = powerkColors(selected, dark)
    val type = powerkType(LocalConfiguration.current.locales[0].language == "ar")
    SideEffect {
        context.findActivity()?.let { activity ->
            activity.window.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(colors.bg.toArgb()))
            WindowCompat.getInsetsController(activity.window, activity.window.decorView).apply {
                isAppearanceLightStatusBars = !colors.dark
                isAppearanceLightNavigationBars = !colors.dark
            }
        }
    }
    CompositionLocalProvider(LocalPowerkColors provides colors, LocalPowerkType provides type, LocalThemeChoice provides selected) {
        MaterialExpressiveTheme(colorScheme = colors.scheme(), typography = type.typography(),
            shapes = Shapes(extraSmall = Wobble2, small = Wobble2, medium = Wobble, large = Wobble, extraLarge = Wobble),
            motionScheme = MotionScheme.expressive(), content = content)
    }
}
