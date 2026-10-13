package com.powerk.app

import android.appwidget.AppWidgetManager
import android.content.Context

class OutletWidget : PowerkWidget() {
    companion object {
        suspend fun update(context: Context, id: Int, message: String? = null) {
            WidgetUpdates.refresh(context)
            WidgetUpdates.render(context, OutletWidget::class.java, id, message)
        }
    }
}
