package com.powerk.app

import android.os.Bundle
import android.content.Intent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.setValue
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge

class MainActivity : ComponentActivity() {
    private var requestedTab by mutableIntStateOf(0)
    private var navigationRequest by mutableIntStateOf(0)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        Server.init(this)
        Lang.init(this)
        Lang.applyStored(this)
        requestedTab = if (intent.getStringExtra(EXTRA_TAB) == "energy") 2 else 0
        setContent {
            PowerkTheme {
                PowerkApp(requestedTab, navigationRequest)
            }
        }
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        requestedTab = if (intent.getStringExtra(EXTRA_TAB) == "energy") 2 else 0
        navigationRequest++
    }
    companion object { const val EXTRA_TAB = "com.powerk.app.TAB" }
}
