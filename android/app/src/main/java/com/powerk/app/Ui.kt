package com.powerk.app

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularWavyProgressIndicator
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.ShortNavigationBar
import androidx.compose.material3.ShortNavigationBarItem
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import kotlin.math.roundToInt

@Composable
fun PowerkApp() {
    var tab by remember { mutableIntStateOf(0) }
    var snapshot by remember { mutableStateOf<Snapshot?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    var showSplash by remember { mutableStateOf(true) }
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    // "mac:outlet" -> state we are switching to right now (optimistic UI while the strip answers)
    val pending = remember { mutableStateMapOf<String, Boolean>() }

    suspend fun refresh() {
        if (!Server.configured) return
        loading = true
        try {
            snapshot = Api.snapshot(Server.base, Server.token)
            error = null
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            error = e.message ?: e.javaClass.simpleName
            snapshot = null
        } finally {
            loading = false
        }
    }

    LaunchedEffect(Unit) {
        delay(1100)
        showSplash = false
    }

    LaunchedEffect(lifecycle, Server.host, Server.port, Server.token, Server.secure) {
        snapshot = null
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                refresh()
                delay(2000)
            }
        }
    }

    fun command(mac: String, outlet: Int, on: Boolean) {
        val keys = if (outlet == 0) (1..4).map { "$mac:$it" } else listOf("$mac:$outlet")
        if (keys.any { pending.containsKey(it) }) return
        keys.forEach { pending[it] = on }
        scope.launch {
            try {
                Api.setOutlet(Server.base, Server.token, mac, outlet, on)
                refresh()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                snackbar.showSnackbar(e.message ?: "command failed")
            } finally {
                keys.forEach { pending.remove(it) }
            }
        }
    }

    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
        Scaffold(
            containerColor = MaterialTheme.colorScheme.background,
            contentColor = MaterialTheme.colorScheme.onBackground,
            snackbarHost = { SnackbarHost(snackbar) },
            bottomBar = {
                ShortNavigationBar {
                    ShortNavigationBarItem(
                        selected = tab == 0,
                        onClick = { tab = 0 },
                        icon = { Icon(Icons.Filled.Home, contentDescription = null) },
                        label = { Text(stringResource(R.string.tab_plugs)) },
                    )
                    ShortNavigationBarItem(
                        selected = tab == 1,
                        onClick = { tab = 1 },
                        icon = { Icon(Icons.Filled.Settings, contentDescription = null) },
                        label = { Text(stringResource(R.string.tab_settings)) },
                    )
                }
            },
        ) { padding ->
            AnimatedContent(
                targetState = tab,
                transitionSpec = {
                    val forward = targetState > initialState
                    val shift = if (forward) 1 else -1
                    (fadeIn(tween(240)) + slideInVertically(tween(280)) { it / 10 * shift }) togetherWith
                        (fadeOut(tween(140)) + slideOutVertically(tween(200)) { -it / 14 * shift })
                },
                label = "tab",
                modifier = Modifier.fillMaxSize().padding(padding),
            ) { current ->
                if (current == 0) {
                    PlugsScreen(
                        snapshot = snapshot,
                        error = error,
                        loading = loading,
                        pending = pending,
                        onRefresh = { scope.launch { refresh() } },
                        onOpenSettings = { tab = 1 },
                        onCommand = { mac, outlet, on -> command(mac, outlet, on) },
                    )
                } else {
                    SettingsScreen()
                }
            }
        }

        AnimatedVisibility(
            visible = showSplash,
            exit = fadeOut(tween(420)) + scaleOut(tween(420), targetScale = 1.06f),
        ) { Splash() }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun Splash() {
    val appear = remember { Animatable(0f) }
    LaunchedEffect(Unit) { appear.animateTo(1f, spring(dampingRatio = 0.55f, stiffness = Spring.StiffnessLow)) }
    val scheme = MaterialTheme.colorScheme

    Box(
        Modifier
            .fillMaxSize()
            .background(Brush.verticalGradient(listOf(scheme.primaryContainer, scheme.background))),
        contentAlignment = Alignment.Center,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Box(
                Modifier
                    .size(150.dp)
                    .graphicsLayer {
                        val p = appear.value
                        scaleX = 0.62f + 0.38f * p
                        scaleY = 0.62f + 0.38f * p
                        alpha = p
                        rotationZ = -28f * (1f - p)
                    },
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    painter = painterResource(R.drawable.ic_launcher_foreground),
                    contentDescription = null,
                    modifier = Modifier.size(150.dp),
                    tint = Color.Unspecified,
                )
            }
            Spacer(Modifier.height(18.dp))
            Text(
                stringResource(R.string.app_name),
                style = MaterialTheme.typography.displaySmall,
                fontWeight = FontWeight.SemiBold,
                color = scheme.onSurface,
                modifier = Modifier.graphicsLayer { alpha = appear.value },
            )
            Text(
                stringResource(R.string.splash_tagline),
                style = MaterialTheme.typography.bodyMedium,
                color = scheme.onSurfaceVariant,
                modifier = Modifier.graphicsLayer { alpha = appear.value },
            )
            Spacer(Modifier.height(38.dp))
            CircularWavyProgressIndicator(Modifier.size(30.dp))
        }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun PlugsScreen(
    snapshot: Snapshot?,
    error: String?,
    loading: Boolean,
    pending: Map<String, Boolean>,
    onRefresh: () -> Unit,
    onOpenSettings: () -> Unit,
    onCommand: (String, Int, Boolean) -> Unit,
) {
    val strips = snapshot?.strips.orEmpty()

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(start = 18.dp, end = 18.dp, top = 20.dp, bottom = 36.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        item {
            Header(
                stripCount = strips.size,
                onlineCount = strips.count { it.online },
                configured = Server.configured,
                loading = loading,
                onRefresh = onRefresh,
            )
        }

        item {
            AnimatedVisibility(
                visible = error != null,
                enter = fadeIn() + slideInVertically { -it / 3 },
                exit = fadeOut(),
            ) {
                MessageCard(error ?: "", isError = true, icon = Icons.Filled.Warning)
            }
        }

        if (!Server.configured) {
            item {
                EmptyState(
                    title = stringResource(R.string.no_server_title),
                    body = stringResource(R.string.no_server_body),
                    action = stringResource(R.string.open_settings),
                    onAction = onOpenSettings,
                )
            }
        } else {
            items(strips, key = { it.mac }) { strip ->
                StripCard(
                    strip = strip,
                    showMac = strips.size > 1,
                    pending = pending,
                    onCommand = onCommand,
                    modifier = Modifier.animateItem(),
                )
            }

            if (snapshot != null && strips.isEmpty()) {
                item {
                    EmptyState(
                        title = stringResource(R.string.no_strip_title),
                        body = stringResource(R.string.no_strip_body),
                    )
                }
            }

            if (snapshot == null && error == null) {
                item {
                    Column(
                        Modifier.fillMaxWidth().padding(top = 60.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        CircularWavyProgressIndicator(Modifier.size(34.dp))
                        Spacer(Modifier.height(14.dp))
                        Text(
                            stringResource(R.string.connecting, Server.host),
                            style = MaterialTheme.typography.bodyMedium,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun Header(
    stripCount: Int,
    onlineCount: Int,
    configured: Boolean,
    loading: Boolean,
    onRefresh: () -> Unit,
) {
    val spin = rememberInfiniteTransition(label = "refresh")
    val angle by spin.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(900, easing = LinearEasing)),
        label = "angle",
    )
    Row(verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(
                stringResource(R.string.app_name),
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.SemiBold,
            )
            val subtitle = when {
                !configured -> stringResource(R.string.header_no_server)
                onlineCount > 0 -> stringResource(R.string.header_online, onlineCount, Server.host)
                stripCount > 0 -> stringResource(R.string.header_known_offline, stripCount)
                else -> stringResource(R.string.header_waiting, Server.host)
            }
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        IconButton(onClick = onRefresh, enabled = configured && !loading) {
            Icon(
                Icons.Filled.Refresh,
                contentDescription = stringResource(R.string.refresh),
                modifier = Modifier.rotate(if (loading) angle else 0f),
            )
        }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun StripCard(
    strip: Strip,
    showMac: Boolean,
    pending: Map<String, Boolean>,
    onCommand: (String, Int, Boolean) -> Unit,
    modifier: Modifier = Modifier,
) {
    val stateOf: (Int) -> Boolean = { n -> pending["${strip.mac}:$n"] ?: strip.outletOn(n) }
    val anyOn = (1..4).any(stateOf)
    val allPending = (1..4).any { pending.containsKey("${strip.mac}:$it") }

    ElevatedCard(modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {

            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(online = strip.online)
                Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    val title = stringResource(R.string.strip_name) +
                        if (showMac) " · ${strip.mac.takeLast(7)}" else ""
                    Text(title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
                    Text(
                        stringResource(R.string.strip_fw, strip.fw),
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                PowerRing(watts = strip.powerW, enabled = strip.online)
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    MetricPill("${strip.energyKwh} kWh")
                    strip.voltage?.let { MetricPill("${it.roundToInt()} V") }
                    strip.currentA?.let { MetricPill("$it A") }
                    strip.rssi?.let { MetricPill("$it dBm") }
                }
            }

            for (row in 0 until 2) {
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    for (column in 0 until 2) {
                        val outlet = strip.outlets.getOrNull(row * 2 + column)
                        if (outlet == null) {
                            Spacer(Modifier.weight(1f))
                        } else {
                            OutletTile(
                                outlet = outlet,
                                on = stateOf(outlet.n),
                                pending = pending.containsKey("${strip.mac}:${outlet.n}"),
                                enabled = strip.online,
                                onToggle = { onCommand(strip.mac, outlet.n, it) },
                                modifier = Modifier.weight(1f),
                            )
                        }
                    }
                }
            }

            FilledTonalButton(
                onClick = { onCommand(strip.mac, 0, !anyOn) },
                modifier = Modifier.fillMaxWidth().height(52.dp),
                enabled = strip.online && !allPending,
            ) {
                if (allPending) {
                    CircularWavyProgressIndicator(Modifier.size(22.dp))
                    Spacer(Modifier.width(10.dp))
                }
                Text(
                    stringResource(if (anyOn) R.string.turn_all_off else R.string.turn_all_on),
                    fontWeight = FontWeight.SemiBold,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun OutletTile(
    outlet: Outlet,
    on: Boolean,
    pending: Boolean,
    enabled: Boolean,
    onToggle: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
) {
    val scheme = MaterialTheme.colorScheme
    val haptics = LocalHapticFeedback.current
    val progress by animateFloatAsState(
        targetValue = if (on) 1f else 0f,
        animationSpec = spring(dampingRatio = 0.55f, stiffness = Spring.StiffnessMediumLow),
        label = "toggle",
    )
    val container = lerp(scheme.surfaceContainerHighest, scheme.primaryContainer, progress)
    val content = lerp(scheme.onSurface, scheme.onPrimaryContainer, progress)

    Surface(
        onClick = {
            haptics.performHapticFeedback(HapticFeedbackType.LongPress)
            onToggle(!on)
        },
        enabled = enabled && !pending,
        shape = MaterialTheme.shapes.large,
        color = container,
        contentColor = content,
        modifier = modifier
            .heightIn(min = 96.dp)
            .graphicsLayer {
                val pop = 1f + 0.015f * progress
                scaleX = pop
                scaleY = pop
            },
    ) {
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    stringResource(R.string.outlet_n, outlet.n),
                    style = MaterialTheme.typography.labelLarge,
                    modifier = Modifier.weight(1f),
                )
                // while the strip is answering, the switch becomes a spinner
                AnimatedContent(
                    targetState = pending,
                    transitionSpec = { fadeIn(tween(160)) togetherWith fadeOut(tween(120)) },
                    label = "switch-pending",
                ) { isPending ->
                    if (isPending) {
                        CircularWavyProgressIndicator(Modifier.size(22.dp))
                    } else {
                        MiniSwitch(progress = progress, onColor = scheme.primary)
                    }
                }
            }
            Text(
                stringResource(if (on) R.string.state_on else R.string.state_off),
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold,
            )
            Text(
                if (on) {
                    stringResource(R.string.tile_on_detail, outlet.powerW.toString(), outlet.tempC)
                } else {
                    stringResource(R.string.tile_off_detail, outlet.tempC)
                },
                style = MaterialTheme.typography.labelSmall,
            )
        }
    }
}

/** Small animated switch drawn inside an outlet tile: track + sliding thumb. */
@Composable
private fun MiniSwitch(progress: Float, onColor: Color) {
    val scheme = MaterialTheme.colorScheme
    val track = lerp(scheme.outlineVariant, onColor, progress)
    val thumb = lerp(scheme.surfaceContainerLowest, scheme.onPrimary, progress)

    Box(
        Modifier
            .size(width = 38.dp, height = 22.dp)
            .clip(CircleShape)
            .background(track),
    ) {
        Box(
            Modifier
                .offset(x = (2 + 16 * progress).dp, y = 2.dp)
                .size(18.dp)
                .clip(CircleShape)
                .background(thumb),
        )
    }
}

@Composable
private fun PowerRing(watts: Double, enabled: Boolean) {
    val target = (watts / 2000.0).coerceIn(0.0, 1.0).toFloat()
    val sweep by animateFloatAsState(
        targetValue = target,
        animationSpec = spring(dampingRatio = 0.7f, stiffness = Spring.StiffnessLow),
        label = "sweep",
    )
    val scheme = MaterialTheme.colorScheme
    val track = scheme.surfaceContainerHighest
    val arc = if (enabled && watts > 0.05) scheme.primary else scheme.outlineVariant

    Box(Modifier.size(142.dp), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val stroke = 12.dp.toPx()
            val inset = stroke / 2f
            val arcSize = Size(size.width - stroke, size.height - stroke)
            drawArc(
                color = track,
                startAngle = 135f,
                sweepAngle = 270f,
                useCenter = false,
                topLeft = Offset(inset, inset),
                size = arcSize,
                style = Stroke(width = stroke, cap = StrokeCap.Round),
            )
            drawArc(
                color = arc,
                startAngle = 135f,
                sweepAngle = 270f * sweep,
                useCenter = false,
                topLeft = Offset(inset, inset),
                size = arcSize,
                style = Stroke(width = stroke, cap = StrokeCap.Round),
            )
        }
        // fixed-width inner column keeps the numbers clear of the ring stroke
        Column(
            Modifier.width(92.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Row(verticalAlignment = Alignment.Bottom) {
                Text(
                    "${watts.roundToInt()}",
                    style = MaterialTheme.typography.displaySmall,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1,
                    softWrap = false,
                )
                Spacer(Modifier.width(3.dp))
                Text(
                    stringResource(R.string.unit_watts),
                    style = MaterialTheme.typography.labelLarge,
                    color = scheme.onSurfaceVariant,
                    modifier = Modifier.padding(bottom = 6.dp),
                )
            }
            Text(
                stringResource(R.string.watts_now),
                style = MaterialTheme.typography.labelSmall,
                color = scheme.onSurfaceVariant,
                maxLines = 1,
            )
        }
    }
}

@Composable
private fun MetricPill(value: String) {
    Surface(
        shape = CircleShape,
        color = MaterialTheme.colorScheme.surfaceContainerHighest,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
    ) {
        CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Ltr) {
            Text(
                value,
                Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
                style = MaterialTheme.typography.labelLarge,
                maxLines = 1,
                softWrap = false,
            )
        }
    }
}

@Composable
private fun StatusDot(online: Boolean) {
    val pulse = rememberInfiniteTransition(label = "dot")
    val alpha by pulse.animateFloat(
        initialValue = 0.35f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse),
        label = "alpha",
    )
    Box(
        Modifier
            .size(11.dp)
            .graphicsLayer { this.alpha = if (online) 1f else alpha }
            .background(
                if (online) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error,
                CircleShape,
            )
    )
}

@Composable
private fun MessageCard(text: String, isError: Boolean, icon: androidx.compose.ui.graphics.vector.ImageVector? = null) {
    Card(
        Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = if (isError) MaterialTheme.colorScheme.errorContainer
            else MaterialTheme.colorScheme.surfaceContainerHighest,
            contentColor = if (isError) MaterialTheme.colorScheme.onErrorContainer
            else MaterialTheme.colorScheme.onSurface,
        ),
    ) {
        Row(
            Modifier.padding(16.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (icon != null) Icon(icon, contentDescription = null)
            Text(text, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
        }
    }
}

@Composable
private fun EmptyState(title: String, body: String, action: String? = null, onAction: (() -> Unit)? = null) {
    Column(
        Modifier.fillMaxWidth().padding(top = 40.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Surface(shape = CircleShape, color = MaterialTheme.colorScheme.surfaceContainerHighest) {
            Icon(
                Icons.Filled.Info,
                contentDescription = null,
                modifier = Modifier.padding(18.dp).size(30.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(16.dp))
        Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(6.dp))
        Text(
            body,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(horizontal = 12.dp),
        )
        if (action != null && onAction != null) {
            Spacer(Modifier.height(18.dp))
            Button(onClick = onAction) { Text(action) }
        }
    }
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
private fun SettingsScreen() {
    var host by remember { mutableStateOf(Server.host) }
    var port by remember { mutableStateOf(Server.port.toString()) }
    var token by remember { mutableStateOf(Server.token) }
    var secure by remember { mutableStateOf(Server.secure) }
    var status by remember { mutableStateOf<String?>(null) }
    var statusOk by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var lang by remember { mutableStateOf(Lang.code) }
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    val resources = LocalResources.current

    fun commit() = Server.save(host, port.toIntOrNull() ?: 8080, token, secure)

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(
            stringResource(R.string.settings_title),
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.SemiBold,
        )

        ElevatedCard(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                Text(
                    stringResource(R.string.language_title),
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                )
                val options = listOf(
                    null to stringResource(R.string.lang_system),
                    "en" to stringResource(R.string.lang_english),
                    "ar" to stringResource(R.string.lang_arabic),
                )
                SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                    options.forEachIndexed { index, (value, label) ->
                        SegmentedButton(
                            selected = lang == value,
                            onClick = {
                                if (lang != value) {
                                    lang = value
                                    Lang.set(context, value)
                                }
                            },
                            shape = SegmentedButtonDefaults.itemShape(index = index, count = options.size),
                        ) { Text(label) }
                    }
                }
            }
        }

        ElevatedCard(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                Text(
                    stringResource(R.string.server_title),
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                )
                Text(
                    stringResource(R.string.server_body),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                // technical values stay left-to-right even in RTL locales
                CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Ltr) {
                    OutlinedTextField(
                        value = host,
                        onValueChange = { host = it },
                        label = { Text(stringResource(R.string.field_host)) },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        OutlinedTextField(
                            value = port,
                            onValueChange = { port = it.filter(Char::isDigit).take(5) },
                            label = { Text(stringResource(R.string.field_port)) },
                            singleLine = true,
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                            modifier = Modifier.weight(1f),
                        )
                        OutlinedTextField(
                            value = token,
                            onValueChange = { token = it },
                            label = { Text(stringResource(R.string.field_token)) },
                            visualTransformation = PasswordVisualTransformation(),
                            singleLine = true,
                            modifier = Modifier.weight(1.4f),
                        )
                    }
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(stringResource(R.string.use_https), Modifier.weight(1f))
                    Switch(checked = secure, onCheckedChange = { secure = it })
                }
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Button(
                        onClick = {
                            commit()
                            status = resources.getString(R.string.saved)
                            statusOk = true
                        },
                        modifier = Modifier.weight(1f),
                    ) { Text(stringResource(R.string.save)) }
                    FilledTonalButton(
                        enabled = !busy,
                        onClick = {
                            commit()
                            busy = true
                            status = null
                            scope.launch {
                                try {
                                    val snap = Api.snapshot(Server.base, Server.token)
                                    status = resources.getString(
                                        R.string.connected_summary,
                                        snap.strips.size,
                                        snap.strips.count { it.online },
                                    )
                                    statusOk = true
                                } catch (e: CancellationException) {
                                    throw e
                                } catch (e: Exception) {
                                    status = e.message ?: "connection failed"
                                    statusOk = false
                                }
                                busy = false
                            }
                        },
                        modifier = Modifier.weight(1f),
                    ) { Text(stringResource(R.string.test)) }
                }
                if (busy) CircularWavyProgressIndicator(Modifier.fillMaxWidth())
                AnimatedVisibility(visible = status != null, enter = fadeIn(), exit = fadeOut()) {
                    Surface(
                        shape = MaterialTheme.shapes.medium,
                        color = if (statusOk) MaterialTheme.colorScheme.primaryContainer
                        else MaterialTheme.colorScheme.errorContainer,
                        contentColor = if (statusOk) MaterialTheme.colorScheme.onPrimaryContainer
                        else MaterialTheme.colorScheme.onErrorContainer,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(status ?: "", Modifier.padding(14.dp), style = MaterialTheme.typography.bodyMedium)
                    }
                }
                OutlinedButton(
                    onClick = {
                        commit()
                        context.startActivity(
                            android.content.Intent(
                                android.content.Intent.ACTION_VIEW,
                                android.net.Uri.parse(Server.base),
                            )
                        )
                    },
                    modifier = Modifier.fillMaxWidth(),
                    enabled = Server.configured,
                ) { Text(stringResource(R.string.open_web_ui)) }
            }
        }

        ElevatedCard(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(
                    stringResource(R.string.setup_title),
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold,
                )
                SetupStep(1, stringResource(R.string.setup_step_1))
                SetupStep(2, stringResource(R.string.setup_step_2))
                SetupStep(3, stringResource(R.string.setup_step_3))
                CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Ltr) {
                    Text(
                        "python powerk.py provision --ip <ip> --ssid WIFI --password PW",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(start = 30.dp),
                    )
                }
                SetupStep(4, stringResource(R.string.setup_step_4))
                HorizontalDivider(Modifier.padding(vertical = 4.dp))
                Text(
                    stringResource(R.string.setup_footer),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun SetupStep(number: Int, text: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Surface(
            shape = CircleShape,
            color = MaterialTheme.colorScheme.primaryContainer,
            contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
            modifier = Modifier.size(22.dp),
        ) {
            Box(contentAlignment = Alignment.Center) {
                Text("$number", style = MaterialTheme.typography.labelSmall, fontWeight = FontWeight.Bold)
            }
        }
        Text(text, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
    }
}
