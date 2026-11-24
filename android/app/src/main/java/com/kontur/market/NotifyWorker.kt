package com.kontur.market

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.webkit.CookieManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

/**
 * Фоновая проверка уведомлений Контура (Android запускает её примерно раз в 15 минут, когда есть сеть):
 * GET /api/notifications — скопированные сделки, заявки, замены в автопилоте. Вход — тот же, что в приложении
 * (cookie из WebView). Первый запуск только запоминает старые уведомления, чтобы не показать их пачкой.
 */
class NotifyWorker(ctx: Context, params: WorkerParameters) : Worker(ctx, params) {

    override fun doWork(): Result {
        val base = Prefs.server(applicationContext) ?: return Result.success()
        return try {
            val items = fetch(base) ?: return Result.success()
            val seen = Prefs.seen(applicationContext)
            val ready = Prefs.seenReady(applicationContext)
            // новые — от старых к свежим, не больше 5 за раз (остальное — в колокольчике приложения)
            val fresh = items.filter { it.unread && it.id !in seen }.reversed()
            if (ready && !MainActivity.foreground) {
                fresh.takeLast(5).forEach { show(applicationContext, base, it) }
            }
            seen.addAll(items.map { it.id }.filter { it !in seen })
            Prefs.saveSeen(applicationContext, seen)
            Result.success()
        } catch (e: Exception) {
            Result.retry()
        }
    }

    data class Item(val id: String, val title: String, val body: String, val link: String, val unread: Boolean)

    private fun fetch(base: String): List<Item>? {
        val conn = URL("$base/api/notifications").openConnection() as HttpURLConnection
        conn.connectTimeout = 10_000
        conn.readTimeout = 15_000
        CookieManager.getInstance().getCookie(base)?.let { conn.setRequestProperty("Cookie", it) }
        conn.setRequestProperty("Accept", "application/json")
        try {
            if (conn.responseCode != 200) return null // не вошли или сервер недоступен — молча ждём
            val body = conn.inputStream.bufferedReader().use { it.readText() }
            val arr = JSONObject(body).optJSONArray("rows") ?: return emptyList()
            return (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                Item(
                    o.optString("id"),
                    o.optString("title"),
                    o.optString("body"),
                    o.optString("link", "/").ifEmpty { "/" },
                    o.optInt("read", 0) == 0,
                )
            }
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        const val CHANNEL = "kontur"
        private const val WORK = "kontur-notify"

        fun schedule(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<NotifyWorker>(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(WORK, ExistingPeriodicWorkPolicy.KEEP, req)
        }

        fun createChannel(ctx: Context) {
            val ch = NotificationChannel(CHANNEL, ctx.getString(R.string.channel_name), NotificationManager.IMPORTANCE_HIGH)
            ch.description = ctx.getString(R.string.channel_desc)
            ctx.getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }

        fun show(ctx: Context, base: String, it: Item) {
            if (Build.VERSION.SDK_INT >= 33 &&
                ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
            ) {
                return
            }
            val open = Intent(ctx, MainActivity::class.java)
                .putExtra(MainActivity.EXTRA_LINK, it.link)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            val pi = PendingIntent.getActivity(
                ctx, it.id.hashCode(), open, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val n = NotificationCompat.Builder(ctx, CHANNEL)
                .setSmallIcon(R.drawable.ic_notify)
                .setColor(ContextCompat.getColor(ctx, R.color.brand))
                .setContentTitle(it.title)
                .setContentText(it.body)
                .setStyle(NotificationCompat.BigTextStyle().bigText(it.body))
                .setAutoCancel(true)
                .setContentIntent(pi)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .build()
            NotificationManagerCompat.from(ctx).notify(it.id.hashCode(), n)
        }
    }
}
