package com.powerk.app

import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.LayoutDirection
import com.github.takahirom.roborazzi.captureRoboImage
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

@RunWith(RobolectricTestRunner::class)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
@Config(sdk = [35], qualifiers = "ar-rEG-w412dp-h1000dp-mdpi")
class HomeScreenshotTest {
    @get:Rule val compose = createComposeRule()
    @Before fun setUp() { Server.save("127.0.0.1", 1, "") }
    @OptIn(ExperimentalMaterial3Api::class)
    private fun home(name: String, choice: ThemeChoice, english: Boolean = false, dark: Boolean = false) {
        val fixture = visualSnapshot(english)
        compose.setContent {
            PowerkTheme(choice, dark) {
                CompositionLocalProvider(LocalLayoutDirection provides if (english) LayoutDirection.Ltr else LayoutDirection.Rtl) {
                    Scaffold(topBar = { TopAppBar(title = { Text("POWERK", style = LocalPowerkType.current.brand) },
                        actions = { TextButton(onClick = {}) { Text(if (english) "العربية" else "EN") }
                            IconButton(onClick = {}) { Icon(Icons.Default.Settings, stringResource(R.string.open_settings)) } }) },
                        bottomBar = { Column { DashedDivider(); ShortNavigationBar(containerColor = LocalPowerkColors.current.bar) {
                            listOf(R.string.tab_plugs, R.string.tab_rules, R.string.tab_energy, R.string.tab_alerts).forEachIndexed { i, label ->
                                val icon = listOf(Icons.Default.Home, Icons.Default.DateRange, Icons.Default.Star, Icons.Default.Notifications)[i]
                                ShortNavigationBarItem(selected = i == 0, onClick = {}, icon = { Icon(icon, null) }, label = { Text(stringResource(label)) })
                            }
                        } } }) { padding ->
                        Box(Modifier.padding(padding)) { PlugsScreen(fixture, null, false, emptyMap(), {}, {}, { _, _, _ -> }) }
                    }
                }
            }
        }
        compose.onNodeWithText(if (english) "FRIDGE" else "التلاجة").assertExists()
        compose.onNodeWithText("${if (english) "OFF" else "مقفولة"} · ${isolateFigures("31°")}").assertExists()
        compose.onRoot().captureRoboImage("build/outputs/roborazzi/$name.png")
    }
    @Test fun homeArabicKraft() = home("home_ar_kraft", ThemeChoice())
    @Test fun homeArabicCleanLight() = home("home_ar_clean_light", ThemeChoice(AppTheme.Clean), dark = false)
    @Test fun homeArabicCleanDark() = home("home_ar_clean_dark", ThemeChoice(AppTheme.Clean), dark = true)
    @Test fun homeArabicNight() = home("home_ar_night", ThemeChoice(AppTheme.Night))
    @Test @Config(qualifiers = "en-rUS-w412dp-h1000dp-mdpi")
    fun homeEnglishKraft() = home("home_en_kraft", ThemeChoice(), english = true)
    @Test fun settingsArabicKraft() {
        compose.setContent { PowerkTheme(ThemeChoice()) {
            CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Rtl) {
                Surface { SettingsScreen(visualSnapshot(), ServerSettings(currency = "EGP", priceKwh = 1.5)) }
            }
        } }
        compose.onNodeWithText("معلومات المشترك").assertExists()
        compose.onNodeWithText("الشكل").assertExists()
        compose.onRoot().captureRoboImage("build/outputs/roborazzi/settings_ar_kraft.png")
    }
}
