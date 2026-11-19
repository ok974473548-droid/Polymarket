package com.whalefollow.app

import android.content.Context

/** Настройки приложения: адрес сервера и какие уведомления уже показаны. */
object Prefs {
    private const val FILE = "whalefollow"
    private const val SERVER = "server"
    private const val SEEN = "seen_ids"
    private const val SEEN_READY = "seen_ready"
    private const val MAX_SEEN = 300

    fun server(ctx: Context): String? = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(SERVER, null)

    fun setServer(ctx: Context, url: String) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
            .putString(SERVER, url)
            .remove(SEEN).putBoolean(SEEN_READY, false) // другой сервер — свои уведомления
            .apply()
    }

    /** «192.168.1.10:8787» → «http://192.168.1.10:8787»; без «/» в конце. Пусто или мусор — null. */
    fun normalize(raw: String): String? {
        var s = raw.trim().trimEnd('/')
        if (s.isEmpty()) return null
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "http://$s"
        val host = s.substringAfter("://").substringBefore('/').substringBefore(':')
        if (host.isEmpty() || host.contains(' ')) return null
        return s
    }

    fun seen(ctx: Context): MutableList<String> =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(SEEN, "")!!
            .split('\n').filter { it.isNotEmpty() }.toMutableList()

    fun seenReady(ctx: Context): Boolean = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getBoolean(SEEN_READY, false)

    fun saveSeen(ctx: Context, ids: List<String>) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
            .putString(SEEN, ids.takeLast(MAX_SEEN).joinToString("\n"))
            .putBoolean(SEEN_READY, true)
            .apply()
    }
}
