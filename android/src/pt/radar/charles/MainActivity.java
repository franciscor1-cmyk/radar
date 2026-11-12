package pt.radar.charles;

import android.app.Activity;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.KeyEvent;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Radar Charles para Android TV: abre o painel do PC em ecrã inteiro e
 * passa as teclas do comando diretamente para o painel (função JS act()).
 * Sem PC guardado (ou se falhar), procura sozinho o PC na rede local.
 */
public class MainActivity extends Activity {
    static final int PORT = 3000;
    static final String SETUP = "file:///android_asset/setup.html";

    WebView web;
    SharedPreferences prefs;
    volatile boolean onPanel = false;
    volatile boolean wantScan = false;
    volatile String status = "";
    long lastBack = 0;
    final AtomicBoolean scanning = new AtomicBoolean(false);

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(0x00000080 | 0x00000400); // KEEP_SCREEN_ON | FULLSCREEN
        prefs = getSharedPreferences("radar", 0);

        web = new WebView(this);
        web.setBackgroundColor(0xFF05070D);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        web.addJavascriptInterface(new Bridge(), "Radar");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView v, String url) {
                onPanel = url != null && url.startsWith("http");
                if (onPanel) wantScan = false;
            }

            @Override
            public void onReceivedError(WebView v, int code, String desc, String url) {
                if (url != null && url.startsWith("http")) {
                    showSetup("Não consegui ligar ao PC (" + hostOf(url) + "). A procurar na rede…", true);
                }
            }
        });
        setContentView(web);
        immersive();

        String saved = prefs.getString("url", null);
        if (saved != null) openPanel(saved);
        else showSetup("A procurar o PC com o Radar Charles…", true);
    }

    void immersive() {
        // LAYOUT_STABLE | LAYOUT_HIDE_NAV | LAYOUT_FULLSCREEN | HIDE_NAV | FULLSCREEN | IMMERSIVE_STICKY
        getWindow().getDecorView().setSystemUiVisibility(0x1706);
    }

    void openPanel(String base) {
        wantScan = false;
        prefs.edit().putString("url", base).apply();
        onPanel = false;
        web.loadUrl(base + "/?tv=1");
        web.requestFocus();
    }

    void showSetup(String msg, boolean scan) {
        onPanel = false;
        status = msg;
        web.loadUrl(SETUP);
        if (scan) startScan();
    }

    static String hostOf(String url) {
        String h = url.replaceFirst("^https?://", "");
        int i = h.indexOf('/');
        return i >= 0 ? h.substring(0, i) : h;
    }

    static String normalize(String in) {
        String u = in.trim();
        if (u.isEmpty()) return null;
        if (!u.startsWith("http")) u = "http://" + u;
        u = u.replaceAll("/+$", "");
        if (!u.substring(7).contains(":")) u = u + ":" + PORT;
        return u;
    }

    void js(String code) {
        web.evaluateJavascript(code, null);
    }

    void toast(String msg) {
        Toast.makeText(this, msg, 0).show();
    }

    // ---------- comando da TV ----------
    static String cmdFor(int k) {
        switch (k) {
            case 19: return "up";            // DPAD_UP
            case 20: return "down";          // DPAD_DOWN
            case 21: return "left";          // DPAD_LEFT
            case 22: return "right";         // DPAD_RIGHT
            case 23: case 66: case 160: return "ok"; // DPAD_CENTER, ENTER, NUMPAD_ENTER
            case 4: case 111: return "back"; // BACK, ESCAPE
            case 166: case 92: return "pageup";    // CHANNEL_UP, PAGE_UP
            case 167: case 93: return "pagedown";  // CHANNEL_DOWN, PAGE_DOWN
            case 183: return "red";          // PROG_RED
            case 184: case 85: case 126: case 127: return "green"; // PROG_GREEN, PLAY/PAUSE
            case 185: return "map";          // PROG_YELLOW -> mapa da trajetória
            case 186: case 82: return "help"; // PROG_BLUE, MENU
            case 165: case 176: return "settings"; // INFO, SETTINGS
            default:
                if (k >= 8 && k <= 16) return "city:" + (k - 7);      // teclas 1..9
                if (k >= 145 && k <= 153) return "city:" + (k - 144); // teclado numérico 1..9
                return null;
        }
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent e) {
        if (!onPanel) return super.dispatchKeyEvent(e); // ecrã de configuração: navegação normal
        String cmd = cmdFor(e.getKeyCode());
        if (cmd == null) return super.dispatchKeyEvent(e);
        if (e.getAction() != 0) return true; // só ACTION_DOWN
        int rep = e.getRepeatCount();

        if (cmd.equals("ok") && rep == 15) { // OK carregado ~1,5 s -> escolher PC
            showSetup("Escolhe o PC do Radar Charles", false);
            return true;
        }
        if (rep > 0 && !(cmd.equals("up") || cmd.equals("down") || cmd.equals("left") || cmd.equals("right"))) return true;

        if (cmd.equals("settings")) {
            showSetup("Escolhe o PC do Radar Charles", false);
            return true;
        }
        if (cmd.equals("back")) {
            long now = System.currentTimeMillis();
            if (now - lastBack < 1500) { finish(); return true; }
            lastBack = now;
            toast("Carrega outra vez em Voltar para sair");
        }
        js("window.act&&act('" + cmd + "')");
        return true;
    }

    // ---------- procurar o PC na rede ----------
    void startScan() {
        wantScan = true;
        if (!scanning.compareAndSet(false, true)) return;
        new Thread(new Runnable() {
            public void run() {
                try {
                    while (wantScan) {
                        final String found = scanOnce();
                        if (found != null && wantScan) {
                            status = "Encontrei o PC: " + found;
                            runOnUiThread(new Runnable() { public void run() { openPanel(found); } });
                            return;
                        }
                        if (!wantScan) return;
                        status = "Não encontrei o PC. Confirma que o Radar Charles está aberto no PC, "
                                + "no mesmo Wi-Fi, e que a firewall permite. A tentar outra vez…";
                        Thread.sleep(8000);
                    }
                } catch (InterruptedException ignored) {
                } finally {
                    scanning.set(false);
                }
            }
        }).start();
    }

    String scanOnce() throws InterruptedException {
        String saved = prefs.getString("url", null);
        if (saved != null && isRadar(saved, 1500)) return saved;

        List<String> prefixes = new ArrayList<String>();
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs != null && ifs.hasMoreElements()) {
                NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                Enumeration<InetAddress> as = ni.getInetAddresses();
                while (as.hasMoreElements()) {
                    InetAddress a = as.nextElement();
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) {
                        String ip = a.getHostAddress();
                        String p = ip.substring(0, ip.lastIndexOf('.') + 1);
                        if (!prefixes.contains(p)) prefixes.add(p);
                    }
                }
            }
        } catch (Exception ignored) {}
        if (prefixes.isEmpty()) { status = "A TV não está ligada a nenhuma rede."; return null; }

        final AtomicReference<String> hit = new AtomicReference<String>(null);
        ExecutorService pool = Executors.newFixedThreadPool(48);
        for (final String p : prefixes) {
            status = "A procurar o PC na rede " + p + "x …";
            for (int i = 1; i < 255; i++) {
                final String url = "http://" + p + i + ":" + PORT;
                final String host = p + i;
                pool.execute(new Runnable() {
                    public void run() {
                        if (hit.get() != null) return;
                        Socket sock = new Socket();
                        try {
                            sock.connect(new InetSocketAddress(host, PORT), 350);
                            sock.close();
                            if (isRadar(url, 2000)) hit.compareAndSet(null, url);
                        } catch (Exception ignored) {
                            try { sock.close(); } catch (Exception ignored2) {}
                        }
                    }
                });
            }
        }
        pool.shutdown();
        pool.awaitTermination(30, TimeUnit.SECONDS);
        return hit.get();
    }

    static boolean isRadar(String base, int timeout) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/info").openConnection();
            c.setConnectTimeout(timeout);
            c.setReadTimeout(timeout);
            InputStream in = c.getInputStream();
            byte[] buf = new byte[2048];
            int n = in.read(buf);
            in.close();
            return n > 0 && new String(buf, 0, n, "UTF-8").contains("\"viewers\"");
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    // ---------- ponte para o ecrã de configuração (setup.html) ----------
    public class Bridge {
        @JavascriptInterface
        public String status() { return status; }

        @JavascriptInterface
        public String saved() {
            String s = prefs.getString("url", null);
            return s == null ? "" : s.replaceFirst("^http://", "");
        }

        @JavascriptInterface
        public void connect(String addr) {
            final String u = normalize(addr);
            if (u == null) return;
            wantScan = false;
            status = "A ligar a " + u + " …";
            new Thread(new Runnable() {
                public void run() {
                    if (isRadar(u, 3000)) {
                        runOnUiThread(new Runnable() { public void run() { openPanel(u); } });
                    } else {
                        status = "Não respondeu: " + u + ". Confirma o endereço e a firewall do PC.";
                    }
                }
            }).start();
        }

        @JavascriptInterface
        public void scan() {
            runOnUiThread(new Runnable() { public void run() { startScan(); } });
        }

        @JavascriptInterface
        public void quit() {
            runOnUiThread(new Runnable() { public void run() { finish(); } });
        }
    }
}
