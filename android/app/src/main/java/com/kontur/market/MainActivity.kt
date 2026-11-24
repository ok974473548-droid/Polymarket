package com.kontur.market

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * Контур рынка для Android: интерфейс сервера Контура (Railway или свой компьютер) в WebView.
 * Сервер по умолчанию — облачный (Prefs.DEFAULT_SERVER); адрес можно сменить (ярлык «Сменить сервер» или экран ошибки).
 * Весь интерфейс как в браузере: рынки, трейдеры, копирование, автопилот, портфель, живые обновления, вход по паролю
 * (cookie сохраняются). Ссылки на другие сайты открываются в браузере. Уведомления — фоновая проверка (NotifyWorker).
 */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var setup: View
    private lateinit var error: View
    private lateinit var progress: ProgressBar
    private lateinit var address: EditText
    private lateinit var setupError: TextView
    private lateinit var connect: Button
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        web = findViewById(R.id.web)
        setup = findViewById(R.id.setup)
        error = findViewById(R.id.error)
        progress = findViewById(R.id.progress)
        address = findViewById(R.id.address)
        setupError = findViewById(R.id.setup_error)
        connect = findViewById(R.id.connect)

        configureWeb()
        connect.setOnClickListener { tryConnect() }
        address.setOnEditorActionListener { _, id, _ -> if (id == EditorInfo.IME_ACTION_DONE) { tryConnect(); true } else false }
        findViewById<Button>(R.id.retry).setOnClickListener { Prefs.server(this)?.let { open(it) } }
        findViewById<Button>(R.id.change).setOnClickListener { showSetup() }

        NotifyWorker.createChannel(this)
        NotifyWorker.schedule(this)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        if (savedInstanceState != null && Prefs.server(this) != null) {
            web.restoreState(savedInstanceState)
            showOnly(web)
        } else {
            route(intent)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        route(intent)
    }

    /** Куда открыть: смена сервера (ярлык), экран из уведомления (link), иначе — главная. */
    private fun route(i: Intent?) {
        val base = Prefs.server(this)
        when {
            i?.action == ACTION_CHANGE_SERVER || base == null -> showSetup()
            i?.getStringExtra(EXTRA_LINK) != null -> open(base + i.getStringExtra(EXTRA_LINK)!!.let { if (it.startsWith("/")) it else "/$it" })
            web.url == null -> open(base)
            else -> showOnly(web)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWeb() {
        val s = web.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        s.loadWithOverviewMode = true
        s.useWideViewPort = true
        s.setSupportZoom(false)
        s.userAgentString = s.userAgentString + " KonturAndroid/" + VERSION
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.addJavascriptInterface(Bridge(), "KonturAndroid")
        web.webChromeClient = WebChromeClient() // окна подтверждения (confirm/alert) — системные
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
                val base = Prefs.server(this@MainActivity) ?: return false
                val url = req.url
                if (url.host == Uri.parse(base).host && url.port == Uri.parse(base).port) return false
                // Polymarket, новости, сайты источников — во внешнем браузере
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, url)) }
                return true
            }

            override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
                progress.visibility = View.VISIBLE
            }

            override fun onPageFinished(view: WebView, url: String?) {
                progress.visibility = View.GONE
                CookieManager.getInstance().flush()
            }

            override fun onReceivedError(view: WebView, req: WebResourceRequest, err: WebResourceError) {
                if (req.isForMainFrame) {
                    progress.visibility = View.GONE
                    showError("${err.description}\n${Prefs.server(this@MainActivity) ?: ""}")
                }
            }
        }
    }

    private fun open(url: String) {
        showOnly(web)
        web.loadUrl(url)
    }

    private fun showOnly(v: View) {
        for (x in listOf<View>(web, setup, error)) x.visibility = if (x === v) View.VISIBLE else View.GONE
    }

    private fun showSetup() {
        address.setText(Prefs.server(this) ?: "")
        setupError.visibility = View.GONE
        showOnly(setup)
    }

    private fun showError(text: String) {
        findViewById<TextView>(R.id.error_text).text = text
        showOnly(error)
    }

    /** Проверка связи: сервер Контура отвечает на /api/health {"ok": true}. */
    private fun tryConnect() {
        val url = Prefs.normalize(address.text.toString())
        if (url == null) {
            setupError.text = "Введите адрес, например https://….up.railway.app или http://192.168.1.10:8787"
            setupError.visibility = View.VISIBLE
            return
        }
        connect.isEnabled = false
        connect.setText(R.string.setup_checking)
        setupError.visibility = View.GONE
        thread {
            val problem = try {
                val c = URL("$url/api/health").openConnection() as HttpURLConnection
                c.connectTimeout = 10000
                c.readTimeout = 10000
                val code = c.responseCode
                val ok = code == 200 && c.inputStream.bufferedReader().use { it.readText() }.contains("\"ok\"")
                c.disconnect()
                if (ok) null else "Сервер ответил кодом $code — это Контур рынка?"
            } catch (e: Exception) {
                "Нет связи с $url: ${e.javaClass.simpleName}. Адрес верный? Для компьютера дома телефон должен быть в той же Wi-Fi сети."
            }
            main.post {
                connect.isEnabled = true
                connect.setText(R.string.setup_connect)
                if (problem == null) {
                    (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager)
                        .hideSoftInputFromWindow(address.windowToken, 0)
                    Prefs.setServer(this, url)
                    web.clearHistory()
                    open(url)
                } else {
                    setupError.text = problem
                    setupError.visibility = View.VISIBLE
                }
            }
        }
    }

    @Deprecated("Системная кнопка «назад»")
    override fun onBackPressed() {
        when {
            setup.visibility == View.VISIBLE && Prefs.server(this) != null && web.url != null -> showOnly(web)
            web.visibility == View.VISIBLE && web.canGoBack() -> web.goBack()
            else -> @Suppress("DEPRECATION") super.onBackPressed()
        }
    }

    override fun onResume() {
        super.onResume()
        foreground = true
        web.onResume()
    }

    override fun onPause() {
        foreground = false
        web.onPause()
        CookieManager.getInstance().flush()
        super.onPause()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    /** Мост для веб-интерфейса (window.KonturAndroid). */
    inner class Bridge {
        @JavascriptInterface
        fun info(): String = JSONObject().put("version", VERSION).put("notifications", "background").toString()

        @JavascriptInterface
        fun changeServer() {
            main.post { showSetup() }
        }

        /** Сохранить текст в «Загрузки» (WebView не скачивает blob-ссылки). Возвращает сообщение для пользователя. */
        @JavascriptInterface
        fun saveFile(name: String, mime: String, text: String): String = try {
            val safe = name.replace(Regex("[^\\w.\\-]"), "_")
            if (Build.VERSION.SDK_INT >= 29) {
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, safe)
                    put(MediaStore.Downloads.MIME_TYPE, mime)
                }
                val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                    ?: throw IllegalStateException("нет доступа к «Загрузкам»")
                contentResolver.openOutputStream(uri)!!.use { it.write(text.toByteArray(Charsets.UTF_8)) }
                "Отчёт сохранён в «Загрузки»: $safe"
            } else {
                val dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)!!
                File(dir, safe).writeText(text, Charsets.UTF_8)
                "Отчёт сохранён: ${dir.absolutePath}/$safe"
            }
        } catch (e: Exception) {
            "Не удалось сохранить отчёт: ${e.message}"
        }
    }

    companion object {
        const val EXTRA_LINK = "link"
        const val ACTION_CHANGE_SERVER = "com.kontur.market.CHANGE_SERVER"
        val VERSION: String = BuildConfig.VERSION_NAME

        @Volatile
        var foreground = false
    }
}
