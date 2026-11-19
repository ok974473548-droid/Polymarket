package com.whalefollow.app

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
 * WhaleFollow для Android: экран сервера WhaleFollow (компьютер с Docker или VPS) в WebView.
 * Первый запуск — ввод адреса сервера и проверка связи; дальше — весь интерфейс: сигналы, события, ставки, живые
 * обновления, вход по паролю (cookie сохраняются). Ссылки на другие сайты открываются в браузере.
 * Уведомления приходят из фоновой проверки (NotifyWorker). Мост WFAndroid для веб-интерфейса: сохранить отчёт,
 * сменить сервер, версия.
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
        s.userAgentString = s.userAgentString + " WhaleFollowAndroid/" + VERSION
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.addJavascriptInterface(Bridge(), "WFAndroid")
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

    /** Проверка связи: сервер WhaleFollow отвечает на /api/status (200, или 401 — если включён пароль). */
    private fun tryConnect() {
        val url = Prefs.normalize(address.text.toString())
        if (url == null) {
            setupError.text = "Введите адрес, например http://192.168.1.10:8787"
            setupError.visibility = View.VISIBLE
            return
        }
        connect.isEnabled = false
        connect.setText(R.string.setup_checking)
        setupError.visibility = View.GONE
        thread {
            val problem = try {
                val c = URL("$url/api/status").openConnection() as HttpURLConnection
                c.connectTimeout = 7000
                c.readTimeout = 7000
                val code = c.responseCode
                c.disconnect()
                if (code == 200 || code == 401 || code == 403) null else "Сервер ответил кодом $code — это WhaleFollow?"
            } catch (e: Exception) {
                "Нет связи с $url: ${e.javaClass.simpleName}. Телефон в той же сети? В .env сервера — WF_BIND=0.0.0.0?"
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

    /** Мост для веб-интерфейса (window.WFAndroid). */
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
        const val ACTION_CHANGE_SERVER = "com.whalefollow.app.CHANGE_SERVER"
        val VERSION: String = BuildConfig.VERSION_NAME

        @Volatile
        var foreground = false
    }
}
