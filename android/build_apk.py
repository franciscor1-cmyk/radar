"""Constrói RadarCharles.apk SEM o Android SDK.

Peças usadas:
  - Java 17 portátil (Microsoft)          -> javac, keytool, jarsigner
  - R8/D8 (open-source, licença BSD)       -> .class -> classes.dex
  - este script                            -> AndroidManifest.xml binário + resources.arsc
As classes android.* abaixo são só "moldes" para compilar: na TV usam-se as verdadeiras.

Uso:  python android/build_apk.py
"""
import os
import shutil
import struct
import subprocess
import sys
import zipfile

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(os.path.expanduser("~"), ".radarcharles-build")  # fora da AppData (o Python da Store não a vê)
JDK = os.path.join(TOOLS, "jdk", "bin")
R8 = os.path.join(TOOLS, "r8.jar")
BUILD = os.path.join(TOOLS, "build")  # fora do OneDrive (que bloqueia pastas temporárias)
OUT = os.path.join(ROOT, "RadarCharles.apk")
KEYSTORE = os.path.join(HERE, "radar-charles.jks")
KEYPASS = "radarcharles"
PKG = "pt.radar.charles"
VERSION_CODE, VERSION_NAME = 4, "1.3"

# ------------------------------------------------------------------ moldes android.*
STUBS = {
    "android/content/Context.java": """package android.content;
public class Context { public SharedPreferences getSharedPreferences(String n, int m) { throw new RuntimeException(); } }""",
    "android/content/SharedPreferences.java": """package android.content;
public interface SharedPreferences {
  String getString(String k, String d);
  Editor edit();
  interface Editor { Editor putString(String k, String v); void apply(); }
}""",
    "android/app/Activity.java": """package android.app;
public class Activity extends android.content.Context {
  protected void onCreate(android.os.Bundle b) {}
  public android.view.Window getWindow() { return null; }
  public void setContentView(android.view.View v) {}
  public boolean dispatchKeyEvent(android.view.KeyEvent e) { return false; }
  public final void runOnUiThread(Runnable r) {}
  public void finish() {}
}""",
    "android/os/Bundle.java": "package android.os; public final class Bundle {}",
    "android/view/KeyEvent.java": """package android.view;
public class KeyEvent { public final int getKeyCode() { return 0; } public final int getAction() { return 0; }
  public final int getRepeatCount() { return 0; } }""",
    "android/view/View.java": """package android.view;
public class View { public void setSystemUiVisibility(int v) {} public void setBackgroundColor(int c) {}
  public final boolean requestFocus() { return false; } }""",
    "android/view/Window.java": """package android.view;
public abstract class Window { public void addFlags(int f) {} public abstract View getDecorView(); }""",
    "android/webkit/JavascriptInterface.java": """package android.webkit;
import java.lang.annotation.*;
@Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD) public @interface JavascriptInterface {}""",
    "android/webkit/ValueCallback.java": "package android.webkit; public interface ValueCallback<T> { void onReceiveValue(T v); }",
    "android/webkit/WebSettings.java": """package android.webkit;
public abstract class WebSettings { public abstract void setJavaScriptEnabled(boolean b);
  public abstract void setDomStorageEnabled(boolean b); public abstract void setMediaPlaybackRequiresUserGesture(boolean b); }""",
    "android/webkit/WebViewClient.java": """package android.webkit;
public class WebViewClient { public void onPageFinished(WebView v, String u) {}
  public void onReceivedError(WebView v, int c, String d, String u) {} }""",
    "android/webkit/WebView.java": """package android.webkit;
public class WebView extends android.view.View {
  public WebView(android.content.Context c) {}
  public WebSettings getSettings() { return null; }
  public void loadUrl(String u) {}
  public void setWebViewClient(WebViewClient c) {}
  public void evaluateJavascript(String s, ValueCallback<String> cb) {}
  public void addJavascriptInterface(Object o, String n) {}
}""",
    "android/widget/Toast.java": """package android.widget;
public class Toast { public static Toast makeText(android.content.Context c, CharSequence t, int d) { return null; }
  public void show() {} }""",
}

# ------------------------------------------------------------------ XML binário (AXML)
ANDROID_NS = "http://schemas.android.com/apk/res/android"
ATTR_IDS = {
    "theme": 0x01010000, "label": 0x01010001, "icon": 0x01010002, "name": 0x01010003,
    "exported": 0x01010010, "launchMode": 0x0101001D, "screenOrientation": 0x0101001E,
    "configChanges": 0x0101001F, "minSdkVersion": 0x0101020C, "versionCode": 0x0101021B,
    "versionName": 0x0101021C, "targetSdkVersion": 0x01010270, "required": 0x0101028E,
    "hardwareAccelerated": 0x010102D3, "banner": 0x010103F2, "usesCleartextTraffic": 0x010104EC,
}
T_REF, T_STR, T_DEC, T_HEX, T_BOOL = 0x01, 0x03, 0x10, 0x11, 0x12


def string_pool(strings):
    """ResStringPool em UTF-16."""
    offsets, data = [], b""
    for s in strings:
        offsets.append(len(data))
        u = s.encode("utf-16-le")
        data += struct.pack("<H", len(u) // 2) + u + b"\0\0"
    while len(data) % 4:
        data += b"\0"
    start = 28 + 4 * len(strings)
    head = struct.pack("<HHIIIIII", 0x0001, 28, start + len(data), len(strings), 0, 0, start, 0)
    return head + b"".join(struct.pack("<I", o) for o in offsets) + data


def A(name, typ, val):  # atributo android:*
    return ("android", name, typ, val)


def P(name, typ, val):  # atributo sem namespace
    return (None, name, typ, val)


MANIFEST = ("manifest", [P("package", T_STR, PKG), A("versionCode", T_DEC, VERSION_CODE),
                         A("versionName", T_STR, VERSION_NAME)], [
    ("uses-sdk", [A("minSdkVersion", T_DEC, 21), A("targetSdkVersion", T_DEC, 29)], []),
    ("uses-permission", [A("name", T_STR, "android.permission.INTERNET")], []),
    ("uses-feature", [A("name", T_STR, "android.software.leanback"), A("required", T_BOOL, False)], []),
    ("uses-feature", [A("name", T_STR, "android.hardware.touchscreen"), A("required", T_BOOL, False)], []),
    ("application", [A("theme", T_REF, 0x01030007),  # @android:style/Theme.NoTitleBar.Fullscreen
                     A("label", T_STR, "Radar Charles"), A("icon", T_REF, 0x7F010000),
                     A("hardwareAccelerated", T_BOOL, True), A("banner", T_REF, 0x7F010001),
                     A("usesCleartextTraffic", T_BOOL, True)], [
        ("activity", [A("name", T_STR, PKG + ".MainActivity"), A("exported", T_BOOL, True),
                      A("launchMode", T_DEC, 2), A("screenOrientation", T_DEC, 0),
                      A("configChanges", T_HEX, 0x7F0)], [
            ("intent-filter", [], [
                ("action", [A("name", T_STR, "android.intent.action.MAIN")], []),
                ("category", [A("name", T_STR, "android.intent.category.LAUNCHER")], []),
                ("category", [A("name", T_STR, "android.intent.category.LEANBACK_LAUNCHER")], []),
            ]),
        ]),
    ]),
])


def axml(root):
    # 1) recolher strings: primeiro os nomes de atributos com ID (ordenados), depois o resto
    attr_names, others = set(), []

    def walk(el):
        tag, attrs, kids = el
        others.append(tag)
        for ns, name, typ, val in attrs:
            (attr_names.add(name) if ns == "android" else others.append(name))
            if typ == T_STR:
                others.append(val)
        for k in kids:
            walk(k)
    walk(root)
    mapped = sorted(attr_names, key=lambda n: ATTR_IDS[n])
    strings = list(mapped)
    for s in ["android", ANDROID_NS] + others:
        if s not in strings:
            strings.append(s)
    idx = {s: i for i, s in enumerate(strings)}
    NONE = 0xFFFFFFFF
    ns_i, pre_i = idx[ANDROID_NS], idx["android"]

    body = struct.pack("<HHIIIII", 0x0100, 16, 24, 1, NONE, pre_i, ns_i)
    line = [1]

    def emit(el):
        nonlocal body
        tag, attrs, kids = el
        line[0] += 1
        enc = []
        for ns, name, typ, val in attrs:
            rid = ATTR_IDS.get(name, 0) if ns == "android" else 0
            if typ == T_STR:
                raw, data = idx[val], idx[val]
            elif typ == T_BOOL:
                raw, data = NONE, (0xFFFFFFFF if val else 0)
            else:
                raw, data = NONE, val
            enc.append((rid, struct.pack("<IIIHBBI", ns_i if ns else NONE, idx[name], raw, 8, 0, typ, data)))
        enc.sort(key=lambda t: t[0])  # o Android exige atributos por ordem de ID
        size = 36 + 20 * len(enc)
        body += struct.pack("<HHIII", 0x0102, 16, size, line[0], NONE)
        body += struct.pack("<IIHHHHHH", NONE, idx[tag], 20, 20, len(enc), 0, 0, 0)
        body += b"".join(e for _, e in enc)
        for k in kids:
            emit(k)
        line[0] += 1
        body += struct.pack("<HHIIIII", 0x0103, 16, 24, line[0], NONE, NONE, idx[tag])
    emit(root)
    body += struct.pack("<HHIIIII", 0x0101, 16, 24, line[0] + 1, NONE, pre_i, ns_i)

    pool = string_pool(strings)
    resmap = struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(mapped)) + b"".join(
        struct.pack("<I", ATTR_IDS[n]) for n in mapped)
    content = pool + resmap + body
    return struct.pack("<HHI", 0x0003, 8, 8 + len(content)) + content


# ------------------------------------------------------------------ resources.arsc
def arsc(entries):
    """entries: [(nome, caminho_no_apk)] todos do tipo 'drawable' -> 0x7f01xxxx."""
    n = len(entries)
    gpool = string_pool([p for _, p in entries])
    tpool = string_pool(["drawable"])
    kpool = string_pool([k for k, _ in entries])
    spec = struct.pack("<HHIBBHI", 0x0202, 16, 16 + 4 * n, 1, 0, 0, n) + b"\0\0\0\0" * n
    config = struct.pack("<I", 64) + b"\0" * 60
    head = 20 + len(config)
    start = head + 4 * n
    ents, offs = b"", []
    for i in range(n):
        offs.append(len(ents))
        ents += struct.pack("<HHI", 8, 0, i) + struct.pack("<HBBI", 8, 0, T_STR, i)
    typ = (struct.pack("<HHIBBHII", 0x0201, head, start + len(ents), 1, 0, 0, n, start) + config
           + b"".join(struct.pack("<I", o) for o in offs) + ents)
    name = PKG.encode("utf-16-le").ljust(256, b"\0")
    body = tpool + kpool + spec + typ
    pkg = (struct.pack("<HHII", 0x0200, 288, 288 + len(body), 0x7F) + name
           + struct.pack("<IIIII", 288, 1, 288 + len(tpool), n, 0) + body)
    return struct.pack("<HHII", 0x0002, 12, 12 + len(gpool) + len(pkg), 1) + gpool + pkg


# ------------------------------------------------------------------ imagens
def make_images(dst):
    icon = Image.open(os.path.join(ROOT, "icon.png")).convert("RGBA")
    icon.resize((192, 192), Image.LANCZOS).save(os.path.join(dst, "icon.png"))
    W, H = 320, 180  # banner do menu Android TV
    ban = Image.new("RGBA", (W, H))
    px = ban.load()
    for y in range(H):
        for x in range(W):
            t = min(1, ((x - 90) ** 2 + (y - 90) ** 2) ** .5 / 300)
            px[x, y] = (int(24 - 18 * t), int(40 - 32 * t), int(82 - 64 * t), 255)
    ban.alpha_composite(icon.resize((116, 116), Image.LANCZOS), (22, 32))
    d = ImageDraw.Draw(ban)
    font = lambda f, s: ImageFont.truetype(os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", f), s)
    d.text((150, 58), "Radar", font=font("segoeuib.ttf", 34), fill=(232, 238, 252))
    d.text((150, 96), "Charles", font=font("segoeuib.ttf", 34), fill=(111, 211, 255))
    d.rectangle((150, 140, 196, 144), fill=(255, 138, 42))
    ban.convert("RGB").save(os.path.join(dst, "banner.png"))


# ------------------------------------------------------------------ build
def run(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit(f"falhou: {cmd[0]}")
    return r.stdout + r.stderr


def main():
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    stub_src, stub_out, cls_out, dex_out, img = (os.path.join(BUILD, d) for d in ("stubs-src", "stubs", "classes", "dex", "img"))
    for d in (stub_src, stub_out, cls_out, dex_out, img):
        os.makedirs(d)

    print("1/6 moldes android.*")
    files = []
    for rel, src in STUBS.items():
        p = os.path.join(stub_src, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(src)
        files.append(p)
    run(os.path.join(JDK, "javac"), "--release", "8", "-nowarn", "-d", stub_out, *files)

    print("2/6 compilar a app")
    app_src = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(HERE, "src")) for f in fs if f.endswith(".java")]
    run(os.path.join(JDK, "javac"), "--release", "8", "-encoding", "UTF-8", "-nowarn",
        "-cp", stub_out, "-d", cls_out, *app_src)

    print("3/6 converter para dex (D8)")
    classes = [os.path.join(dp, f) for dp, _, fs in os.walk(cls_out) for f in fs if f.endswith(".class")]
    run(os.path.join(JDK, "java"), "-cp", R8, "com.android.tools.r8.D8", "--release", "--min-api", "21",
        "--no-desugaring", "--output", dex_out, *classes)

    print("4/6 manifest + recursos + imagens")
    make_images(img)
    manifest = axml(MANIFEST)
    resources = arsc([("icon", "res/drawable/icon.png"), ("banner", "res/drawable/banner.png")])

    print("5/6 montar o apk")
    unsigned = os.path.join(BUILD, "unsigned.apk")
    with zipfile.ZipFile(unsigned, "w") as z:
        z.writestr(zipfile.ZipInfo("AndroidManifest.xml"), manifest, zipfile.ZIP_DEFLATED)
        z.writestr("classes.dex", open(os.path.join(dex_out, "classes.dex"), "rb").read(), zipfile.ZIP_DEFLATED)
        z.writestr("resources.arsc", resources, zipfile.ZIP_STORED)
        z.write(os.path.join(img, "icon.png"), "res/drawable/icon.png", zipfile.ZIP_STORED)
        z.write(os.path.join(img, "banner.png"), "res/drawable/banner.png", zipfile.ZIP_STORED)
        z.write(os.path.join(HERE, "assets", "setup.html"), "assets/setup.html", zipfile.ZIP_DEFLATED)

    print("6/6 assinar")
    if not os.path.exists(KEYSTORE):
        run(os.path.join(JDK, "keytool"), "-genkeypair", "-keystore", KEYSTORE, "-alias", "radar",
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-storepass", KEYPASS,
            "-keypass", KEYPASS, "-dname", "CN=Radar Charles, O=Radar Charles, C=PT")
    run(os.path.join(JDK, "jarsigner"), "-keystore", KEYSTORE, "-storepass", KEYPASS,
        "-sigalg", "SHA256withRSA", "-digestalg", "SHA-256", unsigned, "radar")
    shutil.copy(unsigned, OUT)
    print(f"\nPronto: {OUT}  ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main()
