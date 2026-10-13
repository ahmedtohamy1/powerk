package com.powerk.app

import android.app.Application
import android.content.Context
import android.content.res.Configuration
import java.util.Locale

class PowerkApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        Server.init(this)
        Lang.init(this)
    }
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        WidgetUpdates.appearanceChanged(this)
    }
}

/** Legacy per-app language also applies to background surfaces. */
fun Context.localized(): Context {
    val language = Lang.code ?: return this
    val config = Configuration(resources.configuration).apply { setLocale(Locale.forLanguageTag(language)) }
    return createConfigurationContext(config)
}
