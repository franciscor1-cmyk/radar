"""Radar Charles - painel em tempo real da depressão Charles.

python app.py            -> abre a janela (pywebview) + servidor para a TV
python app.py --no-window -> só servidor (abrir na TV / browser)
"""
import ctypes
import email.utils
import html
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 3000))
STARTED = time.time()
REFRESH_S = 60
HERE = os.path.dirname(os.path.abspath(__file__))

NEWS_QUERIES = ['"depressão Charles"', '"tempestade Charles"', "Charles IPMA aviso", '"storm Charles" Portugal']

# ---------- eventos (cada aparelho escolhe qual quer ver) ----------
# Ficam em eventos.json (podes acrescentar à mão). As tempestades com nome que aparecem nas
# notícias são acrescentadas sozinhas (sem trajetória, porque não se sabe onde está o centro).
EVENTS_FILE = os.path.join(HERE, "eventos.json")
DEFAULT_EVENTS = {
    "descobrirNasNoticias": True,
    "eventos": [
        {"id": "charles", "nome": "Charles", "tipo": "Depressão", "icone": "🌀", "nomeadoEm": "2026-11-09",
         "pesquisas": NEWS_QUERIES, "centro": [37.5, -11.0]},
        {"id": "geral", "nome": "Tempo em Portugal", "tipo": "Geral", "icone": "🌦", "sempre": True,
         "pesquisas": ["IPMA aviso mau tempo", "IPMA previsão tempo Portugal", "Proteção Civil aviso meteorológico"]},
    ],
}
events = {}       # id -> dados do evento (nome, tipo, pesquisas, centro...)
config = {}
DISCOVER_QUERIES = ['"depressão" IPMA', '"tempestade" IPMA', "IPMA nome tempestade", '"tempestade" Portugal mau tempo']
NAME_RE = re.compile(r"\b(?:[Dd]epressão|[Tt]empestade)\s+(?:(?:[Ss]ub)?[Tt]ropical\s+)?"
                     r"([A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõçà]{2,14})\b")
NOT_NAMES = {"atlântico", "atlântica", "ibérica", "mediterrâneo", "portugal", "ipma", "perfeita", "isolada",
             "tropical", "subtropical", "severa", "forte", "intensa", "marítima", "norte", "sul", "este", "oeste",
             "esta", "este", "essa", "que", "chega", "traz", "deixa", "atinge", "provoca", "continua", "vai",
             "pode", "está", "em", "no", "na", "nos", "nas", "de", "do", "da", "para", "com", "afeta", "passa"}


def slug(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "evento"


def load_events():
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = DEFAULT_EVENTS
        try:
            with open(EVENTS_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    config.update(cfg)
    for e in cfg.get("eventos", []):
        if not e.get("nome"):
            continue
        e = dict(e, id=slug(e.get("id") or e["nome"]), origem="config")
        e.setdefault("tipo", "Tempestade"); e.setdefault("icone", "🌀")
        e.setdefault("kind", "geral" if e.get("sempre") else "tempestade")
        e.setdefault("pesquisas", [f'"{e["tipo"].lower()} {e["nome"]}"'])
        add_event(e)


def add_event(e):
    with lock:
        events[e["id"]] = e
        state["events"].setdefault(e["id"], {"news": [], "track": None, "obs": None, "errors": {}})

IPMA_AREAS = {
    "AVR": "Aveiro", "BJA": "Beja", "BRG": "Braga", "BGC": "Bragança", "CBO": "Castelo Branco",
    "CBR": "Coimbra", "EVR": "Évora", "FAR": "Faro", "GDA": "Guarda", "LRA": "Leiria", "LSB": "Lisboa",
    "PTG": "Portalegre", "PTO": "Porto", "STM": "Santarém", "STB": "Setúbal", "VCT": "Viana do Castelo",
    "VRL": "Vila Real", "VIS": "Viseu", "MCN": "Madeira C. Norte", "MCS": "Madeira C. Sul",
    "MRM": "Madeira Montanha", "MPS": "Porto Santo", "AOR": "Açores Oriental",
    "ACE": "Açores Central", "AOC": "Açores Ocidental",
}

CITIES = [
    ("Lisboa", 38.72, -9.14), ("Porto", 41.15, -8.61), ("Coimbra", 40.21, -8.43),
    ("Faro", 37.02, -7.93), ("Funchal", 32.65, -16.91), ("Ponta Delgada", 37.74, -25.67),
    ("Viana do Castelo", 41.69, -8.83), ("Évora", 38.57, -7.91),
]

state = {"updatedAt": None, "warnings": [], "weather": [], "errors": {}, "events": {}}
lock = threading.Lock()
viewers = {}  # IPs de TVs/aparelhos ligados ao painel (fora deste PC)
version = 0
cond = threading.Condition()
cmds, cmd_seq = [], 0  # comandos do telemóvel (seq, nome)
ALLOWED_CMDS = {"left", "right", "up", "down", "pageup", "pagedown", "ok", "back",
                "red", "green", "help", "tv", "map", "fit", "card", "home"}
# cidades, eventos e gestos do modo toque do telemóvel
GESTURE_RE = (r"city:[1-9]|type:[0-9]|tab:(live|all)|clear|event:[a-z0-9-]{1,40}|search:[\w .,'-]{0,40}|swipe:(left|right|up|down)"
              r"|drag:-?\d{1,4},-?\d{1,4}|pinch:\d{1,2}\.\d{1,3}")


def get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "storm-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:  # alguns feeds (ex.: Smithsonian) vêm em Windows-1252
        return raw.decode("cp1252", "replace")


_cache, _cache_lock = {}, threading.Lock()


def get_cached(url, ttl=240, timeout=40):
    """Mesmo pedido feito por vários eventos ao mesmo tempo (IPMA, METAR): só vai à rede uma vez."""
    with _cache_lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    body = get(url, timeout=timeout)
    with _cache_lock:
        _cache[url] = (time.time(), body)
    return body


def tag(xml, t):
    m = re.search(rf"<{t}[^>]*>([\s\S]*?)</{t}>", xml)
    if not m:
        return ""
    s = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", m.group(1))
    return re.sub(r"<[^>]+>", "", html.unescape(s)).strip()


def rss(q, when="3d"):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + (f" when:{when}" if when else ""))
           + "&hl=pt-PT&gl=PT&ceid=PT:pt-150")
    out = []
    for raw in get_cached(url, ttl=50, timeout=15).split("<item>")[1:]:
        title, source = tag(raw, "title"), tag(raw, "source")
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)]
        try:
            date = email.utils.parsedate_to_datetime(tag(raw, "pubDate")).astimezone(timezone.utc).isoformat()
        except Exception:
            date = datetime.now(timezone.utc).isoformat()
        out.append({"title": title, "link": tag(raw, "link"), "source": source, "date": date})
    return out


def fetch_news(queries=NEWS_QUERIES, must=None, when="3d"):
    """must = nome do evento: só ficam os títulos que o referem (as pesquisas largas apanham outras coisas).
    when=None: sem limite de datas (tempestades que já aconteceram)."""
    items, seen = [], set()
    for q in queries:
        for n in rss(q, when=when):
            if must:  # texto, ou lista de grupos: o título tem de ter uma palavra de cada grupo
                groups = [[must]] if isinstance(must, str) else must
                tl = n["title"].lower()
                if not all(any(w.lower() in tl for w in g) for g in groups):
                    continue
            if not when and time.time() - datetime.fromisoformat(n["date"]).timestamp() > PAST_LIMIT_S:
                continue  # notícias com mais de 1 ano e meio não contam
            if n["title"].lower() not in seen:
                seen.add(n["title"].lower())
                items.append(n)
    items.sort(key=lambda n: n["date"], reverse=True)
    return items[:40]


PAST_STORMS = set()
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def is_past_storm(name):
    """Uma tempestade dura dias: se já há 3+ notícias com o nome dela de há mais de 14 dias, é antiga.
    Devolve o mês em que mais se falou dela (ex.: "jan 2026") ou None se não for antiga."""
    old = []
    for q in (f'"tempestade {name}"', f'"depressão {name}"'):
        for n in rss(q, when=None):
            d = datetime.fromisoformat(n["date"])
            if time.time() - d.timestamp() > 14 * 86400:
                old.append((d.year, d.month))
    if len(old) < 3:
        return None
    y, m = max(set(old), key=old.count)
    return {"label": f"{MESES[m - 1]} {y}", "ts": datetime(y, m, 15, tzinfo=timezone.utc).timestamp()}


PAST_LIMIT_S = 548 * 86400  # tempestades passadas: no máximo 1 ano e meio


def discover_events():
    """Procura nas notícias tempestades/depressões com nome (ex.: "depressão Charles").
    Só conta um nome que apareça em 3 ou mais títulos diferentes dos últimos 3 dias."""
    titles = {}
    for q in DISCOVER_QUERIES:
        for n in rss(q):
            for m in NAME_RE.finditer(n["title"]):
                name = m.group(1)
                if name.lower() not in NOT_NAMES:
                    titles.setdefault(name, set()).add(n["title"].lower())
    known = {slug(e["nome"]) for e in events.values()} | PAST_STORMS
    new = []
    for name, ts in titles.items():
        if len(ts) >= 3 and slug(name) not in known:
            tipo = "Tempestade" if sum("tempestade" in t for t in ts) > len(ts) / 2 else "Depressão"
            ev = {"id": slug(name), "nome": name, "tipo": tipo, "icone": "🌀", "origem": "notícias",
                  "pesquisas": [f'"depressão {name}"', f'"tempestade {name}"', f'"storm {name}"']}
            quando = is_past_storm(name)  # ex.: "traumas da tempestade Kristin" = tempestade antiga, só referida
            if quando:
                PAST_STORMS.add(slug(name))
                if time.time() - quando["ts"] > PAST_LIMIT_S:
                    print(f"{time.strftime('%H:%M:%S')}  {name}: já aconteceu ({quando['label']}), "
                          "há mais de 1 ano e meio: fica de fora")
                    continue
                ev.update(passada=True, quando=quando["label"], quandoTs=quando["ts"])
                print(f"{time.strftime('%H:%M:%S')}  {name}: tempestade que já aconteceu ({quando['label']})")
            add_event(ev)
            new.append(name)
            print(f"{time.strftime('%H:%M:%S')}  novo evento nas notícias: {tipo} {name}")
    return new


def fetch_warnings():
    data = json.loads(get("https://api.ipma.pt/open-data/forecast/warnings/warnings_www.json"))
    order = {"red": 3, "orange": 2, "yellow": 1}
    now = datetime.now().isoformat()
    out = [{
        "area": IPMA_AREAS.get(w["idAreaAviso"], w["idAreaAviso"]),
        "type": w.get("awarenessTypeName", ""), "level": w["awarenessLevelID"],
        "start": w["startTime"], "end": w["endTime"], "text": w.get("text", ""),
    } for w in data if w.get("awarenessLevelID") in order and w.get("endTime", "") > now[:19]]
    out.sort(key=lambda w: (-order[w["level"]], w["start"]))
    fx = os.environ.get("RADAR_FIXTURES")  # testes: avisos de exemplo acrescentados
    if fx and os.path.exists(os.path.join(fx, "warnings_extra.json")):
        with open(os.path.join(fx, "warnings_extra.json"), encoding="utf-8") as f:
            out += json.load(f)
    return out


def fetch_weather():
    lat = ",".join(str(c[1]) for c in CITIES)
    lon = ",".join(str(c[2]) for c in CITIES)
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m,pressure_msl,cloud_cover"
           "&daily=wind_gusts_10m_max,precipitation_sum,temperature_2m_max,temperature_2m_min"
           "&forecast_days=2&timezone=Europe%2FLisbon")
    data = json.loads(get(url))
    data = data if isinstance(data, list) else [data]
    return [{
        "city": CITIES[i][0], "lat": CITIES[i][1], "lon": CITIES[i][2],
        "temp": d["current"]["temperature_2m"], "rain": d["current"]["precipitation"],
        "wind": d["current"]["wind_speed_10m"], "gust": d["current"]["wind_gusts_10m"],
        "dir": d["current"]["wind_direction_10m"], "pressure": d["current"]["pressure_msl"],
        "cloud": d["current"]["cloud_cover"],
        "gustMaxToday": d["daily"]["wind_gusts_10m_max"][0],
        "gustMaxTomorrow": d["daily"]["wind_gusts_10m_max"][1],
        "rainToday": d["daily"]["precipitation_sum"][0],
        "tMaxToday": d["daily"]["temperature_2m_max"][0], "tMinToday": d["daily"]["temperature_2m_min"][0],
        "tMaxTomorrow": d["daily"]["temperature_2m_max"][1], "tMinTomorrow": d["daily"]["temperature_2m_min"][1],
    } for i, d in enumerate(data)]


# ---------- trajetória da depressão (centro = mínimo de pressão do modelo) ----------
TRACK_LATS = [25.5 + 1.5 * i for i in range(15)]    # 25.5 .. 46.5 N
TRACK_LONS = [-34.5 + 1.5 * j for j in range(26)]   # 34.5 W .. 3 E
TRACK_SEED = (37.5, -11.0)  # o núcleo mais perto de Portugal/Madeira é a Charles
STEP_H = 3                   # um ponto a cada 3 h
MAX_JUMP_KM = 480            # deslocação máxima aceitável entre pontos (3 h)


def km(a, b):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def local_minima(P):
    """Mínimos locais da grelha, afinados com uma parábola entre pontos (sub-grelha)."""
    out, NI, NJ = [], len(TRACK_LATS), len(TRACK_LONS)
    for i in range(1, NI - 1):
        for j in range(1, NJ - 1):
            v = P[i][j]
            nb = [P[i + a][j + b] for a in (-1, 0, 1) for b in (-1, 0, 1) if a or b]
            if v is None or any(n is None or n <= v for n in nb):
                continue

            def off(m, c, p):
                den = m - 2 * c + p
                return 0.0 if den <= 0 else max(-.5, min(.5, (m - p) / (2 * den)))
            lat = TRACK_LATS[i] + off(P[i - 1][j], v, P[i + 1][j]) * 1.5
            lon = TRACK_LONS[j] + off(P[i][j - 1], v, P[i][j + 1]) * 1.5
            out.append({"lat": round(lat, 2), "lon": round(lon, 2), "p": round(v, 1)})
    return out


GRID_VARS = ("pressure_msl,temperature_2m,precipitation,wind_speed_10m,wind_direction_10m,"
             "wind_gusts_10m,cloud_cover")
FIELD = {"version": 0, "json": b"{}"}  # camadas do mapa (estilo Ventusky), servidas em /api/field


def get_grid():
    """Grelha de 390 pontos com todas as variáveis (um só pedido, guardado em cache 3 h).
    No Open-Meteo conta como 390 consultas, por isso não se repete a cada arranque."""
    pts = [(la, lo) for la in TRACK_LATS for lo in TRACK_LONS]
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + ",".join(f"{p[0]:g}" for p in pts)
           + "&longitude=" + ",".join(f"{p[1]:g}" for p in pts)
           + f"&hourly={GRID_VARS}&past_days=3&forecast_days=3&timezone=UTC")
    cache = os.path.join(HERE, "cache_trajetoria.json")
    try:
        with open(cache, encoding="utf-8") as f:
            c = json.load(f)
        if time.time() - c["fetched"] < 3 * 3600 and c.get("url") == url:
            return c["data"], c["fetched"]
    except (OSError, ValueError, KeyError):
        pass
    data = json.loads(get(url, timeout=120))
    fetched = time.time()
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"fetched": fetched, "url": url, "data": data}, f)
    return data, fetched


def build_field(data, fetched):
    """Fotogramas de 3 em 3 h, de -24 h a +48 h: temperatura, chuva, vento (u/v), rajadas, nuvens, pressão."""
    import math
    times = data[0]["hourly"]["time"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    idx = [i for i, t in enumerate(times) if i % STEP_H == 0
           and -24 * 3600 <= (datetime.fromisoformat(t) - now).total_seconds() <= 48 * 3600]
    out = {"lat0": TRACK_LATS[0], "lon0": TRACK_LONS[0], "dlat": 1.5, "dlon": 1.5,
           "nlat": len(TRACK_LATS), "nlon": len(TRACK_LONS), "times": [times[i] + "Z" for i in idx],
           "now": min(range(len(idx)), key=lambda k: abs((datetime.fromisoformat(times[idx[k]]) - now).total_seconds())),
           "vars": {k: [] for k in ("temp", "rain", "wind", "gust", "cloud", "pres", "u", "v")}}
    r1 = lambda x: None if x is None else round(x, 1)
    for i in idx:
        fr = {k: [] for k in out["vars"]}
        for d in data:  # ordem: latitude (sul->norte) e depois longitude (oeste->este)
            h = d["hourly"]
            ws, wd = h["wind_speed_10m"][i], h["wind_direction_10m"][i]
            fr["temp"].append(r1(h["temperature_2m"][i]))
            fr["rain"].append(r1(h["precipitation"][i]))
            fr["wind"].append(None if ws is None else round(ws))
            fr["gust"].append(None if h["wind_gusts_10m"][i] is None else round(h["wind_gusts_10m"][i]))
            fr["cloud"].append(h["cloud_cover"][i])
            fr["pres"].append(r1(h["pressure_msl"][i]))
            if ws is None or wd is None:
                fr["u"].append(None); fr["v"].append(None)
            else:  # direção meteorológica = de onde vem o vento -> componentes u (para E) e v (para N) em m/s
                ms = ws / 3.6
                fr["u"].append(round(-ms * math.sin(math.radians(wd)), 1))
                fr["v"].append(round(-ms * math.cos(math.radians(wd)), 1))
        for k in out["vars"]:
            out["vars"][k].append(fr[k])
    FIELD["json"] = json.dumps(out, separators=(",", ":")).encode()
    FIELD["version"] = int(fetched)


_grid_lock = threading.Lock()


def shared_grid():
    """A grelha é a mesma para todos os eventos: lê-se uma vez e as camadas do mapa só se refazem se mudar."""
    with _grid_lock:
        data, fetched = get_grid()
        if FIELD["version"] != int(fetched):
            try:
                build_field(data, fetched)
                with lock:
                    state["fieldVersion"] = FIELD["version"]
            except Exception as e:  # as camadas são extra: um erro aqui não estraga a trajetória
                print(f"[camadas] erro: {e}")
        return data


def fetch_track(seed=TRACK_SEED):
    data = shared_grid()
    times = data[0]["hourly"]["time"]
    NJ = len(TRACK_LONS)
    steps = list(range(0, len(times), STEP_H))
    minima = {}
    for ti in steps:
        P = [[data[i * NJ + j]["hourly"]["pressure_msl"][ti] for j in range(NJ)] for i in range(len(TRACK_LATS))]
        minima[ti] = local_minima(P)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    t_now = min(steps, key=lambda ti: abs((datetime.fromisoformat(times[ti]) - now).total_seconds()))
    seed_c = [m for m in minima[t_now] if km((m["lat"], m["lon"]), seed) < 1400]
    if not seed_c:
        return {"points": [], "nowIndex": -1, "note": "Não há núcleo da depressão perto de Portugal neste momento."}
    seed_pt = tuple(seed)
    seed = min(seed_c, key=lambda m: km((m["lat"], m["lon"]), seed_pt))

    def follow(direction, prev=None, max_gap=3):
        """Segue o núcleo no tempo. Usa a velocidade da tempestade para prever a posição seguinte
        e aceita só o mínimo perto dessa previsão (evita saltar para outra depressão).
        Tolera até max_gap passos em que a grelha não apanha o núcleo."""
        chain, cur, k = [], seed, steps.index(t_now)
        vel = (0.0, 0.0)  # graus por passo, no sentido em que estamos a seguir
        if prev:  # prev = ponto anterior a "agora" -> velocidade para a frente
            dt = datetime.fromisoformat(times[t_now]) - datetime.fromisoformat(prev["t"].rstrip("Z"))
            g0 = max(1, round(dt.total_seconds() / (3600 * STEP_H)))
            vel = ((seed["lat"] - prev["lat"]) / g0, (seed["lon"] - prev["lon"]) / g0)
        while True:
            hit = None
            for g in range(1, max_gap + 1):
                kk = k + direction * g
                if not 0 <= kk < len(steps):
                    break
                # tem de estar perto da posição atual E da posição prevista pela velocidade
                # (assim não salta para outra depressão nem volta para trás)
                full = (cur["lat"] + vel[0] * g, cur["lon"] + vel[1] * g)
                pred = (cur["lat"] + .5 * vel[0] * g, cur["lon"] + .5 * vel[1] * g)
                cand = [m for m in minima[steps[kk]]
                        if km((m["lat"], m["lon"]), (cur["lat"], cur["lon"])) <= MAX_JUMP_KM * g
                        and (vel == (0.0, 0.0) or km((m["lat"], m["lon"]), full) <= 400 * g)]
                if cand:
                    hit = (kk, g, min(cand, key=lambda m: km((m["lat"], m["lon"]), pred)))
                    break
            if not hit:
                return chain
            kk, g, nxt = hit
            vel = ((nxt["lat"] - cur["lat"]) / g, (nxt["lon"] - cur["lon"]) / g)
            k, cur = kk, nxt
            chain.append(dict(cur, t=times[steps[k]] + "Z"))

    past = follow(-1)[::-1]
    # para a previsão parte já com a velocidade que trazia (último ponto passado -> agora)
    future = follow(+1, prev=past[-1] if past else None)
    now_pt = dict(seed, t=times[t_now] + "Z")
    points = past + [now_pt] + future
    deepest = min(points, key=lambda m: m["p"])
    return {"points": points, "nowIndex": len(past), "deepest": deepest,
            "source": "Open-Meteo (modelo), mínimo de pressão ao nível do mar",
            "computedAt": datetime.now(timezone.utc).isoformat()}


# ---------- centro OBSERVADO da Charles (estações IPMA + METAR dos aeroportos) ----------
OBS_BOX = (27, -32, 45, 4)  # lat min, lon min, lat max, lon max


def _xy(lat, lon, lat0, lon0):
    import math
    return (lon - lon0) * 111.32 * math.cos(math.radians(lat0)), (lat - lat0) * 110.57


def _solve(A, b):
    """Eliminação de Gauss com pivô (sistema pequeno 6x6)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]


def obs_center(obs):
    """obs: [{lat, lon, p, ...}]. Ajusta p = a + bx + cy + dx² + ey² + fxy às estações perto do mínimo
    e devolve o centro (onde o gradiente é zero) + confiança."""
    import math
    obs = [o for o in obs if o.get("p") and 940 < o["p"] < 1050]
    if len(obs) < 6:
        return None
    mn = min(obs, key=lambda o: o["p"])
    if mn["p"] > 1008:  # nenhuma depressão relevante sobre a rede de estações
        return None
    lat0, lon0 = mn["lat"], mn["lon"]
    near = []
    for o in obs:
        x, y = _xy(o["lat"], o["lon"], lat0, lon0)
        d = math.hypot(x, y)
        if d <= 320:
            near.append((x / 100, y / 100, o["p"], math.exp(-(d / 220) ** 2)))
    result = {"lat": lat0, "lon": lon0, "p": mn["p"], "method": "estação mais baixa",
              "station": mn.get("name"), "n": len(near)}
    if len(near) >= 7:
        A = [[0.0] * 6 for _ in range(6)]
        bv = [0.0] * 6
        for x, y, p, w in near:
            row = [1, x, y, x * x, y * y, x * y]
            for i in range(6):
                bv[i] += w * row[i] * p
                for j in range(6):
                    A[i][j] += w * row[i] * row[j]
        th = _solve(A, bv)
        if th:
            a, b, c, d, e, f = th
            det = 4 * d * e - f * f
            if d > 0 and e > 0 and det > 0:  # é mesmo um mínimo (taça)
                cx = (-2 * e * b + f * c) / det
                cy = (-2 * d * c + f * b) / det
                pc = a + b * cx + c * cy + d * cx * cx + e * cy * cy + f * cx * cy
                if math.hypot(cx, cy) * 100 <= 220 and mn["p"] - 6 <= pc <= mn["p"] + .5:
                    result.update(lat=lat0 + cy * 100 / 110.57,
                                  lon=lon0 + cx * 100 / (111.32 * math.cos(math.radians(lat0))),
                                  p=round(pc, 1), method="ajuste de superfície")
    # confiança: há estações à volta do centro em quantos quadrantes (até 300 km)?
    # (só contam estações a mais de 50 km: numa ilha pequena as estações "rodeiam" tudo e enganavam)
    quads = set()
    for o in obs:
        x, y = _xy(o["lat"], o["lon"], result["lat"], result["lon"])
        if 50 < math.hypot(x, y) <= 300:
            quads.add((x >= 0, y >= 0))
    result["quadrants"] = len(quads)
    result["confidence"] = "alta" if len(quads) == 4 else "média" if len(quads) == 3 else "baixa"
    names = {(True, True): "NE", (False, True): "NO", (False, False): "SO", (True, False): "SE"}
    result["gaps"] = [n for q, n in names.items() if q not in quads]  # lados sem estações (normalmente mar)
    result["lat"], result["lon"] = round(result["lat"], 3), round(result["lon"], 3)
    return result


def fetch_obs(track=None):
    """Observações reais: IPMA (hora a hora, 24 h) + METAR (a cada 30 min). Calcula o centro observado agora
    e a trajetória observada das últimas 24 h; corrige a previsão do modelo (track) com o desvio observado."""
    import math
    stations = {str(s["properties"]["idEstacao"]): s for s in json.loads(get_cached(
        "https://api.ipma.pt/open-data/observation/meteorology/stations/stations.json", ttl=6 * 3600))}
    ipma = json.loads(get_cached("https://api.ipma.pt/open-data/observation/meteorology/stations/observations.json"))
    la0, lo0, la1, lo1 = OBS_BOX
    metar = json.loads(get_cached(
        f"https://aviationweather.gov/api/data/metar?bbox={la0},{lo0},{la1},{lo1}&format=json&hours=24"))

    hours = sorted(ipma.keys())

    def ipma_hour(h, shift_h=0.0):
        """Estações IPMA à hora h. shift_h > 0 leva a pressão para mais tarde usando a tendência
        da última hora de cada estação (para juntar com METAR mais recentes sem misturar horas)."""
        out = []
        prev = ipma.get(hours[hours.index(h) - 1]) if shift_h and hours.index(h) > 0 else None
        for sid, v in (ipma.get(h) or {}).items():
            st = stations.get(sid)
            if not v or not st:
                continue
            p = v.get("pressao", -99)
            if shift_h and p and p > 900 and prev and prev.get(sid) and (prev[sid].get("pressao") or 0) > 900:
                tend = max(-4.0, min(4.0, p - prev[sid]["pressao"]))  # hPa por hora
                p = round(p + tend * shift_h, 1)
            lon, lat = st["geometry"]["coordinates"][:2]
            wind = v.get("intensidadeVentoKM", -99)
            out.append({"name": st["properties"]["localEstacao"], "lat": lat, "lon": lon, "src": "IPMA",
                        "p": p if p and p > 900 else None, "wind": wind if wind is not None and wind >= 0 else None,
                        "gust": None, "t": h + ":00Z"})
        return out

    def metar_near(t_epoch, window=20 * 60):
        best = {}
        for m in metar:
            ot = m.get("obsTime")
            if not ot or abs(ot - t_epoch) > window:
                continue
            k = m["icaoId"]
            if k not in best or abs(ot - t_epoch) < abs(best[k]["obsTime"] - t_epoch):
                best[k] = m
        out = []
        for m in best.values():
            p = m.get("slp") or m.get("altim")
            kt = lambda v: None if v in (None, "VRB") else round(float(v) * 1.852)
            out.append({"name": (m.get("name") or m["icaoId"]).split(",")[0] + f" ({m['icaoId']})",
                        "lat": m["lat"], "lon": m["lon"], "src": "METAR", "p": float(p) if p else None,
                        "wind": kt(m.get("wspd")), "gust": kt(m.get("wgst")),
                        "t": datetime.fromtimestamp(m["obsTime"], timezone.utc).strftime("%Y-%m-%dT%H:%MZ")})
        return out

    epoch = lambda h: datetime.fromisoformat(h).replace(tzinfo=timezone.utc).timestamp()
    if track is None:
        with lock:
            track = dict((state["events"].get("charles") or {}).get("track") or {})
    pts = (track or {}).get("points") or []
    tt = [datetime.fromisoformat(p["t"].replace("Z", "+00:00")).timestamp() for p in pts]

    def model_at(t):
        for i in range(len(pts) - 1):
            if tt[i] <= t <= tt[i + 1]:
                r = (t - tt[i]) / (tt[i + 1] - tt[i] or 1)
                return (pts[i]["lat"] + (pts[i + 1]["lat"] - pts[i]["lat"]) * r,
                        pts[i]["lon"] + (pts[i + 1]["lon"] - pts[i]["lon"]) * r)
        return None

    def plausible(c, t):
        """Só é a Charles se estiver perto de onde o modelo a põe nessa hora (senão a tempestade
        está fora da rede de estações e o mínimo observado é outra coisa)."""
        if not c or c["confidence"] == "baixa":
            return False
        m = model_at(t)
        return m is None or km(m, (c["lat"], c["lon"])) <= 300

    # trajetória observada: uma posição por hora nas últimas 24 h
    track = []
    for h in hours:
        c = obs_center(ipma_hour(h) + metar_near(epoch(h)))
        if plausible(c, epoch(h)):
            track.append({"t": h + ":00Z", "lat": c["lat"], "lon": c["lon"], "p": c["p"], "confidence": c["confidence"]})
    # agora: METAR mais recente de cada aeroporto + IPMA levado à mesma hora pela tendência
    now_ep = time.time()
    latest_m = metar_near(now_ep, window=75 * 60)
    m_times = [datetime.fromisoformat(o["t"].replace("Z", "+00:00")).timestamp() for o in latest_m]
    t_ref = max(m_times) if m_times else epoch(hours[-1])
    t_ref = max(t_ref, epoch(hours[-1]))
    shift = min(2.0, (t_ref - epoch(hours[-1])) / 3600)
    now_obs = ipma_hour(hours[-1], shift_h=shift) + latest_m
    center = obs_center(now_obs)
    newest = datetime.fromtimestamp(t_ref, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    if center:
        center["t"] = newest
        center["plausible"] = plausible(center, t_ref)
        if center["plausible"] and (not track or track[-1]["t"] != newest):
            track.append({"t": newest, "lat": center["lat"], "lon": center["lon"], "p": center["p"],
                          "confidence": center["confidence"]})

    valid = [o for o in now_obs if o["p"]]
    min_st = min(valid, key=lambda o: o["p"]) if valid else None
    # rajadas/vento só das estações a menos de 500 km da Charles (senão apanhava, p.ex., as Canárias)
    ref = (center["lat"], center["lon"]) if center and center.get("plausible") else None
    around = [o for o in now_obs if ref is None or km(ref, (o["lat"], o["lon"])) <= 500]
    gusts = [o for o in around if o.get("gust")]
    winds = [o for o in around if o.get("wind")]
    near_list = []
    if center:
        for o in valid:
            x, y = _xy(o["lat"], o["lon"], center["lat"], center["lon"])
            if math.hypot(x, y) <= 400:
                near_list.append(o)
        near_list = sorted(near_list, key=lambda o: o["p"])[:45]

    # desvio do modelo e previsão corrigida (a correção vai-se desvanecendo em ~12 h)
    corrected, offset_km = [], None
    if center and center.get("plausible") and pts:
        t_c = t_ref
        m_at = model_at(t_c)
        if m_at:
            dlat, dlon = center["lat"] - m_at[0], center["lon"] - m_at[1]
            offset_km = round(km(m_at, (center["lat"], center["lon"])))
            corrected = [{"t": center["t"], "lat": center["lat"], "lon": center["lon"], "p": center["p"]}]
            for p, t in zip(pts, tt):
                if t > t_c:
                    w = math.exp(-(t - t_c) / (12 * 3600))
                    corrected.append({"t": p["t"], "lat": round(p["lat"] + dlat * w, 3),
                                      "lon": round(p["lon"] + dlon * w, 3), "p": p["p"]})
    return {
        "center": center, "track": track, "corrected": corrected, "offsetKm": offset_km,
        "minStation": min_st, "maxGust": max(gusts, key=lambda o: o["gust"]) if gusts else None,
        "maxWind": max(winds, key=lambda o: o["wind"]) if winds else None,
        "stations": near_list, "nStations": len(valid), "updated": datetime.now(timezone.utc).isoformat(),
        "sources": "IPMA (estações, hora a hora) + METAR (aeroportos, 30 min)",
    }


# ================= outros eventos naturais =================
# sismos (IPMA), incêndios (Fogos.pt + risco do IPMA), calor e frio (avisos do IPMA).
# Cada tipo tem os seus dados: num sismo interessa o hipocentro e a magnitude, não o vento.
PLACES = CITIES + [
    ("Braga", 41.55, -8.42), ("Bragança", 41.81, -6.76), ("Vila Real", 41.30, -7.74), ("Aveiro", 40.64, -8.65),
    ("Viseu", 40.66, -7.91), ("Guarda", 40.54, -7.27), ("Castelo Branco", 39.82, -7.49), ("Leiria", 39.74, -8.81),
    ("Santarém", 39.24, -8.69), ("Portalegre", 39.29, -7.43), ("Setúbal", 38.52, -8.89), ("Beja", 38.01, -7.86),
    ("Sines", 37.96, -8.87), ("Lagos", 37.10, -8.67), ("Angra do Heroísmo", 38.65, -27.22), ("Horta", 38.53, -28.63),
    ("Santa Cruz das Flores", 39.45, -31.13), ("Vila do Porto", 36.95, -25.15), ("Porto Santo", 33.06, -16.34),
    ("Madrid", 40.42, -3.70), ("Sevilha", 37.39, -5.98), ("Huelva", 37.26, -6.95), ("Badajoz", 38.88, -6.97),
]
DIRS_PT = {"N": "N", "S": "S", "E": "E", "W": "O", "NE": "NE", "NW": "NO", "SE": "SE", "SW": "SO",
           "NNE": "NNE", "NNW": "NNO", "ENE": "ENE", "ESE": "ESE", "SSE": "SSE", "SSW": "SSO", "WNW": "ONO", "WSW": "OSO"}
RCM_NAME = {1: "reduzido", 2: "moderado", 3: "elevado", 4: "muito elevado", 5: "máximo"}
FIRE_SEEN = {}      # incêndios vistos (fica a última informação de cada um, mesmo depois de acabar)
WARN_SEEN = {}      # calor/frio: último aviso visto
CAT_ORDER = ["Tempestades", "Cheias", "Incêndios", "Sismos", "Vulcões", "Tsunamis", "Secas", "Calor e frio", "Vista geral",
             "Catástrofes humanas"]
KIND_CAT = {"tempestade": "Tempestades", "incendio": "Incêndios", "incendios": "Incêndios", "sismo": "Sismos",
            "sismos": "Sismos", "calor": "Calor e frio", "frio": "Calor e frio", "geral": "Vista geral",
            "cheia": "Cheias", "cheias": "Cheias", "vulcao": "Vulcões", "vulcoes": "Vulcões",
            "tsunami": "Tsunamis", "tsunamis": "Tsunamis", "seca": "Secas", "secas": "Secas",
            "guerra": "Catástrofes humanas", "guerras": "Catástrofes humanas", "terror": "Catástrofes humanas",
            "terrorismo": "Catástrofes humanas", "ciber": "Catástrofes humanas", "ciberataques": "Catástrofes humanas",
            "surto": "Catástrofes humanas", "surtos": "Catástrofes humanas", "acidnuc": "Catástrofes humanas", "nucleares": "Catástrofes humanas"}


def bearing(a, b):
    import math
    la1, la2, dlo = math.radians(a[0]), math.radians(b[0]), math.radians(b[1] - a[1])
    y = math.sin(dlo) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlo)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SO", "O", "NO"][round(deg / 45) % 8]


def nearest_places(lat, lon, n=5):
    out = sorted(({"name": p[0], "km": round(km((lat, lon), (p[1], p[2]))), "dir": bearing((p[1], p[2]), (lat, lon))}
                  for p in PLACES), key=lambda x: x["km"])
    return out[:n]


def nice_region(r):
    """IPMA: "N  S.Brás de Alportel" = a norte de S. Brás de Alportel."""
    r = re.sub(r"\s+", " ", r or "").strip()
    m = re.match(r"^([NSEW]{1,3}) (.+)$", r)
    return f"{DIRS_PT.get(m.group(1), m.group(1))} de {m.group(2)}" if m and m.group(1) in DIRS_PT else r or "—"


def fetch_quakes():
    out = []
    for area, zona in ((7, "Continente e Madeira"), (3, "Açores")):
        d = json.loads(get_cached(f"https://api.ipma.pt/open-data/observation/seismic/{area}.json", ttl=100, timeout=30))
        for q in d.get("data") or []:
            try:
                lat, lon = float(q["lat"]), float(q["lon"])
                t = datetime.fromisoformat(q["time"]).replace(tzinfo=timezone.utc)
            except (TypeError, ValueError, KeyError):
                continue
            mag = float(q.get("magnitud") or -99)
            out.append({"id": "sismo-" + t.strftime("%Y%m%d%H%M%S"), "t": t.isoformat(), "lat": lat, "lon": lon,
                        "depth": q.get("depth"), "mag": None if mag < -9 else round(mag, 1),
                        "magType": q.get("magType"), "region": nice_region(q.get("obsRegion")), "zona": zona,
                        "sensed": bool(q.get("sensed")), "degree": q.get("degree") or None})
    out.sort(key=lambda q: q["t"], reverse=True)
    return out


FIRES_BLOCK = {"until": 0.0}


FIRES_SOURCE = {"name": None, "note": None}
ANEPC_URL = ("https://services-eu1.arcgis.com/VlrHb7fn5ewYhX6y/arcgis/rest/services/OcorrenciasSite/FeatureServer/0/query?"
             + urllib.parse.urlencode({"where": "1=1", "outFields": "*", "f": "json", "outSR": 4326, "resultRecordCount": 2000}))


def fetch_fires():
    """Fonte principal: Fogos.pt (traz também o tempo no local). Plano B: a camada pública de ocorrências
    da Proteção Civil (ANEPC). O Fogos.pt limita os pedidos por IP: se responder 429, espera o tempo que
    ele pedir (Retry-After) e, entretanto, usa a ANEPC."""
    fx = os.environ.get("RADAR_FIXTURES")  # testes: dados de exemplo em vez da rede
    if fx and os.path.exists(os.path.join(fx, "fires.json")):
        with open(os.path.join(fx, "fires.json"), encoding="utf-8") as f:
            return parse_fires(json.load(f))
    why = None
    if time.time() < FIRES_BLOCK["until"]:
        why = f"Fogos.pt pediu para esperar até às {datetime.fromtimestamp(FIRES_BLOCK['until']).strftime('%H:%M')}"
    else:
        try:
            out = parse_fires(json.loads(get_cached("https://api.fogos.pt/new/fires", ttl=240, timeout=30)))
            FIRES_SOURCE.update(name="Fogos.pt", note=None)
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(max(int(e.headers.get("Retry-After") or 900), 300), 3600)
                FIRES_BLOCK["until"] = time.time() + wait
                why = f"Fogos.pt pediu para esperar {wait // 60} min"
            else:
                why = f"Fogos.pt em falha ({e.code})"
        except Exception as e:
            why = f"Fogos.pt em falha ({type(e).__name__})"
    out = fetch_fires_anepc()  # plano B
    FIRES_SOURCE.update(name="Proteção Civil (ANEPC)", note=why)
    return out


ANEPC_COLOR = {"Em Curso": "#B81E1F", "Em Resolução": "#E9A216", "Em Despacho": "#FF6E02", "Conclusão": "#65C4ED", "Vigilância": "#65C4ED"}


def anepc_occurrences():
    return json.loads(get_cached(ANEPC_URL, ttl=100, timeout=30)).get("features") or []


def fetch_fires_anepc():
    """Ocorrências da ANEPC de natureza incêndio (códigos 21xx urbanos/industriais e 31xx rurais)."""
    out = []
    for ft in anepc_occurrences():
        a = ft.get("attributes") or {}
        cod = a.get("CodNatureza") or 0
        if not (2100 <= cod < 2200 or 3100 <= cod < 3200) or a.get("Latitude") is None:
            continue
        estado = a.get("EstadoAgrupado") or a.get("EstadoOcorrencia") or ""
        out.append({
            "id": f"fogo-{a.get('Numero')}", "lat": a["Latitude"], "lon": a["Longitude"],
            "district": a.get("SubRegiao") or a.get("Regiao"), "concelho": a.get("Concelho") or "",
            "freguesia": a.get("Freguesia"), "local": (a.get("Localidade") or "").title() or None, "dico": None,
            "natureza": re.sub(r"^\d+\s*-\s*", "", a.get("Natureza") or ""), "status": a.get("EstadoOcorrencia") or estado,
            "statusColor": ANEPC_COLOR.get(estado, "#FF6E02"),
            "man": a.get("Operacionais") or 0, "terrain": a.get("MeiosTerrestres") or 0, "aerial": a.get("MeiosAereos") or 0,
            "important": (a.get("Operacionais") or 0) >= 100,
            "start": datetime.fromtimestamp(a["DataOcorrencia"] / 1000, timezone.utc).isoformat() if a.get("DataOcorrencia") else None,
            "weather": None, "src": "ANEPC",
        })
    return out


def parse_fires(d):
    out = []
    for f in d.get("data") or []:
        if f.get("isFire") is False or not f.get("lat"):
            continue
        w = f.get("weather") or {}
        out.append({
            "id": f"fogo-{f['id']}", "lat": f["lat"], "lon": f["lng"], "district": f.get("district"),
            "concelho": (f.get("concelho") or "").title().replace(" De ", " de ").replace(" Do ", " do ").replace(" Da ", " da "),
            "freguesia": f.get("freguesia"), "local": f.get("detailLocation") or f.get("localidade"),
            "dico": f.get("dico"), "natureza": f.get("natureza"), "status": f.get("status"),
            "statusColor": "#" + (f.get("statusColor") or "ff8a2a"),
            "man": f.get("man") or 0, "terrain": f.get("terrain") or 0, "aerial": f.get("aerial") or 0,
            "important": bool(f.get("important")),
            "start": datetime.fromtimestamp(f["dateTime"]["sec"], timezone.utc).isoformat() if f.get("dateTime") else None,
            "weather": {"station": w.get("stationLocation"), "km": w.get("stationDistance"), "temp": w.get("temperatura"),
                        "hum": w.get("humidade"), "windFrom": w.get("direccVento"), "t": w.get("date")} if w else None,
        })
    return out


def fetch_rcm():
    d = json.loads(get("https://api.ipma.pt/open-data/forecast/meteorology/rcm/rcm-d0.json", timeout=30))
    return {k: {"rcm": v["data"]["rcm"], "lat": v.get("latitude"), "lon": v.get("longitude")} for k, v in (d.get("local") or {}).items()}


def rcm_for(f, rcm):
    """Risco de incêndio do concelho: pelo código (Fogos.pt) ou pelo concelho mais perto (ANEPC não traz o código)."""
    if f.get("dico") in rcm:
        return rcm[f["dico"]]["rcm"]
    near = [v for v in rcm.values() if v.get("lat") is not None]
    if not near:
        return None
    return min(near, key=lambda v: km((v["lat"], v["lon"]), (f["lat"], f["lon"])))["rcm"]


# ---------- cheias: caudal dos rios (modelo GloFAS do Copernicus, via Open-Meteo) ----------
RIVERS = [  # estação de referência (o servidor procura à volta a célula do rio principal)
    ("Minho", "Valença", 42.03, -8.64), ("Lima", "Ponte de Lima", 41.77, -8.58), ("Douro", "Régua", 41.16, -7.79),
    ("Douro", "Crestuma", 41.07, -8.48), ("Vouga", "Águeda", 40.61, -8.45), ("Mondego", "Coimbra", 40.20, -8.43),
    ("Tejo", "Abrantes", 39.45, -8.20), ("Tejo", "Santarém", 39.23, -8.68), ("Sado", "Alcácer do Sal", 38.37, -8.51),
    ("Guadiana", "Mértola", 37.64, -7.66),
]
RIVER_CACHE = os.path.join(HERE, "cache_rios.json")
FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood?"


def river_setup():
    """Uma vez (fica em cache 30 dias): escolhe a célula de cada rio e calcula o "normal"
    a partir de 10 anos de caudais (percentis 50, 95 e 99 e o máximo)."""
    try:
        with open(RIVER_CACHE, encoding="utf-8") as f:
            c = json.load(f)
        if time.time() - c["at"] < 30 * 86400 and len(c["rivers"]) == len(RIVERS):
            return c["rivers"]
    except (OSError, ValueError, KeyError):
        pass
    off = [(a * .05, b * .05) for a in (-1, 0, 1) for b in (-1, 0, 1)]
    pts = [(r[2] + a, r[3] + b) for r in RIVERS for a, b in off]
    d = json.loads(get(FLOOD_URL + "latitude=" + ",".join(f"{p[0]:.3f}" for p in pts) + "&longitude="
                       + ",".join(f"{p[1]:.3f}" for p in pts) + "&daily=river_discharge&past_days=92&forecast_days=1", timeout=90))
    cells = []
    for i, r in enumerate(RIVERS):
        best = max(d[i * 9:(i + 1) * 9], key=lambda p: sorted(x or 0 for x in p["daily"]["river_discharge"])[46])
        cells.append((best["latitude"], best["longitude"]))
    y = datetime.now().year
    h = json.loads(get(FLOOD_URL + "latitude=" + ",".join(f"{c[0]:.4f}" for c in cells) + "&longitude="
                       + ",".join(f"{c[1]:.4f}" for c in cells)
                       + f"&daily=river_discharge&start_date={y - 10}-01-01&end_date={y - 1}-12-31", timeout=120))
    out = []
    for r, c, p in zip(RIVERS, cells, h):
        q = sorted(x for x in p["daily"]["river_discharge"] if x is not None)
        pc = lambda f: round(q[min(len(q) - 1, int(f * len(q)))], 1)
        out.append({"rio": r[0], "local": r[1], "lat": c[0], "lon": c[1], "p50": pc(.5), "p95": pc(.95), "p99": pc(.99),
                    "max": round(q[-1], 1), "anos": f"{y - 10}–{y - 1}"})
    with open(RIVER_CACHE, "w", encoding="utf-8") as f:
        json.dump({"at": time.time(), "rivers": out}, f, ensure_ascii=False)
    return out


def flood_level(v, r):
    return 3 if v >= r["max"] else 2 if v >= r["p99"] else 1 if v >= r["p95"] else 0


FLOOD_NAME = ["normal", "acima do normal", "cheia", "cheia excecional"]


def fetch_rivers():
    rs = river_setup()
    d = json.loads(get_cached(FLOOD_URL + "latitude=" + ",".join(f"{r['lat']:.4f}" for r in rs) + "&longitude="
                              + ",".join(f"{r['lon']:.4f}" for r in rs)
                              + "&daily=river_discharge,river_discharge_max&past_days=30&forecast_days=10", ttl=3 * 3600, timeout=60))
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for r, p in zip(rs, d):
        t, q = p["daily"]["time"], p["daily"]["river_discharge"]
        qmax = p["daily"].get("river_discharge_max") or q
        i0 = t.index(today) if today in t else 30
        fut = [(t[i], q[i], qmax[i]) for i in range(i0, len(t)) if q[i] is not None]
        peak = max(fut, key=lambda x: x[1]) if fut else (today, None, None)
        now_q = q[i0]
        lvl_now, lvl_peak = flood_level(now_q or 0, r), flood_level(peak[1] or 0, r)
        out.append(dict(r, id="rio-" + slug(r["rio"] + "-" + r["local"]), now=now_q, peak=peak[1], peakDay=peak[0],
                        peakMax=peak[2], level=max(lvl_now, lvl_peak), levelNow=lvl_now,
                        pct=round(100 * (peak[1] or 0) / (r["p99"] or 1)),  # pico em % do limiar de cheia
                        trend="a subir" if (q[min(i0 + 2, len(q) - 1)] or 0) > (now_q or 0) * 1.1 else
                              "a descer" if (q[min(i0 + 2, len(q) - 1)] or 0) < (now_q or 0) * .9 else "estável",
                        series=[{"t": t[i], "q": q[i]} for i in range(len(t))], today=today))
    return out


# ---------- vulcões: relatório semanal do Smithsonian (GVP) + vigilância sísmica dos vulcões dos Açores ----------
AZORES_VOLCANOES = [
    ("Sete Cidades", "São Miguel", 37.86, -25.79), ("Fogo (Água de Pau)", "São Miguel", 37.77, -25.47),
    ("Furnas", "São Miguel", 37.77, -25.32), ("Santa Bárbara", "Terceira", 38.73, -27.32),
    ("Pico", "Pico", 38.47, -28.40), ("Caldeira do Faial", "Faial", 38.58, -28.71), ("São Jorge (fissural)", "São Jorge", 38.65, -28.08),
    ("Graciosa", "Graciosa", 39.03, -28.00), ("Flores", "Flores", 39.45, -31.20),
]


def fetch_volcanoes():
    x = get_cached("https://volcano.si.edu/news/WeeklyVolcanoRSS.xml", ttl=3 * 3600, timeout=40)
    out = []
    for it in x.split("<item>")[1:]:
        title = tag(it, "title")
        m = re.match(r"^(.+?) \((.+?)\) - Report for (.+?) - (.+)$", title)
        pt = re.findall(r"<georss:point>\s*([-\d.]+)\s+([-\d.]+)", it)
        if not m or not pt:
            continue
        lat, lon = float(pt[0][0]), float(pt[0][1])
        desc = re.sub(r"\s+", " ", tag(it, "description"))
        new = "new" in m.group(4).lower()
        period = m.group(3)
        for en, pt in (("January", "jan"), ("February", "fev"), ("March", "mar"), ("April", "abr"), ("May", "mai"), ("June", "jun"),
                       ("July", "jul"), ("August", "ago"), ("September", "set"), ("October", "out"), ("November", "nov"), ("December", "dez")):
            period = period.replace(" " + en, " " + pt)
        period = period.replace("-", " a ")
        out.append({"id": "vulcao-" + slug(m.group(1)), "name": m.group(1), "country": m.group(2), "period": period,
                    "status": "Nova atividade eruptiva" if new else "Atividade eruptiva em curso", "new": new,
                    "lat": lat, "lon": lon, "link": tag(it, "link"), "excerpt": desc[:320] + ("…" if len(desc) > 320 else ""),
                    "kmLisboa": round(km((lat, lon), (38.72, -9.14))),
                    "near": -40 <= lon <= 45 and 20 <= lat <= 72})  # Europa e Atlântico (cinzas podem chegar cá)
    out.sort(key=lambda v: (not v["near"], not v["new"], v["kmLisboa"]))
    return out


def azores_volcano_watch():
    """Sismos do IPMA a menos de 15 km de cada vulcão dos Açores nos últimos 7 dias (indicador de agitação)."""
    now = time.time()
    qs = [q for q in state.get("quakes") or [] if now - datetime.fromisoformat(q["t"]).timestamp() <= 7 * 86400]
    out = []
    for name, ilha, lat, lon in AZORES_VOLCANOES:
        near = [q for q in qs if km((q["lat"], q["lon"]), (lat, lon)) <= 15]
        mags = [q["mag"] for q in near if q["mag"] is not None]
        out.append({"name": name, "ilha": ilha, "lat": lat, "lon": lon, "n7": len(near), "maxMag": max(mags) if mags else None,
                    "sensed": sum(q["sensed"] for q in near), "quakes": near[:30],
                    "state": "agitação sísmica" if len(near) >= 20 or (mags and max(mags) >= 3.5) else
                             "alguma sismicidade" if len(near) >= 5 else "calmo"})
    return out


# ---------- tsunamis: boletins dos centros de alerta da NOAA + sismos fortes no mar ao largo de Portugal ----------
TSU_FEEDS = [("PTWC", "https://www.tsunami.gov/events/xml/PHEBAtom.xml"), ("NTWC", "https://www.tsunami.gov/events/xml/PAAQAtom.xml")]
TSU_LEVEL = {"warning": 3, "threat": 2, "watch": 2, "advisory": 2, "information": 1}
TSU_PT = {"warning": "Aviso de tsunami", "threat": "Ameaça de tsunami", "watch": "Vigilância de tsunami",
          "advisory": "Alerta de tsunami", "information": "Informação (sem ameaça)"}
TSU_SPEED_KMH = 700  # velocidade típica de um tsunami no oceano profundo (~4 km de fundo)


def fetch_tsunamis():
    out = []
    for centro, url in TSU_FEEDS:
        x = get_cached(url, ttl=100, timeout=30)
        for e in x.split("<entry>")[1:]:
            s = re.sub(r"<[^>]+>", " ", html.unescape(e))
            cat = (re.search(r"Category:\s*([A-Za-z]+)", s) or [None, "Information"])[1].lower()
            mag = re.search(r"Preliminary Magnitude:\s*([\d.]+)", s)
            lat, lon = re.search(r"<geo:lat>([-\d.]+)", e), re.search(r"<geo:long>([-\d.]+)", e)
            link = re.search(r'title="Bulletin" href="([^"]+)"', e)
            if not lat or not lon:
                continue
            t = tag(e, "updated")
            la, lo = float(lat.group(1)), float(lon.group(1))
            out.append({"id": "tsunami-" + slug(centro + "-" + t), "centro": centro, "region": tag(e, "title").title(),
                        "t": t.replace("Z", "+00:00"), "lat": la, "lon": lo, "mag": float(mag.group(1)) if mag else None,
                        "cat": cat, "catPt": TSU_PT.get(cat, cat), "level": TSU_LEVEL.get(cat, 1),
                        "link": link.group(1) if link else "https://www.tsunami.gov/",
                        "kmLisboa": round(km((la, lo), (38.72, -9.14)))})
    # Portugal: sismo forte no mar (M >= 6, pouco profundo) nas últimas 6 h = possível tsunami (IPMA/ANEPC emitem o aviso oficial)
    now = time.time()
    for q in state.get("quakes") or []:
        if (q["mag"] or 0) >= 6.0 and (q["depth"] or 0) <= 100 and now - datetime.fromisoformat(q["t"]).timestamp() <= 6 * 3600:
            out.append({"id": "tsunami-pt-" + q["id"], "centro": "IPMA (sismo)", "region": q["region"], "t": q["t"],
                        "lat": q["lat"], "lon": q["lon"], "mag": q["mag"], "cat": "watch", "level": 3 if q["mag"] >= 7 else 2,
                        "catPt": "Sismo forte no mar: possível tsunami — segue os avisos do IPMA e da Proteção Civil",
                        "link": "https://www.ipma.pt/pt/geofisica/sismologia/", "kmLisboa": round(km((q["lat"], q["lon"]), (38.72, -9.14)))})
    for t in out:
        t["etaLisboaH"] = round(t["kmLisboa"] / TSU_SPEED_KMH, 1)
    out.sort(key=lambda t: t["t"], reverse=True)
    return out


# ---------- secas: chuva acumulada (ERA5) comparada com 1991–2020, por distrito (ao estilo do índice SPI) ----------
DISTRICTS = [("Aveiro", 40.64, -8.65), ("Beja", 38.01, -7.86), ("Braga", 41.55, -8.42), ("Bragança", 41.81, -6.76),
             ("Castelo Branco", 39.82, -7.49), ("Coimbra", 40.21, -8.43), ("Évora", 38.57, -7.91), ("Faro", 37.02, -7.93),
             ("Guarda", 40.54, -7.27), ("Leiria", 39.74, -8.81), ("Lisboa", 38.72, -9.14), ("Portalegre", 39.29, -7.43),
             ("Porto", 41.15, -8.61), ("Santarém", 39.24, -8.69), ("Setúbal", 38.52, -8.89), ("Viana do Castelo", 41.69, -8.83),
             ("Vila Real", 41.30, -7.74), ("Viseu", 40.66, -7.91), ("Madeira", 32.65, -16.91), ("Açores", 37.74, -25.67)]
POWER = "https://power.larc.nasa.gov/api/temporal/{res}/point?parameters=PRECTOTCORR&community=AG&format=JSON"
DROUGHT_NAME = ["normal", "seca moderada", "seca severa", "seca extrema"]


def fetch_droughts():
    """Índice ao estilo SPI (3 e 12 meses): chuva acumulada até ao último dia com dados, comparada com
    a mesma janela de cada ano de 1991–2020. Fonte: NASA POWER (mensal + diário para os meses mais recentes)."""
    import calendar
    from datetime import date, timedelta
    today = date.today()
    # os dias mais recentes (que a NASA ainda não tem) vêm do Open-Meteo: um pedido leve para os 20 distritos
    om = json.loads(get_cached("https://api.open-meteo.com/v1/forecast?latitude=" + ",".join(str(d[1]) for d in DISTRICTS)
                               + "&longitude=" + ",".join(str(d[2]) for d in DISTRICTS)
                               + "&daily=precipitation_sum&past_days=14&forecast_days=1&timezone=UTC", ttl=3 * 3600, timeout=60))
    out = []
    for (name, lat, lon), omd in zip(DISTRICTS, om):
        base = POWER.format(res="monthly") + f"&latitude={lat}&longitude={lon}&start=1991&end={today.year}"
        mon = json.loads(get_cached(base, ttl=7 * 86400, timeout=60))["properties"]["parameter"]["PRECTOTCORR"]
        months = {}
        for k, v in mon.items():
            y, m = int(k[:4]), int(k[4:])
            if m <= 12 and v is not None and v >= 0:
                months[(y, m)] = v * calendar.monthrange(y, m)[1]  # mm/dia -> mm no mês
        last_full = max(months)
        first_missing = date(last_full[0] + (last_full[1] == 12), last_full[1] % 12 + 1, 1)
        dly = json.loads(get_cached(POWER.format(res="daily") + f"&latitude={lat}&longitude={lon}"
                                    f"&start={first_missing:%Y%m%d}&end={today:%Y%m%d}", ttl=12 * 3600, timeout=60))
        days = {k: v for k, v in dly["properties"]["parameter"]["PRECTOTCORR"].items() if v is not None and v >= 0}
        last_power = max(days) if days else ""
        for t, v in zip(omd["daily"]["time"], omd["daily"]["precipitation_sum"]):
            k = t.replace("-", "")
            if k > last_power and t < today.isoformat() and v is not None:  # até ontem (hoje ainda não acabou)
                days[k] = v
        end = date.fromisoformat(f"{max(days)[:4]}-{max(days)[4:6]}-{max(days)[6:]}") if days else \
            date(last_full[0], last_full[1], calendar.monthrange(*last_full)[1])
        extra = {}
        for k, v in days.items():  # meses que ainda só existem no diário (o último fica parcial)
            ym = (int(k[:4]), int(k[4:6]))
            if ym > last_full:
                extra[ym] = extra.get(ym, 0) + v
        months.update(extra)
        cur_month = (end.year, end.month)
        partial = months.get(cur_month, 0)
        frac = end.day / calendar.monthrange(*cur_month)[1]
        res = {"name": name, "lat": lat, "lon": lon, "until": end.isoformat(), "id": "seca-" + slug(name),
               "powerUntil": f"{last_power[:4]}-{last_power[4:6]}-{last_power[6:]}" if last_power else None}

        def back(y, m, n):  # mês n meses antes de (y, m)
            t = y * 12 + (m - 1) - n
            return t // 12, t % 12 + 1
        for win in (3, 12):
            full = [back(*cur_month, i) for i in range(1, win)]
            now_sum = partial + sum(months.get(ym, 0) for ym in full)
            sums = []
            for y in range(1991, 2021):
                dy = y - cur_month[0]
                s = sum(months.get((a + dy, b), 0) for a, b in full) + frac * months.get((y, cur_month[1]), 0)
                sums.append(s)
            mean = sum(sums) / len(sums)
            rank = sum(s < now_sum for s in sums) / len(sums)  # 0 = mais seco do que todos os 30 anos
            lvl = 3 if rank <= .05 else 2 if rank <= .10 else 1 if rank <= .20 else 0
            res.update({f"p{win}": round(now_sum), f"normal{win}": round(mean), f"pct{win}": round(100 * now_sum / mean) if mean else None,
                        f"rank{win}": round(rank, 2), f"level{win}": lvl,
                        f"state{win}": DROUGHT_NAME[lvl] if lvl else ("mais chuvoso que o normal" if rank >= .8 else "normal")})
        res["level"] = max(res["level3"], res["level12"])
        res["months"] = [{"ym": f"{a}-{b:02d}", "mm": round(months.get((a, b), 0) if (a, b) != cur_month else partial),
                          "normal": round(sum(months.get((y, b), 0) for y in range(1991, 2021)) / 30)}
                         for a, b in (back(*cur_month, i) for i in range(11, -1, -1))]
        out.append(res)
    return out


# ================= eventos que já aconteceram (até 1 ano e meio), de dados reais =================
# incêndios: áreas ardidas do EFFIS (Copernicus) · sismos e tsunamis: catálogo do USGS · cheias: caudais do GloFAS
# secas: chuva mensal (NASA POWER) · calor/frio: temperaturas diárias (Open-Meteo, arquivo ERA5) · vulcões: relatórios guardados
HIST_CACHE = os.path.join(HERE, "cache_historico.json")
HIST = {"events": {}, "at": 0}
MES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
mes_ano = lambda iso: f"{MES[int(iso[5:7]) - 1]} {iso[:4]}"


def _simplify(geom, max_pts=160):
    """Reduz o polígono da área ardida (para o mapa)."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    out = []
    for poly in sorted(polys, key=lambda p: -len(p[0]))[:6]:
        ring = poly[0]; step = max(1, len(ring) // max_pts)
        out.append([[round(y, 4), round(x, 4)] for x, y in ring[::step]])
    return out


def hist_fires(start):
    flt = ('<Filter><And><PropertyIsGreaterThanOrEqualTo><PropertyName>FIREDATE</PropertyName>'
           f'<Literal>{start}</Literal></PropertyIsGreaterThanOrEqualTo><PropertyIsGreaterThanOrEqualTo>'
           '<PropertyName>AREA_HA</PropertyName><Literal>1000</Literal></PropertyIsGreaterThanOrEqualTo>'
           '<PropertyIsEqualTo><PropertyName>COUNTRY</PropertyName><Literal>PT</Literal></PropertyIsEqualTo></And></Filter>')
    url = "https://maps.effis.emergency.copernicus.eu/effis?" + urllib.parse.urlencode(
        {"service": "WFS", "version": "1.1.0", "request": "GetFeature", "typename": "ms:modis.ba.poly", "outputformat": "geojson", "FILTER": flt})
    fs = json.loads(get(url, timeout=300))["features"]
    out = []
    for f in sorted(fs, key=lambda f: -float(f["properties"]["AREA_HA"]))[:14]:
        p, polys = f["properties"], _simplify(f["geometry"])
        pts = [pt for poly in polys for pt in poly]
        lat, lon = sum(x[0] for x in pts) / len(pts), sum(x[1] for x in pts) / len(pts)
        cover = {"floresta": float(p.get("BROADLEA") or 0) + float(p.get("CONIFER") or 0) + float(p.get("MIXED") or 0),
                 "mato": float(p.get("SCLEROPH") or 0) + float(p.get("TRANSIT") or 0) + float(p.get("OTHERNATLC") or 0),
                 "agrícola": float(p.get("AGRIAREAS") or 0), "urbano": float(p.get("ARTIFSURF") or 0)}
        t0, t1 = p["FIREDATE"][:10], (p.get("FINALDATE") or p["FIREDATE"])[:10]
        out.append({"id": "past-fogo-" + slug(p["COMMUNE"] + "-" + t0), "kind": "incendio", "past": True,
                    "nome": p["COMMUNE"].split(" (")[0].split(" e ")[0], "freguesia": p["COMMUNE"], "district": p["PROVINCE"],
                    "concelho": p["COMMUNE"].split(" (")[0], "lat": round(lat, 4), "lon": round(lon, 4), "areaHa": round(float(p["AREA_HA"])),
                    "start": t0, "end": t1, "days": (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).days + 1,
                    "cover": {k: round(v) for k, v in cover.items()}, "natura2000": round(float(p.get("PERCNA2K") or 0)),
                    "polys": polys, "nearest": nearest_places(lat, lon, 3), "t": t0, "source": "EFFIS (Copernicus)"})
    return out


def hist_quakes(start):
    q = json.loads(get("https://earthquake.usgs.gov/fdsnws/event/1/query?" + urllib.parse.urlencode(
        {"format": "geojson", "starttime": start, "minmagnitude": 4.5, "minlatitude": 30, "maxlatitude": 45,
         "minlongitude": -33, "maxlongitude": -5, "orderby": "magnitude"}), timeout=60))["features"]
    allq = [{"lat": f["geometry"]["coordinates"][1], "lon": f["geometry"]["coordinates"][0], "depth": round(f["geometry"]["coordinates"][2]),
             "mag": f["properties"]["mag"], "t": datetime.fromtimestamp(f["properties"]["time"] / 1000, timezone.utc).isoformat(),
             "region": f["properties"]["place"], "felt": f["properties"].get("felt"), "url": f["properties"].get("url")} for f in q]
    out = []
    for e in allq[:10]:
        t0 = datetime.fromisoformat(e["t"]).timestamp()
        after = [x for x in allq if 0 < datetime.fromisoformat(x["t"]).timestamp() - t0 <= 7 * 86400 and km((x["lat"], x["lon"]), (e["lat"], e["lon"])) <= 60]
        out.append(dict(e, id="past-sismo-" + e["t"][:19].replace(":", "").replace("-", ""), kind="sismo", past=True,
                        nome=f"M{e['mag']:.1f} · {e['region']}", magType="w", zona="Atlântico/Portugal (USGS)",
                        sensed=bool(e["felt"]), degree=None, aftershocks=[dict(a, sensed=False) for a in after], nAfter=len(after),
                        maxAfter=max((a["mag"] for a in after), default=None), energy=tnt(e["mag"]),
                        nearest=nearest_places(e["lat"], e["lon"]), source="USGS"))
    return out


def hist_tsunamis(start):
    q = json.loads(get("https://earthquake.usgs.gov/fdsnws/event/1/query?" + urllib.parse.urlencode(
        {"format": "geojson", "starttime": start, "minmagnitude": 7.0, "orderby": "magnitude"}), timeout=60))["features"]
    out = []
    for f in [f for f in q if f["properties"].get("tsunami")][:8]:
        lon, lat = f["geometry"]["coordinates"][:2]
        t = datetime.fromtimestamp(f["properties"]["time"] / 1000, timezone.utc).isoformat()
        kml = round(km((lat, lon), (38.72, -9.14)))
        out.append({"id": "past-tsunami-" + t[:10] + "-" + slug(f["properties"]["place"])[:20], "kind": "tsunami", "past": True,
                    "nome": f["properties"]["place"], "region": f["properties"]["place"], "t": t, "lat": lat, "lon": lon,
                    "mag": f["properties"]["mag"], "cat": "information", "level": 1, "centro": "USGS",
                    "catPt": "Sismo forte com potencial de tsunami (registo USGS)", "link": f["properties"].get("url"),
                    "kmLisboa": kml, "etaLisboaH": round(kml / TSU_SPEED_KMH, 1), "nearest": nearest_places(lat, lon, 3), "source": "USGS"})
    return out


def hist_floods(start, end):
    rs = river_setup()
    f = json.loads(get(FLOOD_URL + "latitude=" + ",".join(f"{r['lat']:.4f}" for r in rs) + "&longitude=" + ",".join(f"{r['lon']:.4f}" for r in rs)
                       + f"&daily=river_discharge&start_date={start}&end_date={end}", timeout=120))
    out = []
    for r, p in zip(rs, f):
        t, q = p["daily"]["time"], p["daily"]["river_discharge"]
        i = 0
        while i < len(q):  # episódios: dias seguidos acima do limiar de cheia (percentil 99)
            if q[i] is not None and q[i] >= r["p99"]:
                j = i
                while j + 1 < len(q) and q[j + 1] is not None and q[j + 1] >= r["p99"] * .8:
                    j += 1
                k = max(range(i, j + 1), key=lambda x: q[x])
                lo, hi = max(0, k - 30), min(len(q), k + 11)
                lvl = flood_level(q[k], r)
                out.append(dict(r, id=f"past-cheia-{slug(r['rio'] + '-' + r['local'])}-{t[k]}", kind="cheia", past=True,
                                nome=f"{r['rio']} em {r['local']}", now=q[k], peak=q[k], peakDay=t[k], peakMax=None,
                                level=lvl, levelNow=lvl, levelName=FLOOD_NAME[lvl], levelNowName=FLOOD_NAME[lvl],
                                pct=round(100 * q[k] / r["p99"]), trend=f"{j - i + 1} dias acima do limiar",
                                start=t[i], end=t[j], series=[{"t": t[x], "q": q[x]} for x in range(lo, hi)], today=t[k],
                                nearest=nearest_places(r["lat"], r["lon"], 3), rainWarnings=[], t=t[k], source="Copernicus GloFAS"))
                i = j + 1
            else:
                i += 1
    out.sort(key=lambda e: -e["pct"])
    return out[:12]


def hist_temps(start, end):
    D = DISTRICTS
    r = json.loads(get("https://archive-api.open-meteo.com/v1/archive?latitude=" + ",".join(str(d[1]) for d in D)
                       + "&longitude=" + ",".join(str(d[2]) for d in D) + f"&start_date={start}&end_date={end}"
                       "&daily=temperature_2m_max,temperature_2m_min&timezone=Europe%2FLisbon", timeout=120))
    times = r[0]["daily"]["time"]
    out = []
    for kind, var, test, need, nome in (("calor", "temperature_2m_max", lambda v: v >= 38, 3, "Onda de calor"),
                                        ("frio", "temperature_2m_min", lambda v: v <= -2, 4, "Vaga de frio")):
        hits = [[(D[di][0], x["daily"][var][ti]) for di, x in enumerate(r) if x["daily"][var][ti] is not None and test(x["daily"][var][ti])]
                for ti in range(len(times))]
        i = 0
        while i < len(times):
            if len(hits[i]) >= need:
                j = i
                while j + 2 < len(times) and (len(hits[j + 1]) >= need or len(hits[j + 2]) >= need):  # tolera 1 dia de pausa
                    j += 1 if len(hits[j + 1]) >= need else 2
                days = range(i, j + 1)
                ext = (max if kind == "calor" else min)((h for d in days for h in hits[d]), key=lambda h: h[1])
                peak_day = max(days, key=lambda d: len(hits[d]))
                cities = [{"city": D[di][0], "lat": D[di][1], "lon": D[di][2],
                           "max": max(x["daily"]["temperature_2m_max"][d] for d in days) if kind == "calor" else None,
                           "min": min(x["daily"]["temperature_2m_min"][d] for d in days) if kind == "frio" else None,
                           "now": None, "max2": None, "min2": None} for di, x in enumerate(r)]
                a, b = times[i], times[j]
                quando = (f"{int(a[8:])} {MES[int(a[5:7]) - 1]} {a[:4]}" if a == b else
                          f"{int(a[8:])}–{int(b[8:])} {MES[int(a[5:7]) - 1]} {a[:4]}" if a[:7] == b[:7] else
                          f"{int(a[8:])} {MES[int(a[5:7]) - 1]}–{int(b[8:])} {MES[int(b[5:7]) - 1]} {b[:4]}")
                out.append({"id": f"past-{kind}-{times[i]}", "kind": kind, "past": True, "nome": f"{nome} de {quando}",
                            "start": times[i], "end": times[j], "days": j - i + 1, "t": times[i],
                            "extreme": {"place": ext[0], "value": ext[1]}, "nDistricts": len({h[0] for d in days for h in hits[d]}),
                            "peakDay": times[peak_day], "cities": cities, "warnings": [], "maxLevel": None, "ended": True,
                            "source": "Open-Meteo (arquivo ERA5)"})
                i = j + 1
            else:
                i += 1
    return out


def hist_droughts():
    """Episódios de seca nos últimos 18 meses: meses em que 5+ distritos estavam em seca severa a 3 meses."""
    import calendar
    from datetime import date
    today = date.today()
    per_month = {}  # "AAAA-MM" -> [(distrito, pct, nível)]
    pos = {}
    for name, lat, lon in DISTRICTS:
        pos[name] = (lat, lon)
        mon = json.loads(get_cached(POWER.format(res="monthly") + f"&latitude={lat}&longitude={lon}&start=1991&end={today.year}",
                                    ttl=7 * 86400, timeout=60))["properties"]["parameter"]["PRECTOTCORR"]
        mm = {(int(k[:4]), int(k[4:])): v * calendar.monthrange(int(k[:4]), int(k[4:]))[1]
              for k, v in mon.items() if int(k[4:]) <= 12 and v is not None and v >= 0}
        last = max(mm)
        for back in range(18):
            t = last[0] * 12 + last[1] - 1 - back
            y, m = t // 12, t % 12 + 1
            win = [((t - i) // 12, (t - i) % 12 + 1) for i in range(3)]
            if any(w not in mm for w in win):
                continue
            now = sum(mm[w] for w in win)
            clim = [sum(mm.get((a - (y - yy), b), 0) for a, b in win) for yy in range(1991, 2021)]
            rank = sum(c < now for c in clim) / len(clim)
            lvl = 3 if rank <= .05 else 2 if rank <= .10 else 1 if rank <= .20 else 0
            per_month.setdefault(f"{y}-{m:02d}", []).append((name, round(100 * now / (sum(clim) / len(clim) or 1)), lvl))
    months = sorted(per_month)
    bad = [m for m in months if sum(l >= 2 for _, _, l in per_month[m]) >= 5]
    out, i = [], 0
    while i < len(bad):
        j = i
        while j + 1 < len(bad) and months.index(bad[j + 1]) == months.index(bad[j]) + 1:
            j += 1
        ep = bad[i:j + 1]
        worst = max(ep, key=lambda m: sum(l >= 2 for _, _, l in per_month[m]))
        rows = per_month[worst]
        out.append({"id": f"past-seca-{ep[0]}", "kind": "seca", "past": True, "episode": True,
                    "nome": f"Seca de {mes_ano(ep[0] + '-01')}" + (f" a {mes_ano(ep[-1] + '-01')}" if len(ep) > 1 else ""),
                    "start": ep[0], "end": ep[-1], "months": len(ep), "worstMonth": worst, "t": ep[0] + "-01",
                    "districts": [{"name": n, "lat": pos[n][0], "lon": pos[n][1], "pct3": p, "level": l,
                                   "state3": DROUGHT_NAME[l] if l else "normal"} for n, p, l in sorted(rows, key=lambda r: r[1])],
                    "nSevere": sum(l >= 2 for _, _, l in rows), "nExt": sum(l >= 3 for _, _, l in rows),
                    "source": "NASA POWER (chuva mensal, 3 meses vs. 1991–2020)"})
        i = j + 1
    return out


def fetch_history():
    """Corre uma vez por dia (o pedido ao EFFIS demora ~2 min) e fica em cache_historico.json."""
    try:
        with open(HIST_CACHE, encoding="utf-8") as f:
            c = json.load(f)
        if time.time() - c["at"] < 20 * 3600:
            HIST.update(c)
            return {"n": len(c["events"]), "from": "cache"}
    except (OSError, ValueError, KeyError):
        pass
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - PAST_LIMIT_S))
    end = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 86400))
    evs, errs = {}, {}
    for name, fn in (("incêndios", lambda: hist_fires(start)), ("sismos", lambda: hist_quakes(start)),
                     ("tsunamis", lambda: hist_tsunamis(start)), ("cheias", lambda: hist_floods(start, end)),
                     ("calor/frio", lambda: hist_temps(start, end)), ("secas", hist_droughts)):
        try:
            for e in fn():
                evs[e["id"]] = e
        except Exception as e:
            errs[name] = str(e)
            print(f"[histórico] {name}: {e}")
    for v in (HIST.get("volcanoes") or {}).values():  # vulcões vistos em relatórios anteriores
        evs[v["id"]] = v
    HIST.update(events=evs, at=time.time(), errors=errs)
    with open(HIST_CACHE, "w", encoding="utf-8") as f:
        json.dump(HIST, f, ensure_ascii=False)
    return {"n": len(evs), "errors": errs}


_hist_running = threading.Event()


def start_history():
    """Corre o histórico numa linha à parte (não atrasa o resto do painel)."""
    if _hist_running.is_set():
        return {"running": True}
    def go():
        _hist_running.set()
        try:
            r = fetch_history()
            print(f"{time.strftime('%H:%M:%S')}  histórico: {r}")
        finally:
            _hist_running.clear()
    threading.Thread(target=go, daemon=True).start()
    return {"started": True}


HIST_META = {  # tipo, ícone, palavras das notícias
    "incendio": ("Incêndio", "🔥", ["incêndio", "fogo", "ardida", "chamas"]), "sismo": ("Sismo", "🌍", ["sismo", "terramoto", "abalo"]),
    "tsunami": ("Tsunami", "🌊", ["tsunami", "maremoto", "sismo"]), "cheia": ("Cheia", "🌊", ["cheia", "cheias", "inunda", "caudal"]),
    "calor": ("Onda de calor", "🌡", ["calor", "temperatura"]), "frio": ("Vaga de frio", "🥶", ["frio", "temperatura", "geada", "neve"]),
    "seca": ("Seca", "🏜", ["seca", "chuva"]), "vulcao": ("Vulcão", "🌋", ["vulcão", "erupção", "vulcao"]),
}


def hist_summary(m, d, base):
    k = d["kind"]
    dias = lambda n: f"{n} dia" if n == 1 else f"{n} dias"
    st = {"incendio": [["Área ardida", f"{d.get('areaHa', 0):,} ha".replace(",", " ")], ["Duração", dias(d.get("days"))], ["Início", d.get("start")]],
          "sismo": [["Magnitude", f"{d.get('mag', 0):.1f}"], ["Profundidade", f"{d.get('depth')} km"], ["Data", (d.get("t") or "")[:10]]],
          "tsunami": [["Sismo", f"M{d.get('mag')}"], ["Lisboa", f"{d.get('kmLisboa')} km"], ["Data", (d.get("t") or "")[:10]]],
          "cheia": [["Pico", f"{(d.get('peak') or 0):.0f} m³/s"], ["Do limiar", f"{d.get('pct')} %"], ["Dia do pico", d.get("peakDay")]],
          "calor": [["Máxima", f"{d.get('extreme', {}).get('value', 0):.0f} °C"], ["Duração", dias(d.get("days"))], ["Distritos", d.get("nDistricts")]],
          "frio": [["Mínima", f"{d.get('extreme', {}).get('value', 0):.0f} °C"], ["Duração", dias(d.get("days"))], ["Distritos", d.get("nDistricts")]],
          "seca": [["Distritos em seca", d.get("nSevere")], ["Extrema", d.get("nExt")], ["Meses", d.get("months")]],
          "vulcao": [["País", d.get("country")], ["Relatório", d.get("seenPeriod")], ["Lisboa", f"{d.get('kmLisboa')} km"]]}.get(k, [])
    head = {"incendio": f"{d.get('district')} · {d.get('freguesia')}", "sismo": d.get("region"), "tsunami": d.get("catPt"),
            "cheia": f"{d.get('levelName')} · {d.get('trend')}", "calor": f"{d.get('extreme', {}).get('place')} · {d.get('start')} a {d.get('end')}",
            "frio": f"{d.get('extreme', {}).get('place')} · {d.get('start')} a {d.get('end')}",
            "seca": f"pior mês: {mes_ano(d.get('worstMonth', '2000-01') + '-01')}", "vulcao": d.get("status")}.get(k)
    base.update(ativo=False, passada=True, alert=0, stats=st, headline=f"{head} · {d.get('source')}", quando=mes_ano(d["t"][:10]))
    return base


def remember_volcanoes(volcs):
    """O GVP só mostra a semana atual: guarda os vulcões da Europa/Atlântico para ficarem como 'já aconteceu'."""
    vols = HIST.setdefault("volcanoes", {})
    for v in volcs or []:
        if v["near"] or v["new"]:
            pid = "past-" + v["id"]
            vols[pid] = dict(v, id=pid, kind="vulcao", past=True, source="gvp", nome=v["name"], t=datetime.now(timezone.utc).isoformat(),
                             seenPeriod=v["period"], nearest=nearest_places(v["lat"], v["lon"], 3))


# ================= prevenção de catástrofes humanas: guerras =================
# Notícias (Google Notícias) separadas por tipo de arma, território (Ucrânia: DeepStateMap), classificação da
# Wikipédia e o nível de alerta PARA PORTUGAL (IA da Groq com regras estritas: vermelho só se for extremamente grave).
WARS = [  # id, nome, região, países (ISO numérico p/ o mapa), centro, palavras obrigatórias nas notícias, pesquisas, página da Wikipédia
    ("ucrania", "Guerra Rússia–Ucrânia", "Europa de Leste", ["804", "643"], (48.6, 35.5), ["ucrân", "rússia", "russo", "kiev", "putin", "zelensky", "donbass", "kursk"],
     ["guerra Ucrânia Rússia", "Ucrânia ataque mísseis drones", "Ucrânia linha da frente"], "Russo-Ukrainian war (2022–present)"),
    ("gaza", "Guerra Israel–Gaza", "Médio Oriente", ["376", "275"], (31.45, 34.4), ["gaza", "hamas", "israel", "palestin", "cisjordânia"],
     ["Gaza Israel guerra", "Gaza cessar-fogo Hamas"], "Gaza war"),
    ("irao", "Guerra do Irão", "Médio Oriente", ["364", "376"], (32.4, 53.7), ["irão", "irão", "teerão", "ormuz", "iraniano"],
     ["Irão guerra Israel", "Irão ataque Estados Unidos", "Estreito de Ormuz"], "2026 Iran war"),
    ("libano", "Guerra no Líbano", "Médio Oriente", ["422", "376"], (33.6, 35.7), ["líbano", "hezbollah", "beirute"],
     ["Líbano Israel Hezbollah"], "2026 Lebanon war"),
    ("sudao", "Guerra civil no Sudão", "África", ["729"], (15.5, 30.0), ["sudão", "cartum", "darfur", "rsf"],
     ["guerra Sudão", "Sudão Darfur RSF"], "Sudanese civil war (2023–present)"),
    ("congo", "Conflito na RD Congo", "África", ["180", "646"], (-1.7, 29.2), ["congo", "m23", "goma", "kivu", "ruanda"],
     ["RD Congo M23", "Congo Ruanda conflito"], "M23 campaign (2022–present)"),
    ("sahel", "Guerra no Sahel", "África", ["466", "854", "562"], (14.0, -1.5), ["sahel", "mali", "burkina", "níger", "jihadista"],
     ["Sahel jihadistas Mali Burkina Faso"], "War in the Sahel"),
    ("iemen", "Iémen e Mar Vermelho", "Médio Oriente", ["887"], (15.4, 45.0), ["iémen", "huthi", "houthi", "mar vermelho"],
     ["Iémen Huthis ataques", "Mar Vermelho navios ataque"], "Red Sea crisis"),
    ("myanmar", "Guerra civil em Myanmar", "Ásia", ["104"], (21.0, 96.0), ["myanmar", "birmânia", "junta militar"],
     ["Myanmar guerra civil junta"], "Myanmar civil war (2021–present)"),
    ("afpak", "Afeganistão–Paquistão", "Ásia", ["586", "004"], (33.5, 70.5), ["paquistão", "afeganistão", "talibã", "taliban"],
     ["Paquistão Afeganistão confrontos fronteira"], "Afghanistan–Pakistan border skirmishes"),
    ("cabodelgado", "Insurgência em Cabo Delgado", "África (lusófona)", ["508"], (-12.3, 40.0), ["cabo delgado", "moçambique", "insurg"],
     ["Cabo Delgado insurgência Moçambique"], "Insurgency in Cabo Delgado"),
]
# principais direções de ataque noticiadas (esquema indicativo, não é medição): de -> para
WAR_VECTORS = {  # de, para, legenda, zona atingida (país ISO numérico ou círculo em km)
    "gaza": [((31.5, 34.75), (31.45, 34.4), "Israel → Gaza", {"km": 22})],
    "irao": [((31.8, 34.9), (35.7, 51.4), "Israel → Irão", {"iso": "364"}), ((32.6, 51.7), (32.1, 34.8), "Irão → Israel", {"iso": "376"})],
    "libano": [((33.0, 35.4), (33.85, 35.5), "Israel → Líbano", {"iso": "422"}), ((33.3, 35.5), (32.8, 35.2), "Hezbollah → Israel", {"km": 45})],
    "iemen": [((15.35, 44.2), (14.5, 42.0), "Huthis → navios no Mar Vermelho", {"km": 160}), ((15.35, 44.2), (29.55, 34.95), "Huthis → Israel", {"iso": "376"})],
    "congo": [((-1.95, 30.06), (-1.68, 29.22), "Ruanda/M23 → Goma", {"km": 110})],
}
WEAPON_TAGS = {  # aba -> palavras nos títulos
    "nuclear": ["nuclear", "nucleares", "ogiva", "atómic", "atomic", "radioativ", "radiação", "zaporíjia", "zaporizhzhia", "aiea", "urânio", "enriquecimento"],
    "quimica": ["químic", "quimic", "biológic", "biologic", "sarin", "cloro", "gás tóxico", "opaq", "agente nervos"],
    "convencional": ["míssil", "mísseis", "drone", "artilharia", "bombarde", "bombardei", "ataque", "ataca", "tanque", "caça", "bomba",
                     "rocket", "foguete", "ofensiva", "explos", "combates", "disparo", "tiros", "militar"],
    "territorio": ["conquist", "captur", "avanç", "recu", "ocupa", "liberta", "linha da frente", "frente", "aldeia", "cidade tomada", "retira", "território", "anex"],
}
WAR_CACHE = os.path.join(HERE, "cache_guerras.json")
WAR_AI = {}   # id -> {"nivel", "razao", "situacao", "tendencia", "territorio", "at"}
WAR_TERR = {"areas": {}}  # área ocupada na Ucrânia, registada todos os dias pelo Radar (para a variação)


def tag_weapons(title):
    tl = title.lower()
    return [k for k, ws in WEAPON_TAGS.items() if any(w in tl for w in ws)]


def fetch_war_news(war):
    wid, nome, reg, iso, ctr, must, queries, wiki = war
    short = queries[0].split()[-1]
    qs = list(queries) + [f"{short} nuclear", f"{short} armas químicas"]
    items = fetch_news(qs, must=[must])
    for n in items:
        n["tags"] = tag_weapons(n["title"])
    return items


def wiki_status():
    """Em que secção da lista da Wikipédia está cada guerra (grande: 10 000+ mortes/ano; menor: 1 000–9 999)."""
    out = {}
    for sec, label in ((3, "guerra grande (10 000+ mortes/ano)"), (4, "guerra menor (1 000–9 999 mortes/ano)"),
                       (5, "conflito (100–999 mortes/ano)")):
        w = json.loads(get_cached("https://en.wikipedia.org/w/api.php?action=parse&page=List_of_ongoing_armed_conflicts"
                                  f"&prop=wikitext&section={sec}&format=json", ttl=12 * 3600, timeout=40))["parse"]["wikitext"]["*"]
        for war in WARS:
            key = war[7].split(" (")[0].lower()
            if war[0] not in out and key in w.lower():
                out[war[0]] = label
    return out


def _ring_area_km2(ring):
    import math
    R, s = 6371.0, 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += math.radians(x2 - x1) * (2 + math.sin(math.radians(y1)) + math.sin(math.radians(y2)))
    return abs(s) * R * R / 2


# ---------- território ao longo do tempo (mapas do DeepState desde 2022, para a barra de tempo) ----------
TERR_CACHE = os.path.join(HERE, "cache_territorio.json")
TERR_HIST = {"index": [], "snaps": {}, "crimea": None}
OTHER_TERR = ("transnistria", "придністров", "abkhazia", "tskhinvali", "kuril", "prussia", "petsamo", "salla", "karelia", "абхаз")


def terr_class(name):
    """Etiquetas do DeepState em inglês (agora) e em ucraniano (2022)."""
    nl = name.lower()
    if any(w in nl for w in OTHER_TERR):
        return None
    if "крим" in nl or "crimea" in nl:
        return "crimea"
    if "occupied" in nl or "cadr" in nl or "окупован" in nl or "ордло" in nl:
        return "occupied"
    if "dismissed" in nl or "liberated" in nl or "звільнено" in nl:
        return "liberated"
    if "unkno" in nl or "під питанням" in nl or "невідомо" in nl:
        return "unknown"
    return None


def terr_process(features, max_pts=120):
    """Só os polígonos ocupados da Ucrânia, simplificados; devolve (polígonos, área km², tem_crimeia)."""
    polys, area, crimea = [], 0.0, None

    def rings(g):  # os mapas de 2022 guardam muitas zonas dentro de GeometryCollection / MultiPolygon
        t = g.get("type")
        if t == "Polygon":
            yield g["coordinates"][0]
        elif t == "MultiPolygon":
            for p in g["coordinates"]:
                yield p[0]
        elif t == "GeometryCollection":
            for sub in g.get("geometries") or []:
                yield from rings(sub)
    for f, raw in ((f, r) for f in features for r in rings(f.get("geometry") or {})):
        st = terr_class(f.get("properties", {}).get("name") or "")
        if st not in ("occupied", "crimea") or len(raw) < 3:
            continue
        ring = [(x, y) for x, y, *_ in raw]
        cx, cy = sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
        if not (22 <= cx <= 40.5 and 44 <= cy <= 52.5):
            continue
        a = _ring_area_km2(ring)
        step = max(1, len(ring) // max_pts)
        pts = [[round(y, 3), round(x, 3)] for x, y in ring[::step]]
        if st == "crimea":
            crimea = {"pts": pts, "km2": a}
        polys.append(pts); area += a
    return polys, area, crimea


def load_terr_cache():
    try:
        with open(TERR_CACHE, encoding="utf-8") as f:
            TERR_HIST.update(json.load(f))
    except (OSError, ValueError):
        pass


_terr_running = threading.Event()


def start_territory_history():
    """Descarrega (uma vez) os mapas escolhidos: 1 por mês desde 2022, 1 por semana nos últimos 6 meses e
    1 por dia nos últimos 30 dias. Os mapas passados não mudam: ficam em cache_territorio.json."""
    if _terr_running.is_set():
        return {"running": True}
    def go():
        _terr_running.set()
        try:
            L = json.loads(get_cached("https://deepstatemap.live/api/history/public", ttl=6 * 3600, timeout=90))
            items = sorted(({"id": str(x["id"]), "t": int(x["id"]), "desc": re.sub(r"<[^>]+>", "", x.get("descriptionEn") or "").strip()}
                            for x in L if x.get("status") in (True, "True", "true")), key=lambda x: x["t"])
            now = time.time()
            pick, seen = [], set()
            for it in items:
                d = datetime.fromtimestamp(it["t"], timezone.utc)
                age = now - it["t"]
                key = (d.strftime("%Y-%m-%d") if age <= 30 * 86400 else d.strftime("%Y-W%W") if age <= 183 * 86400 else d.strftime("%Y-%m"))
                if key not in seen:
                    seen.add(key); pick.append(it)
            if items and items[-1]["id"] not in {p["id"] for p in pick}:
                pick.append(items[-1])
            pick_ids = {p["id"] for p in pick}
            newest_first = sorted(pick, key=lambda p: -p["t"])  # o mais recente primeiro (dá a Crimeia aos mapas antigos)
            for it in newest_first:
                if it["id"] in TERR_HIST["snaps"]:
                    continue
                try:
                    d = json.loads(get(f"https://deepstatemap.live/api/history/{it['id']}/geojson", timeout=60))
                    polys, area, crimea = terr_process(d.get("features") or [])
                    if crimea and not TERR_HIST["crimea"]:
                        TERR_HIST["crimea"] = crimea
                    if not crimea and TERR_HIST["crimea"]:  # os primeiros mapas não desenham a Crimeia
                        polys.append(TERR_HIST["crimea"]["pts"]); area += TERR_HIST["crimea"]["km2"]
                    TERR_HIST["snaps"][it["id"]] = {"polys": polys, "km2": round(area)}
                except Exception as e:
                    print(f"[território] {it['id']}: {e}")
                time.sleep(1.5)  # com calma (site de voluntários)
            idx = []
            for it in pick:
                s = TERR_HIST["snaps"].get(it["id"])
                if s:
                    idx.append({"id": it["id"], "date": datetime.fromtimestamp(it["t"], timezone.utc).strftime("%Y-%m-%d"),
                                "km2": s["km2"], "desc": it["desc"][:220]})
            for k in [k for k in TERR_HIST["snaps"] if k not in pick_ids]:
                TERR_HIST["snaps"].pop(k)  # já não é usado (ex.: um dia que passou a semana)
            TERR_HIST["index"] = idx
            with open(TERR_CACHE, "w", encoding="utf-8") as f:
                json.dump(TERR_HIST, f, ensure_ascii=False)
            print(f"{time.strftime('%H:%M:%S')}  território: {len(idx)} mapas na barra de tempo")
        except Exception as e:
            print(f"[território] {e}")
        finally:
            _terr_running.clear()
    threading.Thread(target=go, daemon=True).start()
    return {"started": True}


def fetch_ukraine_territory():
    d = json.loads(get_cached("https://deepstatemap.live/api/history/last", ttl=3 * 3600, timeout=40))
    polys, arrows, area = [], [], 0.0
    for f in d["map"]["features"]:
        name, g = (f["properties"].get("name") or ""), f["geometry"]
        nl = name.lower()
        if any(w in nl for w in ("transnistria", "abkhazia", "tskhinvali", "kuril", "prussia", "petsamo", "salla", "karelia")):
            continue  # o DeepState também marca outros territórios; aqui conta só a guerra na Ucrânia
        st = ("occupied" if "occupied" in nl or "cadr" in nl else "liberated" if "dismissed" in nl or "liberated" in nl
              else "unknown" if "unkno" in nl else None)
        if g["type"] == "Polygon" and st:
            ring = [(x, y) for x, y, *_ in g["coordinates"][0]]
            cx, cy = sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
            if not (22 <= cx <= 40.5 and 44 <= cy <= 52.5):  # só a Ucrânia (o mapa também marca Abcásia, Transnístria…)
                continue
            if st == "occupied":
                area += _ring_area_km2(ring)
            step = max(1, len(ring) // 220)
            polys.append({"st": st, "pts": [[round(y, 3), round(x, 3)] for x, y in ring[::step]]})
        elif g["type"] == "Point" and "Direction of attack" in name:
            x, y = g["coordinates"][:2]
            arrows.append([round(y, 3), round(x, 3)])
    # o DeepState só dá o ponto de cada direção de ataque: o sentido vai da zona ocupada mais perto para fora
    import math
    occ = [(la, lo) for p in polys if p["st"] == "occupied" for la, lo in p["pts"]]
    for a in arrows:
        if not occ:
            a.append(None); continue
        n = min(occ, key=lambda q: (q[0] - a[0]) ** 2 + ((q[1] - a[1]) * math.cos(math.radians(a[0]))) ** 2)
        dy, dx = a[0] - n[0], (a[1] - n[1]) * math.cos(math.radians(a[0]))
        if abs(dx) + abs(dy) < 1e-3:  # em cima da fronteira: usa o centro da zona ocupada
            cy, cx = sum(q[0] for q in occ) / len(occ), sum(q[1] for q in occ) / len(occ)
            dy, dx = a[0] - cy, (a[1] - cx) * math.cos(math.radians(a[0]))
        a.append(round(math.degrees(math.atan2(dx, dy)) % 360))  # rumo (0 = norte, 90 = este)
    day = datetime.now().strftime("%Y-%m-%d")
    WAR_TERR["areas"][day] = round(area)
    hist = WAR_TERR["areas"]
    def change(days):
        ref = (datetime.now() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")
        old = [v for k, v in sorted(hist.items()) if k <= ref]
        return round(area - old[-1]) if old else None
    return {"polys": polys, "arrows": arrows[:80], "occupiedKm2": round(area), "updated": d.get("datetime"),
            "change7": change(7), "change30": change(30), "since": min(hist) if hist else day, "source": "DeepStateMap"}


def fetch_wars():
    try:
        status = wiki_status()
    except Exception:
        status = {}
    terr = None
    try:
        terr = fetch_ukraine_territory()
    except Exception as e:
        print(f"[guerras] território: {e}")
    try:
        with open(WAR_CACHE, "w", encoding="utf-8") as f:
            json.dump({"ai": WAR_AI, "terr": WAR_TERR, "don": DON_AI}, f, ensure_ascii=False)
    except OSError:
        pass
    return {"wiki": status, "ukraine": terr, "at": time.time()}


def load_war_cache():
    try:
        with open(WAR_CACHE, encoding="utf-8") as f:
            c = json.load(f)
        WAR_AI.update(c.get("ai") or {}); WAR_TERR.update(c.get("terr") or {}); DON_AI.update(c.get("don") or {})
    except (OSError, ValueError):
        pass


WAR_SYSTEM = (
    "És analista de risco para a proteção civil portuguesa. Recebes títulos recentes de notícias sobre uma guerra. "
    "Classifica o nível de alerta PARA PORTUGAL (não para a região em guerra):\n"
    "3 (vermelho) SÓ se for extremamente grave para Portugal: ataque ou ameaça militar direta e concreta a Portugal; "
    "ataque a território da NATO/UE com o Artigo 5 invocado; uso real (não ameaça) de arma nuclear, química ou biológica; "
    "acidente nuclear com risco real para a Europa ocidental.\n"
    "2 (laranja): escalada séria com impacto na Europa (ataques a países NATO/UE sem Artigo 5, ameaças nucleares concretas "
    "com mudança de postura, bloqueio de rotas de energia/comércio com efeito direto na Europa).\n"
    "1 (amarelo): guerra ativa sem impacto direto em Portugal. 0: sem atividade relevante.\n"
    "Na dúvida, escolhe o nível mais baixo. Responde APENAS com JSON: {\"nivel\": 0-3, \"razao\": frase curta, "
    "\"situacao\": 2 frases em português de Portugal sobre como está a decorrer a guerra, "
    "\"tendencia\": \"escalada\"|\"estável\"|\"desescalada\", \"territorio\": frase curta sobre ganhos/perdas de território ou null}")


def war_ai_one(war, news):
    key = groq_key()
    if not key or not news:
        return None
    heads = "\n".join(f"- {n['date'][:10]} {n['title'][:140]}" for n in news[:18])
    body = {"model": GROQ_MODEL, "temperature": 0, "max_completion_tokens": 600, "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": WAR_SYSTEM},
                         {"role": "user", "content": f"Guerra: {war[1]} ({war[2]}). Hoje: {datetime.now():%Y-%m-%d}.\nTítulos:\n{heads}"}]}
    req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "radar-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        txt = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"] or "{}"
    m = re.search(r"\{[\s\S]*\}", txt)
    res = json.loads(m.group(0) if m else "{}")
    lvl = max(0, min(3, int(res.get("nivel") or 0)))
    # salvaguarda: vermelho só com sinais concretos nos títulos (nuclear/químico usado, NATO/Artigo 5, Portugal)
    sig = " ".join(n["title"].lower() for n in news[:18])
    if lvl == 3 and not any(w in sig for w in ("nuclear", "químic", "biológic", "artigo 5", "nato", "otan", "portugal", "portugu")):
        lvl = 2
    return {"nivel": lvl, "razao": str(res.get("razao") or "")[:200], "situacao": str(res.get("situacao") or "")[:420],
            "tendencia": res.get("tendencia"), "territorio": res.get("territorio"), "at": time.time()}


def war_overview_news():
    with lock:
        allnews = [dict(n, war=w[1]) for w in WARS for n in ((state["events"].get(w[0]) or {}).get("news") or [])]
    seen, out = set(), []
    for n in sorted(allnews, key=lambda n: n["date"], reverse=True):
        if n["title"] not in seen:
            seen.add(n["title"]); out.append(n)
    return out[:60]


def war_detail(wid):
    """Dados de uma guerra (chamado com o lock)."""
    war = next((w for w in WARS if w[0] == wid), None)
    if not war:
        return None
    wid, nome, reg, iso, ctr, must, queries, wiki = war
    news = (state["events"].get(wid) or {}).get("news") or []
    now = time.time()
    recent = [n for n in news if now - datetime.fromisoformat(n["date"]).timestamp() <= 72 * 3600]
    counts = {k: sum(k in (n.get("tags") or []) for n in recent) for k in WEAPON_TAGS}
    wars = state.get("wars") or {}
    ai = WAR_AI.get(wid)
    return {"id": wid, "nome": nome, "regiao": reg, "countries": iso, "lat": ctr[0], "lon": ctr[1], "wikiTitle": wiki,
            "wiki": (wars.get("wiki") or {}).get(wid), "kmLisboa": round(km(ctr, (38.72, -9.14))),
            "ai": ai, "counts": counts, "n72": len(recent), "lastNews": news[0]["date"] if news else None,
            "vectors": [{"from": a, "to": b, "label": lab, "target": tg} for a, b, lab, tg in WAR_VECTORS.get(wid, [])],
            "territory": wars.get("ukraine") if wid == "ucrania" else None}


# ================= terrorismo e ciberataques =================
# Notícias -> a IA (Groq) extrai incidentes distintos (onde, quando, vítimas, autor, alvo). Ciber: + CISA KEV (oficial).
TERROR_Q = ["ataque terrorista", "atentado terrorista", "atentado bomba mortos", "terrorismo ataque"]
TERROR_MUST = [["terror", "atentado", "jihad", "estado islâmico", "suicida", "extremista"]]
CYBER_Q = ["ciberataque", "ataque informático", "ransomware ataque", "ciberataque Portugal", "fuga de dados hackers"]
CYBER_MUST = [["ciberataque", "ataque informático", "ransomware", "hacker", "pirata informático", "ddos", "fuga de dados",
               "dados roubados", "cibersegurança", "ciberseguranca", "phishing", "malware"]]
INC_CACHE = os.path.join(HERE, "cache_incidentes.json")
INCIDENTS = {}  # id -> incidente (fica guardado; os antigos passam a "já aconteceu", até 1 ano e meio)


def load_incidents():
    try:
        with open(INC_CACHE, encoding="utf-8") as f:
            INCIDENTS.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_incidents():
    cut = time.time() - PAST_LIMIT_S
    for k in [k for k, v in INCIDENTS.items() if datetime.fromisoformat(v["date"]).timestamp() < cut]:
        INCIDENTS.pop(k)
    try:
        with open(INC_CACHE, "w", encoding="utf-8") as f:
            json.dump(INCIDENTS, f, ensure_ascii=False)
    except OSError:
        pass


INC_SYSTEM = {
    "terror": ("Recebes títulos de notícias numerados. Extrai os ATAQUES TERRORISTAS reais e distintos (ignora ameaças, julgamentos, "
               "aniversários e opinião). Junta títulos do mesmo ataque. Para cada um: {\"titulo\": frase curta em português de Portugal, "
               "\"cidade\", \"pais\" (em português), \"lat\", \"lon\" (coordenadas aproximadas da cidade), \"data\": \"AAAA-MM-DD\", "
               "\"mortos\": número ou null, \"feridos\": número ou null, \"autor\": grupo ou suspeito ou null, \"alvo\": frase curta, "
               "\"tipo\": \"bomba\"|\"tiroteio\"|\"atropelamento\"|\"esfaqueamento\"|\"outro\", \"portugal\": true se foi em Portugal, "
               "\"europa\": true se foi num país da UE/NATO europeu, \"fontes\": [números dos títulos]}. "
               "Responde APENAS com JSON: {\"incidentes\": [...]} (lista vazia se não houver)."),
    "ciber": ("Recebes títulos de notícias numerados. Extrai os CIBERATAQUES reais e distintos (ignora conselhos, opinião e produtos). "
              "Junta títulos do mesmo ataque. Para cada um: {\"titulo\": frase curta em português de Portugal, \"alvo\": organização atacada, "
              "\"pais\" (em português), \"lat\", \"lon\" (coordenadas aproximadas do país/cidade do alvo), \"data\": \"AAAA-MM-DD\", "
              "\"tipo\": \"ransomware\"|\"ddos\"|\"fuga de dados\"|\"phishing\"|\"espionagem\"|\"outro\", \"autor\": grupo ou país suspeito ou null, "
              "\"impacto\": frase curta, \"infra_critica\": true se atingiu energia, água, saúde, transportes, telecomunicações ou Estado, "
              "\"portugal\": true se o alvo é português, \"europa\": true se é europeu, \"fontes\": [números dos títulos]}. "
              "Responde APENAS com JSON: {\"incidentes\": [...]} (lista vazia se não houver)."),
}


def incidents_ai(kind, news):
    key = groq_key()
    if not key or not news:
        return 0
    heads = "\n".join(f"{i}. {n['date'][:10]} {n['title'][:150]}" for i, n in enumerate(news[:30]))
    body = {"model": GROQ_MODEL, "temperature": 0, "max_completion_tokens": 1800, "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": INC_SYSTEM[kind]},
                         {"role": "user", "content": f"Hoje: {datetime.now():%Y-%m-%d}.\nTítulos:\n{heads}"}]}
    req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "radar-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        txt = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"] or "{}"
    m = re.search(r"\{[\s\S]*\}", txt)
    found = 0
    for it in (json.loads(m.group(0) if m else "{}").get("incidentes") or [])[:15]:
        try:
            lat, lon = float(it["lat"]), float(it["lon"])
            day = datetime.fromisoformat(str(it.get("data"))[:10]).strftime("%Y-%m-%d")
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        for f in ("cidade", "autor", "alvo"):  # "desconhecido" = sem informação
            if isinstance(it.get(f), str) and re.search(r"desconhec|n[ãa]o (identific|indicad)|unknown", it[f], re.I):
                it[f] = None
        place = it.get("cidade") or it.get("alvo") or ""
        iid = f"{kind}-{slug((it.get('pais') or '') + '-' + place)[:40]}-{day}"
        srcs = [news[i] for i in it.get("fontes") or [] if isinstance(i, int) and 0 <= i < len(news)]
        old = INCIDENTS.get(iid, {})
        links = {n["link"]: n for n in (old.get("news") or []) + srcs}
        rec = dict(old)
        rec.update({k: v for k, v in it.items() if k not in ("fontes", "lat", "lon", "data")})
        rec.update(id=iid, kind=kind, lat=lat, lon=lon, date=day, seen=old.get("seen") or time.time(),
                   news=sorted(links.values(), key=lambda n: n["date"], reverse=True)[:15])
        INCIDENTS[iid] = rec
        found += 1
    save_incidents()
    return found


def fetch_cisa_kev():
    """Vulnerabilidades que estão a ser exploradas por atacantes (lista oficial da CISA, EUA)."""
    d = json.loads(get_cached("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json", ttl=6 * 3600, timeout=60))
    vs = d.get("vulnerabilities") or []
    now = datetime.now()
    recent = sorted((v for v in vs if (now - datetime.fromisoformat(v["dateAdded"])).days <= 30), key=lambda v: v["dateAdded"], reverse=True)
    return {"n7": sum((now - datetime.fromisoformat(v["dateAdded"])).days <= 7 for v in recent), "n30": len(recent), "total": len(vs),
            "ransomware30": sum(v.get("knownRansomwareCampaignUse") == "Known" for v in recent),
            "recent": [{"cve": v["cveID"], "vendor": v["vendorProject"], "product": v["product"], "name": v["vulnerabilityName"],
                        "date": v["dateAdded"], "ransomware": v.get("knownRansomwareCampaignUse") == "Known"} for v in recent[:12]]}


# ---------- pandemias e surtos (OMS) · acidentes nucleares (AIEA, escala INES) ----------
EU_EN = {"Portugal", "Spain", "France", "Italy", "Germany", "Belgium", "Netherlands", "Luxembourg", "Ireland", "Austria", "Denmark",
         "Sweden", "Finland", "Greece", "Poland", "Czechia", "Czech Republic", "Slovakia", "Hungary", "Romania", "Bulgaria", "Croatia",
         "Slovenia", "Estonia", "Latvia", "Lithuania", "Malta", "Cyprus", "United Kingdom", "Norway", "Switzerland", "Ukraine"}
DON_AI = {}  # DonId -> resumo em português (IA)


def _plain(htmltxt, n=600):
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(htmltxt or ""))).strip()
    return t[:n] + ("…" if len(t) > n else "")


def fetch_outbreaks():
    d = json.loads(get_cached("https://www.who.int/api/news/diseaseoutbreaknews?$orderby=PublicationDate%20desc&$top=40", ttl=3 * 3600, timeout=40))
    out = []
    for it in d.get("value") or []:
        title = it.get("OverrideTitle") if it.get("UseOverrideTitle") else it.get("Title")
        title = (title or "").strip()
        m = re.match(r"^(.+?)\s*[-–—]\s+(.+)$", title) or re.match(r"^(.+?),\s+(.+)$", title)
        disease, where = (m.group(1), m.group(2)) if m else (title, "")
        countries = [c.strip() for c in re.split(r",| and ", re.sub(r"^the ", "", where)) if c.strip()]
        text = " ".join(_plain(it.get(f), 4000) for f in ("Summary", "Overview", "Assessment"))
        tl = text.lower()
        did = it.get("DonId") or it.get("UrlName") or slug(title)
        out.append({"id": "surto-" + slug(did), "donId": did, "disease": disease, "where": where, "countries": countries,
                    "date": (it.get("PublicationDate") or "")[:10], "summary": _plain(it.get("Summary") or it.get("Overview"), 700),
                    "assessment": _plain(it.get("Assessment"), 700),
                    "pheic": "public health emergency of international concern" in tl and not re.search(r"not (constitute|a) (a )?public health emergency", tl),
                    # pandemia só se a OMS disser que a declarou (não basta a palavra "pandemic" no texto)
                    "pandemic": bool(re.search(r"\bpandemic (has been|was) declared|declared (a|the) pandemic|characteri[sz]ed as a pandemic", tl)),
                    "europe": any(c in EU_EN for c in countries), "portugal": "Portugal" in countries,
                    "link": "https://www.who.int/emergencies/disease-outbreak-news/item/" + (it.get("UrlName") or ""), "ai": DON_AI.get(did)})
    return out


NUC_EN_EU = EU_EN | {"Russian Federation", "Russia", "Belarus", "Türkiye", "Turkey"}


def fetch_nuclear_events():
    x = get_cached("https://www-news.iaea.org/EventList.aspx", ttl=3 * 3600, timeout=40)
    out = []
    for m in re.finditer(r'font-size-3">\s*(.+?),\s*(\d{1,2} \w{3} \d{4}), INES:\s*([^<]+?)\s*</span>\s*<h4[^>]*>\s*<a href=\'([^\']+)\'>\s*(.+?)\s*</a>'
                         r'[\s\S]*?<p>\s*([\s\S]*?)<br', x):
        country, day, ines, link, title, summ = (html.unescape(g.strip()) for g in m.groups())
        try:
            date = datetime.strptime(day, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        lvl = int(ines) if ines.strip().isdigit() else 0
        out.append({"id": "nuclear-" + slug(country + "-" + date + "-" + title)[:60], "country": country, "date": date, "ines": lvl,
                    "inesTxt": ines, "title": title, "summary": re.sub(r"\s+", " ", summ).strip()[:500],
                    "europe": country in NUC_EN_EU, "link": "https://www-news.iaea.org/" + link})
    return out


NUCLEAR_PLANTS_NEAR = [  # centrais nucleares perto de Portugal (Espanha), distância à fronteira é o que mais interessa
    ("Almaraz", 39.807, -5.697), ("Trillo", 40.701, -2.622), ("Cofrentes", 39.214, -1.050), ("Ascó", 41.200, 0.570),
    ("Vandellós", 40.951, 0.866)]


def outbreak_alert(o):
    if (datetime.now() - datetime.fromisoformat(o["date"])).days > 30:
        return 0
    return 3 if o["pandemic"] or (o["portugal"] and o["pheic"]) else 2 if o["pheic"] or o["europe"] else 1


def nuclear_alert(e):
    if (datetime.now() - datetime.fromisoformat(e["date"])).days > 60:
        return 0
    return 3 if e["ines"] >= 6 or (e["ines"] >= 5 and e["europe"]) else 2 if e["ines"] >= 3 or (e["ines"] >= 2 and e["europe"]) else 1


def don_ai_one(o):
    """Resumo em português e o risco para Portugal (só descreve; o nível de alerta segue as regras acima)."""
    key = groq_key()
    if not key:
        return None
    body = {"model": GROQ_MODEL, "temperature": 0, "max_completion_tokens": 500, "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": "Traduz e resume para português de Portugal uma notícia de surto da OMS. "
                          "Responde APENAS com JSON: {\"doenca\": nome da doença em português, \"resumo\": 2 frases, "
                          "\"risco_portugal\": 1 frase sobre o risco para Portugal (baixo/moderado/alto e porquê), "
                          "\"casos\": número de casos se for dito ou null, \"mortes\": número de mortes se for dito ou null}"},
                         {"role": "user", "content": f"{o['disease']} — {o['where']} ({o['date']})\nResumo: {o['summary']}\nAvaliação da OMS: {o['assessment']}"}]}
    req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "radar-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"] or "{}"
    m = re.search(r"\{[\s\S]*\}", txt)
    return json.loads(m.group(0) if m else "{}")


def incident_alert(inc):
    if time.time() - datetime.fromisoformat(inc["date"]).timestamp() > 72 * 3600:
        return 0
    if inc["kind"] == "terror":
        return 3 if inc.get("portugal") else 2 if inc.get("europa") else 1
    return (3 if inc.get("portugal") and inc.get("infra_critica") else
            2 if inc.get("portugal") or (inc.get("europa") and inc.get("infra_critica")) else 1)


_war_ai_running = threading.Event()


def start_war_ai():
    """Classifica as guerras com a IA, uma de cada vez e com pausas (limite da Groq). Corre à parte."""
    if _war_ai_running.is_set() or not groq_key():
        return {"skipped": True}
    def go():
        _war_ai_running.set()
        try:
            for _ in range(12):  # espera que cheguem as notícias das guerras (na 1.ª volta ainda não há)
                time.sleep(20)
                with lock:
                    if any((state["events"].get(w[0]) or {}).get("news") for w in WARS):
                        break
            for war in WARS:
                with lock:
                    news = list((state["events"].get(war[0]) or {}).get("news") or [])
                if not news:
                    continue
                try:
                    r = war_ai_one(war, news)
                    if r:
                        WAR_AI[war[0]] = r
                except Exception as e:
                    print(f"[guerras IA] {war[0]}: {e}")
                time.sleep(25)
            # surtos da OMS dos últimos 30 dias: resumo em português (uma vez por surto)
            for o in [o for o in (state.get("outbreaks") or []) if o["donId"] not in DON_AI
                      and (datetime.now() - datetime.fromisoformat(o["date"])).days <= 30][:5]:
                try:
                    r = don_ai_one(o)
                    if r:
                        DON_AI[o["donId"]] = r; o["ai"] = r
                except Exception as e:
                    print(f"[surtos IA] {o['donId']}: {e}")
                time.sleep(20)
            # terrorismo e ciberataques: a IA tira dos títulos os incidentes (onde, quando, vítimas, autor…)
            for kind, agg in (("terror", "terrorismo"), ("ciber", "ciberataques")):
                with lock:
                    news = list((state["events"].get(agg) or {}).get("news") or [])
                try:
                    n = incidents_ai(kind, news)
                    print(f"{time.strftime('%H:%M:%S')}  {agg}: {n} incidentes reconhecidos")
                except Exception as e:
                    print(f"[{agg} IA] {e}")
                time.sleep(25)
        finally:
            _war_ai_running.clear()
    threading.Thread(target=go, daemon=True).start()
    return {"started": True}


ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}


def degree_num(g):
    return max((ROMAN.get(x.strip(), 0) for x in re.split(r"[/-]", g or "")), default=0)


def quake_alert(q):
    deg = degree_num(q.get("degree"))
    m = q.get("mag") or 0
    return 3 if m >= 5.5 or deg >= 6 else 2 if m >= 4.5 or q.get("sensed") else 1 if m >= 3.5 else 0


def fire_alert(f):
    return 3 if f["man"] >= 150 or f["aerial"] >= 5 else 2 if f["man"] >= 60 or f["important"] else 1


def big_fire(f):
    return f["important"] or f["man"] >= 40 or f["aerial"] >= 2


def place_of(q):  # "a NE de Bragança" -> "Bragança" (para pesquisar notícias)
    return re.sub(r"^[A-Z]{1,3} de ", "", q["region"])


def sync_natural():
    """Cria/atualiza/retira os eventos que vêm das fontes em direto (chamado a cada volta)."""
    now = time.time()
    with lock:
        quakes, fires, warns = state.get("quakes"), state.get("fires"), list(state.get("warnings") or [])
        rivers, volcs = state.get("rivers"), state.get("volcanoes")
        tsus, droughts = state.get("tsunamis"), state.get("droughts")
    must_q = [["sismo", "terramoto", "abalo", "sísmic"]]
    must_f = [["incêndio", "incendio", "fogo", "chamas", "fogos"]]
    want = {}
    if quakes is not None:
        want["sismos"] = {"kind": "sismos", "nome": "Atividade sísmica", "tipo": "Sismos", "icone": "🌍", "sempre": True,
                          "pesquisas": ["sismo IPMA", "sismo Açores", "sismo Portugal"], "must": must_q, "origem": "IPMA"}
        sig = [q for q in quakes if (q["mag"] or 0) >= 3.5 or q["sensed"]][:8]
        for q in sig:
            want[q["id"]] = {"kind": "sismo", "nome": f"M{q['mag'] or '?'} · {q['region']}", "tipo": "Sismo", "icone": "🌍",
                             "ref": q["id"], "origem": "IPMA", "must": must_q,
                             "pesquisas": [f"sismo {place_of(q)}", f"sismo {q['zona'].split()[0]}"]}
    if fires is not None:
        for f in fires:
            FIRE_SEEN[f["id"]] = dict(FIRE_SEEN.get(f["id"], {}), **f, lastSeen=now, ended=None,
                                      big=FIRE_SEEN.get(f["id"], {}).get("big") or big_fire(f))
        live = {f["id"] for f in fires}
        for fid, f in list(FIRE_SEEN.items()):
            if fid not in live and not f.get("ended"):
                f["ended"] = now
            if f.get("ended") and now - f["ended"] > 48 * 3600:
                FIRE_SEEN.pop(fid)
        want["incendios"] = {"kind": "incendios", "nome": "Incêndios em Portugal", "tipo": "Incêndios", "icone": "🔥",
                             "sempre": True, "origem": FIRES_SOURCE["name"] or "Fogos.pt", "must": must_f,
                             "pesquisas": ["incêndio bombeiros", "incêndio Proteção Civil", "fogo florestal"]}
        bigs = sorted((f for f in FIRE_SEEN.values() if f.get("big")), key=lambda f: (bool(f.get("ended")), -f["man"]))[:10]
        for f in bigs:
            want[f["id"]] = {"kind": "incendio", "nome": f["concelho"] or f["district"], "tipo": "Incêndio", "icone": "🔥",
                             "ref": f["id"], "origem": "Proteção Civil (ANEPC)" if f.get("src") == "ANEPC" else "Fogos.pt", "must": must_f + [[(f["concelho"] or "").lower()]],
                             "pesquisas": [f"incêndio {f['concelho']}", f"fogo {f['concelho']}"]}
    must_c = [["cheia", "cheias", "inunda", "caudal", "transbord", "rio "]]
    if rivers is not None:
        want["cheias"] = {"kind": "cheias", "nome": "Rios e cheias", "tipo": "Cheias", "icone": "🌊", "sempre": True,
                          "origem": "Copernicus GloFAS", "must": must_c, "pesquisas": ["cheia rio", "inundações Portugal", "caudal rio APA"]}
        for r in rivers:
            if r["level"] >= 2:  # cheia prevista (acima do percentil 99 dos últimos 10 anos)
                want[r["id"]] = {"kind": "cheia", "nome": f"{r['rio']} em {r['local']}", "tipo": "Cheia", "icone": "🌊",
                                 "ref": r["id"], "origem": "Copernicus GloFAS", "must": [["cheia", "cheias", "inunda", "caudal", "transbord", "subida"], [r["rio"].lower()]],
                                 "pesquisas": [f"rio {r['rio']} cheia", f"{r['rio']} caudal {r['local']}"]}
    must_v = [["vulcão", "vulcao", "erupção", "erupcao", "vulcânic", "lava", "cinzas"]]
    if volcs is not None:
        want["vulcoes"] = {"kind": "vulcoes", "nome": "Vulcões", "tipo": "Vulcões", "icone": "🌋", "sempre": True,
                           "origem": "Smithsonian GVP + IPMA", "must": must_v, "pesquisas": ["vulcão erupção", "vulcão Açores", "erupção cinzas voos"]}
        for v in volcs:
            if v["near"] or v["new"]:
                want[v["id"]] = {"kind": "vulcao", "nome": v["name"], "tipo": "Vulcão", "icone": "🌋", "ref": v["id"],
                                 "origem": "Smithsonian GVP", "must": must_v + [[v["name"].lower().split()[0]]],
                                 "pesquisas": [f"vulcão {v['name']}", f"{v['name']} erupção"]}
        for a in azores_volcano_watch():
            if a["state"] == "agitação sísmica":
                vid = "vulcao-" + slug(a["name"])
                want[vid] = {"kind": "vulcao", "nome": a["name"], "tipo": "Vulcão · Açores", "icone": "🌋", "ref": vid,
                             "origem": "IPMA (sismos)", "must": must_v + [[a["ilha"].lower(), a["name"].lower().split()[0]]],
                             "pesquisas": [f"vulcão {a['name']} {a['ilha']}", f"sismos {a['ilha']}"]}
    if tsus is not None:
        must_t = [["tsunami", "maremoto"]]
        want["tsunamis"] = {"kind": "tsunamis", "nome": "Tsunamis", "tipo": "Tsunamis", "icone": "🌊", "sempre": True,
                            "origem": "NOAA (PTWC/NTWC) + IPMA", "must": must_t, "pesquisas": ["tsunami", "tsunami aviso", "maremoto"]}
        for t in tsus:
            if now - datetime.fromisoformat(t["t"]).timestamp() <= 7 * 86400 and (t["level"] >= 2 or t["centro"].startswith("IPMA")):
                want[t["id"]] = {"kind": "tsunami", "nome": t["region"], "tipo": "Tsunami", "icone": "🌊", "ref": t["id"],
                                 "origem": t["centro"], "must": must_t, "pesquisas": [f"tsunami {t['region'].split(',')[-1].strip()}"]}
    if droughts is not None:
        must_s = [["seca", "chuva", "barragens", "albufeiras", "reservas de água"]]
        want["secas"] = {"kind": "secas", "nome": "Seca em Portugal", "tipo": "Secas", "icone": "🏜", "sempre": True,
                         "origem": "NASA POWER + Open-Meteo", "must": must_s, "pesquisas": ["seca Portugal", "seca IPMA", "barragens seca"]}
        for d in sorted(droughts, key=lambda d: d["rank3"])[:4]:
            if d["level"] >= 3:  # só seca extrema vira evento próprio (o resto vê-se na vista geral da seca)
                want[d["id"]] = {"kind": "seca", "nome": d["name"], "tipo": "Seca", "icone": "🏜", "ref": d["id"],
                                 "origem": "NASA POWER + Open-Meteo", "must": [["seca"]], "pesquisas": [f"seca {d['name']}"]}
    for kind, wtype, nome, icone in (("calor", "Tempo Quente", "Tempo quente", "🌡"), ("frio", "Tempo Frio", "Tempo frio", "🥶")):
        ws = [w for w in warns if w["type"] == wtype]
        if ws:
            WARN_SEEN[kind] = {"warnings": ws, "lastSeen": now}
        if kind in WARN_SEEN and now - WARN_SEEN[kind]["lastSeen"] < 48 * 3600:
            want[kind] = {"kind": kind, "nome": nome, "tipo": "Aviso IPMA", "icone": icone, "origem": "IPMA",
                          "pesquisas": [f'"{wtype.lower()}" IPMA aviso', f"{'calor' if kind == 'calor' else 'frio'} aviso IPMA"],
                          "must": [["calor", "quente", "temperatura"] if kind == "calor" else ["frio", "temperatura", "geada", "neve"]]}
    # guerras (prevenção de catástrofes humanas)
    want["guerras"] = {"kind": "guerras", "nome": "Guerras no mundo", "tipo": "Catástrofes humanas", "icone": "⚔", "sempre": True,
                       "origem": "Notícias + Wikipédia + DeepStateMap + IA (Groq)", "pesquisas": [], "must": None}
    want["terrorismo"] = {"kind": "terrorismo", "nome": "Terrorismo", "tipo": "Catástrofes humanas", "icone": "💣", "sempre": True,
                          "origem": "Notícias + IA (Groq)", "pesquisas": TERROR_Q, "must": TERROR_MUST}
    want["ciberataques"] = {"kind": "ciberataques", "nome": "Ciberataques", "tipo": "Catástrofes humanas", "icone": "🛡", "sempre": True,
                            "origem": "Notícias + IA (Groq) + CISA", "pesquisas": CYBER_Q, "must": CYBER_MUST}
    # pandemias e surtos (OMS) · acidentes nucleares (AIEA) — também na categoria das catástrofes humanas
    must_s = [["surto", "epidemia", "pandemia", "vírus", "virus", "oms", "dgs", "infeç", "infec", "contágio", "ébola", "gripe"]]
    must_n = [["nuclear", "radia", "radioativ", "aiea", "almaraz", "zaporíjia", "zaporizhzhia", "atómic", "urânio"]]
    want["surtos"] = {"kind": "surtos", "nome": "Pandemias e surtos", "tipo": "Catástrofes humanas", "icone": "🦠", "sempre": True,
                      "origem": "OMS + notícias + IA", "pesquisas": ["surto OMS", "epidemia casos", "pandemia", "DGS surto Portugal"], "must": must_s}
    want["nucleares"] = {"kind": "nucleares", "nome": "Acidentes nucleares", "tipo": "Catástrofes humanas", "icone": "☢", "sempre": True,
                         "origem": "AIEA (INES) + notícias", "pesquisas": ["acidente nuclear", "central nuclear radiação", "Zaporíjia central nuclear", "Almaraz central nuclear"],
                         "must": must_n}
    seen_ob = set()
    for o in (state.get("outbreaks") or []):  # já vêm do mais recente: fica só a última atualização de cada surto
        key_ob = (o["disease"].lower(), o["where"].lower())
        if key_ob in seen_ob:
            continue
        seen_ob.add(key_ob)
        age = (datetime.now() - datetime.fromisoformat(o["date"])).days
        if age * 86400 <= PAST_LIMIT_S:
            want[o["id"]] = {"kind": "surto", "nome": f"{(o.get('ai') or {}).get('doenca') or o['disease']} · {o['where']}"[:80], "tipo": "Surto (OMS)",
                             "icone": "🦠", "ref": o["id"], "origem": "OMS", "passada": age > 30, "must": [must_s[0] + [o["disease"].split()[0].lower()]],
                             "pesquisas": [f"{o['disease'].split(' caused')[0]} surto", f"{o['disease'].split()[0]} {o['countries'][0] if o['countries'] else ''}"]}
    for e in (state.get("nuclear") or []):
        age = (datetime.now() - datetime.fromisoformat(e["date"])).days
        if age * 86400 <= PAST_LIMIT_S:
            want[e["id"]] = {"kind": "acidnuc", "nome": f"{e['title']} · {e['country']}"[:80], "tipo": f"Evento nuclear · INES {e['inesTxt']}",
                             "icone": "☢", "ref": e["id"], "origem": "AIEA", "passada": age > 60, "must": must_n,
                             "pesquisas": [f"nuclear {e['country']}", f"radiação {e['country']}"]}
    for iid, inc in list(INCIDENTS.items()):
        recent = now - datetime.fromisoformat(inc["date"]).timestamp() <= 72 * 3600
        want[iid] = {"kind": inc["kind"], "nome": inc.get("titulo") or iid, "tipo": "Ataque terrorista" if inc["kind"] == "terror" else "Ciberataque",
                     "icone": "💣" if inc["kind"] == "terror" else "🛡", "ref": iid, "origem": "Notícias + IA",
                     "pesquisas": [], "must": None, "passada": not recent}
    for war in WARS:
        want[war[0]] = {"kind": "guerra", "nome": war[1], "tipo": "Guerra", "icone": "⚔", "ref": war[0], "origem": "Notícias + IA",
                        "pesquisas": war[6], "must": [war[5]], "war": True}
    # eventos que já aconteceram (histórico de dados reais, até 1 ano e meio)
    if volcs:
        remember_volcanoes(volcs)
    hist = dict(HIST.get("events") or {})
    for pid, v in (HIST.get("volcanoes") or {}).items():
        hist.setdefault(pid, v)
    for pid, h in hist.items():
        try:
            age = now - datetime.fromisoformat(h["t"][:19] if "T" in h["t"] else h["t"][:10]).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if age > PAST_LIMIT_S or (h["kind"] == "vulcao" and pid[5:] in want):  # vulcão ainda no relatório atual: não duplica
            continue
        tipo, icone, words = HIST_META.get(h["kind"], ("Evento", "•", []))
        want[pid] = {"kind": h["kind"], "nome": h["nome"], "tipo": tipo, "icone": icone, "ref": pid, "hist": True, "passada": True,
                     "origem": h.get("source"), "must": [words], "pesquisas": [f"{words[0]} {h['nome'].split(' · ')[-1].split(' de ')[0][:40]}"]}
    for eid, ev in want.items():
        ev.update(id=eid, dyn=True)
        if eid in events:
            events[eid].update(ev)
        else:
            add_event(ev)
    with lock:
        for eid in [k for k, m in events.items() if m.get("dyn") and k not in want]:
            events.pop(eid, None)
            state["events"].pop(eid, None)


def quake_by_id(qid):
    return next((q for q in state.get("quakes") or [] if q["id"] == qid), None)


def tnt(mag):
    tons = 10 ** (1.5 * mag + 4.8) / 4.184e9
    return f"{tons / 1000:,.1f} kt".replace(",", " ") if tons >= 1000 else f"{tons:,.0f} t".replace(",", " ")


def event_detail(eid):
    """Dados próprios de cada tipo de evento (chamado com o lock)."""
    m = events[eid]
    k, now = m.get("kind"), time.time()
    ts = ts_ = lambda iso: datetime.fromisoformat(iso).timestamp()
    if m.get("hist"):
        return (HIST.get("events") or {}).get(m["ref"]) or (HIST.get("volcanoes") or {}).get(m["ref"])
    if k == "surto":
        o = next((x for x in state.get("outbreaks") or [] if x["id"] == m["ref"]), None)
        return dict(o, alert=outbreak_alert(o), ai=DON_AI.get(o["donId"])) if o else None
    if k == "surtos":
        seen_ob, os_ = set(), []
        for o in state.get("outbreaks") or []:  # só a última atualização de cada surto
            if (o["disease"].lower(), o["where"].lower()) not in seen_ob:
                seen_ob.add((o["disease"].lower(), o["where"].lower()))
                os_.append(dict(o, alert=outbreak_alert(o), ai=DON_AI.get(o["donId"])))
        recent = [o for o in os_ if (datetime.now() - datetime.fromisoformat(o["date"])).days <= 30]
        return {"outbreaks": os_[:30], "n30": len(recent), "pheic": sum(o["pheic"] for o in recent),
                "europe30": sum(o["europe"] for o in recent), "portugal30": sum(o["portugal"] for o in recent)}
    if k == "acidnuc":
        e = next((x for x in state.get("nuclear") or [] if x["id"] == m["ref"]), None)
        return dict(e, alert=nuclear_alert(e), plants=NUCLEAR_PLANTS_NEAR) if e else None
    if k == "nucleares":
        es = [dict(e, alert=nuclear_alert(e)) for e in state.get("nuclear") or []]
        return {"events": es, "n60": sum((datetime.now() - datetime.fromisoformat(e["date"])).days <= 60 for e in es),
                "maxInes": max((e["ines"] for e in es), default=0), "europe": sum(e["europe"] for e in es),
                "plants": [{"name": n, "lat": la, "lon": lo, "kmPT": round(min(km((la, lo), p) for p in ((39.29, -7.43), (38.0, -7.0), (41.0, -6.9), (39.66, -7.0))))}
                           for n, la, lo in NUCLEAR_PLANTS_NEAR]}
    if k in ("terror", "ciber"):
        inc = INCIDENTS.get(m["ref"])
        return dict(inc, alert=incident_alert(inc), nearest=nearest_places(inc["lat"], inc["lon"], 3),
                    kmLisboa=round(km((inc["lat"], inc["lon"]), (38.72, -9.14)))) if inc else None
    if k in ("terrorismo", "ciberataques"):
        kind = "terror" if k == "terrorismo" else "ciber"
        incs = sorted((dict(v, alert=incident_alert(v), news=None) for v in INCIDENTS.values() if v["kind"] == kind
                       and now - datetime.fromisoformat(v["date"]).timestamp() <= 30 * 86400), key=lambda v: v["date"], reverse=True)
        week = [v for v in incs if now - datetime.fromisoformat(v["date"]).timestamp() <= 7 * 86400]
        out = {"incidents": incs[:40], "n7": len(week), "n30": len(incs), "aiOn": bool(groq_key()),
               "europe7": sum(bool(v.get("europa")) for v in week), "portugal7": sum(bool(v.get("portugal")) for v in week)}
        if kind == "terror":
            out["deaths7"] = sum(int(v.get("mortos") or 0) for v in week)
        else:
            out["kev"] = state.get("kev"); out["critical7"] = sum(bool(v.get("infra_critica")) for v in week)
        return out
    if k == "guerra":
        return war_detail(m["ref"])
    if k == "guerras":
        ws = [war_detail(w[0]) for w in WARS]
        terr = (state.get("wars") or {}).get("ukraine")
        return {"wars": [dict(w, territory=None) for w in ws], "ukraineOccupied": terr and terr["occupiedKm2"],
                "counts": {k2: sum(w["counts"][k2] for w in ws) for k2 in WEAPON_TAGS},
                "active": sum(1 for w in ws if w["n72"]), "aiOn": bool(groq_key())}
    if k == "sismo":
        q = quake_by_id(m["ref"])
        if not q:
            return None
        t0 = ts(q["t"])
        after = [x for x in state["quakes"] if t0 < ts(x["t"]) <= t0 + 7 * 86400
                 and km((x["lat"], x["lon"]), (q["lat"], q["lon"])) <= 40]
        return dict(q, nearest=nearest_places(q["lat"], q["lon"]), aftershocks=after[:40], nAfter=len(after),
                    maxAfter=max((x["mag"] or 0 for x in after), default=None),
                    energy=tnt(q["mag"]) if q["mag"] is not None else None)
    if k == "sismos":
        qs = [q for q in state.get("quakes") or [] if now - ts(q["t"]) <= 7 * 86400]
        day = [q for q in qs if now - ts(q["t"]) <= 86400]
        mags = [q for q in qs if q["mag"] is not None]
        return {"quakes": qs[:80], "n24": len(day), "n7": len(qs), "sensed7": sum(q["sensed"] for q in qs),
                "biggest": max(mags, key=lambda q: q["mag"]) if mags else None,
                "byZona": {z: sum(q["zona"] == z for q in qs) for z in ("Continente e Madeira", "Açores")}}
    if k in ("incendio", "incendios"):
        rcm = state.get("rcm") or {}
        fires = sorted(FIRE_SEEN.values(), key=lambda f: (bool(f.get("ended")), -f["man"]))
        if k == "incendio":
            f = FIRE_SEEN.get(m["ref"])
            if not f:
                return None
            wf = (f.get("weather") or {}).get("windFrom")
            opp = {"N": "S", "NE": "SO", "E": "O", "SE": "NO", "S": "N", "SO": "NE", "SW": "NE", "O": "E", "W": "E",
                   "NO": "SE", "NW": "SE"}
            r = rcm_for(f, rcm)
            return dict(f, rcm=r, rcmName=RCM_NAME.get(r), spreadTo=opp.get(wf), nearest=nearest_places(f["lat"], f["lon"], 3),
                        source=FIRES_SOURCE["name"], sourceNote=FIRES_SOURCE["note"],
                        others=[x for x in fires if x["id"] != f["id"] and not x.get("ended")
                                and km((x["lat"], x["lon"]), (f["lat"], f["lon"])) <= 60][:12])
        live = [f for f in fires if not f.get("ended")]
        return {"fires": live[:60], "n": len(live), "man": sum(f["man"] for f in live),
                "terrain": sum(f["terrain"] for f in live), "aerial": sum(f["aerial"] for f in live),
                "emCurso": sum((f["status"] or "").lower() == "em curso" for f in live),
                "source": FIRES_SOURCE["name"], "sourceNote": FIRES_SOURCE["note"],
                "rcmCount": {RCM_NAME[n]: sum(v["rcm"] == n for v in rcm.values()) for n in (5, 4, 3)}, "rcmTotal": len(rcm)}
    if k in ("cheia", "cheias"):
        rs = state.get("rivers") or []
        rain = [w for w in state.get("warnings") or [] if w["type"] == "Precipitação"]
        if k == "cheia":
            r = next((x for x in rs if x["id"] == m["ref"]), None)
            return dict(r, levelName=FLOOD_NAME[r["level"]], levelNowName=FLOOD_NAME[r["levelNow"]],
                        nearest=nearest_places(r["lat"], r["lon"], 3), rainWarnings=rain) if r else None
        return {"rivers": [dict(x, levelName=FLOOD_NAME[x["level"]], series=None) for x in
                           sorted(rs, key=lambda x: (-x["level"], -(x["peak"] or 0) / (x["p99"] or 1)))],
                "nCheia": sum(x["level"] >= 2 for x in rs), "nAcima": sum(x["level"] == 1 for x in rs), "rainWarnings": rain}
    if k in ("vulcao", "vulcoes"):
        vs, watch = state.get("volcanoes") or [], azores_volcano_watch()
        if k == "vulcao":
            v = next((x for x in vs if x["id"] == m["ref"]), None)
            a = next((x for x in watch if "vulcao-" + slug(x["name"]) == m["ref"]), None)
            if v:
                return dict(v, source="gvp", nearest=nearest_places(v["lat"], v["lon"], 3))
            return dict(a, source="ipma", id=m["ref"], kmLisboa=round(km((a["lat"], a["lon"]), (38.72, -9.14)))) if a else None
        return {"volcanoes": vs, "azores": watch, "period": vs[0]["period"] if vs else None,
                "nNear": sum(v["near"] for v in vs), "nNew": sum(v["new"] for v in vs)}
    if k in ("tsunami", "tsunamis"):
        ts = state.get("tsunamis") or []
        if k == "tsunami":
            t = next((x for x in ts if x["id"] == m["ref"]), None)
            return dict(t, nearest=nearest_places(t["lat"], t["lon"], 3)) if t else None
        recent = [x for x in ts if now - ts_(x["t"]) <= 7 * 86400]
        return {"bulletins": recent, "all": ts[:20], "maxLevel": max((x["level"] for x in recent), default=0),
                "ptQuakes": [q for q in state.get("quakes") or [] if (q["mag"] or 0) >= 5 and now - ts_(q["t"]) <= 7 * 86400]}
    if k in ("seca", "secas"):
        ds = state.get("droughts") or []
        if k == "seca":
            return next((dict(x, levelName=DROUGHT_NAME[x["level"]]) for x in ds if x["id"] == m["ref"]), None)
        return {"districts": sorted(ds, key=lambda x: x["rank3"]), "until": ds[0]["until"] if ds else None,
                "n3": sum(x["level3"] >= 2 for x in ds), "n12": sum(x["level12"] >= 2 for x in ds),
                "nExt": sum(x["level"] >= 3 for x in ds)}
    if k in ("calor", "frio"):
        seen = WARN_SEEN.get(k) or {}
        ws = [w for w in state.get("warnings") or [] if w["type"] == ("Tempo Quente" if k == "calor" else "Tempo Frio")]
        order = {"red": 3, "orange": 2, "yellow": 1}
        return {"warnings": ws, "ended": not ws, "lastSeen": seen.get("lastSeen"),
                "maxLevel": max((w["level"] for w in ws), key=lambda l: order[l], default=None),
                "cities": [{"city": c["city"], "lat": c["lat"], "lon": c["lon"], "now": c["temp"],
                            "max": c.get("tMaxToday"), "min": c.get("tMinToday"),
                            "max2": c.get("tMaxTomorrow"), "min2": c.get("tMinTomorrow")} for c in state.get("weather") or []]}
    return None


def kind_summary(eid, m, base):
    """Estado e números do cartão na lista de eventos, conforme o tipo."""
    k, now = m.get("kind"), time.time()
    d = event_detail(eid) or {}
    if m.get("hist"):
        return hist_summary(m, d, base) if d else dict(base, ativo=False, passada=True, alert=0)
    if k == "surto" and d:
        ai, on = d.get("ai") or {}, d["alert"] > 0
        base.update(ativo=on, passada=not on, alert=d["alert"], quando=d["date"],
                    stats=[["Países", ", ".join(d["countries"])[:40] or "—"], ["Casos", ai.get("casos") or "—"], ["Publicado", d["date"]]],
                    headline=(ai.get("resumo") or d["summary"])[:160] + (" · EMERGÊNCIA INTERNACIONAL (PHEIC)" if d["pheic"] else ""))
        return base
    if k == "surtos" and d:
        base.update(alert=0, stats=[["Surtos (30 d)", d["n30"]], ["Na Europa", d["europe30"]], ["Emergências (PHEIC)", d["pheic"]]],
                    headline=f"{d['portugal30']} em Portugal nos últimos 30 dias · fonte OMS")
        return base
    if k == "acidnuc" and d:
        on = d["alert"] > 0
        base.update(ativo=on, passada=not on, alert=d["alert"], quando=d["date"],
                    stats=[["INES", d["inesTxt"]], ["País", d["country"]], ["Data", d["date"]]], headline=d["summary"][:160])
        return base
    if k == "nucleares" and d:
        base.update(alert=0, stats=[["Eventos (60 d)", d["n60"]], ["INES máx.", d["maxInes"]], ["Na Europa", d["europe"]]],
                    headline="Almaraz a " + str(d["plants"][0]["kmPT"]) + " km da fronteira portuguesa · fonte AIEA")
        return base
    if k in ("terror", "ciber") and d:
        on = d["alert"] > 0
        base.update(ativo=on, passada=not on, alert=d["alert"], quando=d["date"],
                    stats=([["Mortos", d.get("mortos") if d.get("mortos") is not None else "—"], ["Feridos", d.get("feridos") if d.get("feridos") is not None else "—"],
                            ["Onde", f"{d.get('cidade') or ''}, {d.get('pais') or ''}".strip(", ")]] if k == "terror" else
                           [["Tipo", d.get("tipo") or "—"], ["Alvo", (d.get("alvo") or "—")[:30]], ["País", d.get("pais") or "—"]]),
                    headline=(d.get("autor") and f"Autor: {d['autor']} · " or "") + (d.get("alvo") or d.get("impacto") or ""))
        return base
    if k in ("terrorismo", "ciberataques") and d:
        base.update(alert=0, stats=[["Últimos 7 dias", d["n7"]], ["Na Europa (7 d)", d["europe7"]], ["Em Portugal (7 d)", d["portugal7"]]],
                    headline=(f"{d['deaths7']} mortos em 7 dias" if k == "terrorismo" else
                              f"CISA: {(d.get('kev') or {}).get('n7', '—')} falhas exploradas novas em 7 dias") +
                             ("" if d["aiOn"] else " · sem IA: só notícias"))
        return base
    if k == "guerra" and d and not d["lastNews"] and time.time() - STARTED < 300:
        base.update(ativo=True, passada=False, alert=(d.get("ai") or {}).get("nivel", 1),
                    stats=[["Para Portugal", "a carregar"], ["Notícias 3 dias", "…"], ["Tendência", "—"]], headline="A carregar as notícias…")
        return base  # acabou de arrancar: ainda não chegaram as notícias (não é "já aconteceu")
    if k == "guerra" and d:
        ai, on = d.get("ai") or {}, d["n72"] > 0
        lvl = (ai.get("nivel", 1) if ai else 1) if on else 0
        base.update(ativo=on, passada=not on, alert=lvl,
                    stats=[["Para Portugal", ["sem alerta", "amarelo", "laranja", "VERMELHO"][lvl]], ["Notícias 3 dias", d["n72"]],
                           ["Tendência", ai.get("tendencia") or "—"]],
                    headline=(ai.get("situacao") or (state["events"].get(d["id"]) or {}).get("news", [{}])[0].get("title") or "")[:160]
                    if on else "Sem notícias nos últimos 3 dias")
        return base
    if k == "guerras":
        lv = [ (w["ai"] or {}).get("nivel", 1) if w["n72"] else 0 for w in d.get("wars") or []]
        base.update(alert=0, stats=[["Guerras ativas", d.get("active", 0)], ["Nucleares (3 d)", d.get("counts", {}).get("nuclear", 0)],
                                    ["Químicas (3 d)", d.get("counts", {}).get("quimica", 0)]],
                    headline=f"Nível máx. para Portugal: {['nenhum', 'amarelo', 'laranja', 'vermelho'][max(lv, default=0)]}")
        return base
    ago_h = lambda iso: (now - datetime.fromisoformat(iso).timestamp()) / 3600
    if k == "sismo" and d:
        base.update(ativo=ago_h(d["t"]) < 24, quando=d["t"][:10],
                    stats=[["Magnitude", f"{d['mag']:.1f} {d['magType'] or ''}".strip() if d["mag"] is not None else "a calcular"],
                           ["Profundidade", f"{d['depth']} km"], ["Hora", d["t"]]],
                    headline=f"{d['zona']}" + (f" · sentido, grau {d['degree']}" if d["sensed"] and d["degree"] else
                                               " · sentido pela população" if d["sensed"] else "") +
                             (f" · {d['nAfter']} réplicas" if d["nAfter"] else ""))
    elif k == "sismos":
        b = d.get("biggest")
        base.update(stats=[["Sismos 24 h", d.get("n24", 0)], ["Últimos 7 dias", d.get("n7", 0)],
                           ["Sentidos (7 d)", d.get("sensed7", 0)]],
                    headline=f"Maior em 7 dias: M{b['mag']} {b['region']}" if b else "Sem sismos nos últimos 7 dias")
    elif k == "incendio" and d:
        base.update(ativo=not d.get("ended"),
                    stats=[["Operacionais", d["man"]], ["Meios aéreos", d["aerial"]], ["Estado", d["status"] or "—"]],
                    headline=f"{d['district']} · {d['freguesia'] or ''} · {d['natureza'] or ''}".strip(" ·")
                             + (" · terminou" if d.get("ended") else ""))
    elif k == "incendios":
        top = (d.get("fires") or [None])[0]
        base.update(stats=[["Ativos", d.get("n", 0)], ["Operacionais", d.get("man", 0)], ["Meios aéreos", d.get("aerial", 0)]],
                    headline=f"Maior: {top['concelho']} · {top['man']} operacionais" if top else "Nenhum incêndio ativo agora")
    elif k == "cheia" and d:
        base.update(ativo=d["level"] >= 2,
                    stats=[["Caudal hoje", f"{d['now'] or 0:.0f} m³/s"], ["Pico previsto", f"{d['peak'] or 0:.0f} m³/s"],
                           ["Nível", d["levelName"]]],
                    headline=f"Pico a {datetime.fromisoformat(d['peakDay']).strftime('%d/%m')} · {d['pct']} % do limiar de cheia · {d['trend']}")
    elif k == "cheias":
        top = (d.get("rivers") or [None])[0]
        base.update(stats=[["Rios em cheia", d.get("nCheia", 0)], ["Acima do normal", d.get("nAcima", 0)],
                           ["Rios seguidos", len(d.get("rivers") or [])]],
                    headline=f"Mais crítico: {top['rio']} em {top['local']} · {top['levelName']}" if top else "")
    elif k == "vulcao" and d:
        if d["source"] == "gvp":
            base.update(ativo=True, stats=[["Estado", d["status"].replace(" eruptiva", "")], ["País", d["country"]],
                                           ["Distância", f"{d['kmLisboa']} km"]],
                        headline=f"Relatório de {d['period']}")
        else:
            base.update(ativo=True, stats=[["Sismos 7 d", d["n7"]], ["Maior", f"M{d['maxMag']:.1f}" if d["maxMag"] else "—"],
                                           ["Ilha", d["ilha"]]], headline="Agitação sísmica (não é erupção)")
    elif k == "vulcoes":
        agit = [a for a in d.get("azores") or [] if a["state"] != "calmo"]
        base.update(stats=[["Em atividade (mundo)", len(d.get("volcanoes") or [])], ["Europa/Atlântico", d.get("nNear", 0)],
                           ["Açores com sismos", len(agit)]],
                    headline=f"Relatório semanal GVP: {d['period']}" if d.get("period") else "")
    elif k == "tsunami" and d:
        base.update(ativo=now - datetime.fromisoformat(d["t"]).timestamp() <= 24 * 3600, quando=d["t"][:10],
                    stats=[["Tipo", d["catPt"].split(":")[0]], ["Sismo", f"M{d['mag']}" if d["mag"] else "—"], ["Boletim", d["t"]]],
                    headline=f"{d['centro']} · {d['kmLisboa']} km de Lisboa")
    elif k == "tsunamis":
        b = d.get("bulletins") or []
        base.update(stats=[["Boletins (7 d)", len(b)], ["Avisos/alertas", sum(x["level"] >= 2 for x in b)], ["Último", b[0]["t"] if b else "—"]],
                    headline=f"{b[0]['catPt'].split(':')[0]} · {b[0]['region']}" if b else "Sem boletins nos últimos 7 dias")
    elif k == "seca" and d:
        base.update(ativo=d["level"] >= 1, stats=[["Chuva 3 meses", f"{d['pct3']} % do normal"], ["12 meses", f"{d['pct12']} %"],
                                                   ["Estado", d["levelName"]]], headline=f"Dados até {d['until']}")
    elif k == "secas":
        base.update(stats=[["Seca (3 meses)", f"{d.get('n3', 0)} distritos"], ["Seca (12 meses)", f"{d.get('n12', 0)} distritos"],
                           ["Extrema", d.get("nExt", 0)]], headline=f"Chuva até {d.get('until')} vs. 1991–2020")
    elif k in ("calor", "frio") and d:
        temps = [c["max" if k == "calor" else "min"] for c in d["cities"] if c.get("max" if k == "calor" else "min") is not None]
        base.update(ativo=not d["ended"],
                    stats=[["Avisos", len(d["warnings"])], ["Nível", {"red": "vermelho", "orange": "laranja", "yellow": "amarelo"}.get(d["maxLevel"], "—")],
                           ["Máx. hoje" if k == "calor" else "Mín. hoje", f"{max(temps) if k == 'calor' else min(temps):.0f} °C" if temps else "—"]],
                    headline=", ".join(sorted({w["area"] for w in d["warnings"]})) or "Os avisos terminaram")
    base["passada"] = not base["ativo"]
    base["alert"] = alert_level(k, base, d)
    return base


ALERT_NAME = [None, "amarelo", "laranja", "vermelho"]


def alert_level(k, s, d):
    """Importância de momento (0 a 3 = vermelho). Só conta o que está a acontecer."""
    if not s.get("ativo") or not d:
        return 0
    now = time.time()
    if k == "sismo":
        return quake_alert(d)
    if k == "sismos":
        return max((quake_alert(q) for q in d.get("quakes") or [] if now - datetime.fromisoformat(q["t"]).timestamp() <= 86400), default=0)
    if k == "incendio":
        return 0 if d.get("ended") else fire_alert(d)
    if k == "incendios":
        return max((fire_alert(f) for f in d.get("fires") or []), default=0)
    if k == "cheia":
        return d["level"]
    if k == "cheias":
        return max((r["level"] for r in d.get("rivers") or []), default=0)
    if k == "vulcao":
        if d.get("source") == "ipma":
            return 2
        return 3 if d["near"] and d["new"] else 2 if d["near"] else 1
    if k == "vulcoes":
        vs = d.get("volcanoes") or []
        return max([3 if v["near"] and v["new"] else 2 if v["near"] else 1 for v in vs]
                   + [2 for a in d.get("azores") or [] if a["state"] == "agitação sísmica"], default=0)
    if k == "tsunami":
        return d["level"]
    if k == "tsunamis":
        return d.get("maxLevel", 0)
    # seca: vermelho só se for severa a 3 E a 12 meses (seca de curto prazo fica, no máximo, laranja)
    drought_alert = lambda x: 3 if x["level3"] >= 2 and x["level12"] >= 2 else min(x["level"], 2)
    if k == "seca":
        return drought_alert(d)
    if k == "secas":
        return max((drought_alert(x) for x in d.get("districts") or []), default=0)
    if k in ("calor", "frio"):
        return {"red": 3, "orange": 2, "yellow": 1}.get(d.get("maxLevel"), 0)
    return 0


def warnings_alert():
    with_lv = {"red": 3, "orange": 2, "yellow": 1}
    return max((with_lv.get(w["level"], 0) for w in state.get("warnings") or []), default=0)


def make_jobs():
    """Tarefas globais (avisos, cidades) + as de cada evento (notícias; trajetória e observações
    só para os eventos com centro conhecido). Refaz-se a cada volta: apanha eventos novos."""
    jobs = {"warnings": (fetch_warnings, 60), "weather": (fetch_weather, 600),
            "quakes": (fetch_quakes, 120), "fires": (fetch_fires, 300), "rcm": (fetch_rcm, 3 * 3600),
            "rivers": (fetch_rivers, 3 * 3600), "volcanoes": (fetch_volcanoes, 3 * 3600),
            "tsunamis": (fetch_tsunamis, 120), "droughts": (fetch_droughts, 12 * 3600),
            "history": (start_history, 6 * 3600)}  # eventos passados (corre à parte)
    if config.get("descobrirNasNoticias", True):
        jobs["discover"] = (discover_events, 900)
    jobs["wars"] = (fetch_wars, 3 * 3600)        # Wikipédia + território (Ucrânia)
    jobs["terrhist"] = (start_territory_history, 6 * 3600)  # mapas antigos do território (barra de tempo), à parte
    jobs["kev"] = (fetch_cisa_kev, 6 * 3600)     # vulnerabilidades exploradas (CISA)
    jobs["outbreaks"] = (fetch_outbreaks, 3 * 3600)  # surtos (OMS)
    jobs["nuclear"] = (fetch_nuclear_events, 3 * 3600)  # eventos nucleares e radiológicos (AIEA, INES)
    jobs["warsai"] = (start_war_ai, 1800)        # nível para Portugal e resumo (IA), à parte
    for eid, ev in list(events.items()):
        if ev.get("kind") == "guerra":  # notícias de cada guerra, separadas por tipo de arma
            war = next(w for w in WARS if w[0] == eid)
            jobs[f"news:{eid}"] = ((lambda w=war: fetch_war_news(w)), 600)
            continue
        if ev.get("kind") == "guerras":  # junta as notícias de todas as guerras (sem ir à rede)
            jobs[f"news:{eid}"] = (war_overview_news, 120)
            continue
        if ev.get("kind") in ("terror", "ciber"):  # incidente: as notícias que a IA juntou a ele
            jobs[f"news:{eid}"] = ((lambda r=ev["ref"]: list((INCIDENTS.get(r) or {}).get("news") or [])), 120)
            continue
        if ev.get("kind") in ("terrorismo", "ciberataques"):
            jobs[f"news:{eid}"] = ((lambda q=tuple(ev["pesquisas"]), mu=ev["must"]: fetch_news(q, must=mu)), 600)
            continue
        past = bool(ev.get("passada"))  # já aconteceu: todas as notícias, e só de 30 em 30 min
        must = ev.get("must") or (None if ev.get("sempre") else ev["nome"])
        every = 1800 if past else 120 if ev.get("dyn") and ev.get("sempre") else 300 if ev.get("dyn") else 60
        jobs[f"news:{eid}"] = ((lambda q=tuple(ev["pesquisas"]), n=must, w=(None if past else "3d"):
                                fetch_news(q, must=n, when=w)), every)
        if ev.get("centro"):
            jobs[f"track:{eid}"] = ((lambda s=tuple(ev["centro"]): fetch_track(s)), 3 * 3600)
            # observações reais a cada 5 min (depois da trajetória do modelo, para corrigir a previsão)
            jobs[f"obs:{eid}"] = ((lambda e=eid: fetch_obs(state["events"][e].get("track") or {})), 300)
    return jobs


def refresh_loop():
    global version
    # cada fonte tem o seu ritmo (o Open-Meteo só muda de 15 em 15 min; a trajetória de 3 em 3 h)
    last = {}
    prev_loop = time.time()
    while True:
        jobs = make_jobs()
        # o PC esteve suspenso? (o relógio saltou muito mais que o intervalo normal)
        gap = time.time() - prev_loop
        prev_loop = time.time()
        if gap > REFRESH_S * 3 + 30:
            print(f"{time.strftime('%H:%M:%S')}  o PC acordou ao fim de {int(gap // 60)} min: a atualizar tudo já")
            for k in jobs:
                if k.split(":")[0] in ("news", "warnings", "weather"):
                    last[k] = 0.0
            with lock:
                state["resumedAt"] = datetime.now(timezone.utc).isoformat()

        def run(k, fn):
            kind, _, eid = k.partition(":")
            try:
                val = fn()
                with lock:
                    if eid:
                        state["events"][eid][kind] = val
                        state["events"][eid]["errors"].pop(kind, None)
                    elif kind != "discover":
                        state[kind] = val
                    state["errors"].pop(kind, None)
                last[k] = time.time()
            except Exception as e:
                with lock:
                    (state["events"][eid]["errors"] if eid else state["errors"])[kind] = str(e)
                last[k] = time.time() - jobs[k][1] + 120  # tenta outra vez daqui a 2 min
                print(f"[{k}] erro: {e}")
        due = [(k, fn) for k, (fn, every) in jobs.items() if time.time() - last.get(k, 0.0) >= every]
        ts = [threading.Thread(target=run, args=kv) for kv in due if not kv[0].startswith("obs:")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        for k, fn in due:
            if k.startswith("obs:"):
                run(k, fn)
        try:
            sync_natural()  # sismos / incêndios / calor e frio -> eventos
        except Exception as e:
            print(f"[naturais] erro: {e}")
        with lock:
            state["updatedAt"] = datetime.now(timezone.utc).isoformat()
        with cond:
            version += 1
            cond.notify_all()
        with lock:
            resumo = " ".join(f"{eid}={len(e['news'])}n/{len((e.get('track') or {}).get('points', []))}p"
                              for eid, e in state["events"].items())
        print(f"{time.strftime('%H:%M:%S')}  avisos={len(state['warnings'])} cidades={len(state['weather'])}  {resumo}")
        time.sleep(REFRESH_S)


def event_summary(eid):
    """Cartão do evento na página inicial (chamado com o lock)."""
    m, e = events[eid], state["events"][eid]
    news = e.get("news") or []
    last = news[0]["date"] if news else None
    age_h = (time.time() - datetime.fromisoformat(last).timestamp()) / 3600 if last else None
    oc = (e.get("obs") or {}).get("center")
    oc = oc if oc and oc.get("plausible") else None
    tr = e.get("track") or {}
    now_pt = tr["points"][tr["nowIndex"]] if tr.get("points") and tr.get("nowIndex", -1) >= 0 else None
    ativo = bool(m.get("sempre")) or (not m.get("passada") and age_h is not None and age_h < 48)
    return {
        "id": eid, "nome": m["nome"], "tipo": m["tipo"], "icone": m.get("icone", "🌀"),
        "nomeadoEm": m.get("nomeadoEm"), "origem": m.get("origem"), "sempre": bool(m.get("sempre")),
        "ativo": ativo,
        # já aconteceu: tempestade antiga encontrada nas notícias, ou uma com nome que deixou de ter notícias
        "passada": not ativo, "quando": m.get("quando"),
        "nNews": len(news), "lastNews": last, "headline": news[0]["title"] if news else None,
        "pressure": oc["p"] if oc else (now_pt["p"] if now_pt else None), "measured": bool(oc),
        "pos": {"lat": oc["lat"], "lon": oc["lon"]} if oc else ({"lat": now_pt["lat"], "lon": now_pt["lon"]} if now_pt else None),
        "hasTrack": bool(m.get("centro")), "kind": m.get("kind", "tempestade"),
        "cat": KIND_CAT.get(m.get("kind", "tempestade"), "Vista geral"),
        # tempestade a acontecer: o nível é o do aviso IPMA mais grave (a vista geral não conta para "ao vivo")
        "alert": warnings_alert() if ativo and not m.get("sempre") else 0,
    } if not m.get("dyn") else kind_summary(eid, m, {
        "id": eid, "nome": m["nome"], "tipo": m["tipo"], "icone": m.get("icone"), "origem": m.get("origem"),
        "sempre": bool(m.get("sempre")), "ativo": True, "nNews": len(news), "lastNews": last,
        "headline": None, "pressure": None, "measured": False, "pos": None, "hasTrack": False,
        "kind": m["kind"], "cat": KIND_CAT.get(m["kind"], "Vista geral"), "quando": None})


def events_list():
    with lock:
        # uma tempestade passada que, entretanto, fez 1 ano e meio sai da lista
        out = [event_summary(eid) for eid, m in events.items()
               if not (m.get("quandoTs") and time.time() - m["quandoTs"] > PAST_LIMIT_S)]
    # por categoria; dentro de cada uma: a vista do tipo, depois a acontecer, depois os mais recentes
    out.sort(key=lambda s: (CAT_ORDER.index(s["cat"]) if s["cat"] in CAT_ORDER else 9, not s["sempre"], not s["ativo"],
                            -(datetime.fromisoformat(s["lastNews"]).timestamp() if s["lastNews"] else 0)))
    return out


# ================= pesquisa inteligente: GPT-OSS-120B na Groq =================
# A chave fica em groq_key.txt (na pasta do projeto) ou na variável de ambiente GROQ_API_KEY.
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_ai_cache = {}


def groq_key():
    k = os.environ.get("GROQ_API_KEY")
    if not k:
        try:
            with open(os.path.join(HERE, "groq_key.txt"), encoding="utf-8") as f:
                k = f.read().strip()
        except OSError:
            k = None
    return k or None


def ai_search(q):
    """Recebe o pedido em linguagem natural e devolve os eventos que lhe correspondem (por ordem) + uma frase."""
    q = q.strip()[:200]
    key = groq_key()
    if not key:
        return {"ok": False, "error": "sem chave da Groq (groq_key.txt)"}
    hit = _ai_cache.get(q.lower())
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    evs = events_list()
    # catálogo compacto (o plano gratuito da Groq limita o tamanho do pedido): uma linha por evento, com número
    lines = []
    CAT_SHORT = {"Tempestades": "temp", "Cheias": "cheia", "Incêndios": "fogo", "Sismos": "sismo", "Vulcões": "vulc",
                 "Tsunamis": "tsun", "Secas": "seca", "Calor e frio": "calor/frio", "Vista geral": "geral"}
    for i, e in enumerate(evs):
        lines.append(f"{i}|{e['nome'][:42]}|{CAT_SHORT.get(e['cat'], e['cat'])}|{'agora' if e['ativo'] else 'passado'}|"
                     f"{e.get('quando') or ''}|{['', 'am', 'lar', 'verm'][e.get('alert') or 0]}|{(e.get('headline') or '')[:34]}")
    hoje = datetime.now().strftime("%Y-%m-%d")
    system = ("És o motor de pesquisa do Radar, um painel de eventos naturais em Portugal (tempestades, cheias, incêndios, "
              "sismos, vulcões, tsunamis, secas, calor e frio). Recebes o pedido do utilizador e a lista de eventos. "
              "Escolhe os eventos que respondem ao pedido (até 8, do mais relevante para o menos), percebendo sinónimos, "
              "lugares, datas relativas (hoje é " + hoje + ") e erros de escrita. Cada linha da lista é "
              "número|nome|categoria|agora ou passado|quando|alerta (am/lar/verm)|resumo. "
              "Responde APENAS com JSON: {\"ids\": [números das linhas], \"categoria\": nome da categoria se o pedido for claramente "
              "sobre um tipo ou null, \"resposta\": uma frase curta em português de Portugal a explicar o que encontraste}. "
              "Se nada corresponder, ids vazio e diz isso na resposta.")
    body = {"model": GROQ_MODEL, "temperature": 0, "max_completion_tokens": 700, "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": "Pedido: " + q + "\n\nEventos:\n" + "\n".join(lines)}]}
    req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                          "User-Agent": "radar-dashboard/1.0"})
    t0 = time.time()
    try:
        for attempt in (0, 1):  # limite por minuto do plano gratuito: se a espera for curta, espera e tenta outra vez
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                wait = float(e.headers.get("Retry-After") or 99) if e.code == 429 else 99
                if attempt or wait > 15:
                    raise
                time.sleep(wait + .5)
        txt = data["choices"][0]["message"]["content"] or "{}"
        m = re.search(r"\{[\s\S]*\}", txt)
        res = json.loads(m.group(0) if m else "{}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            wait = e.headers.get("Retry-After")
            return {"ok": False, "error": f"a Groq pediu para esperar{' ' + str(round(float(wait))) + ' s' if wait else ''} (limite do plano gratuito)"}
        msg = {401: "chave da Groq inválida",
               413: "pedido grande demais para a Groq"}.get(e.code, f"Groq em falha ({e.code})")
        return {"ok": False, "error": msg}
    except Exception as e:
        return {"ok": False, "error": f"Groq em falha ({type(e).__name__})"}
    ids = []
    for i in res.get("ids") or []:
        try:
            ids.append(evs[int(i)]["id"])
        except (ValueError, TypeError, IndexError):
            if i in {e["id"] for e in evs}:
                ids.append(i)
    out = {"ok": True, "ids": list(dict.fromkeys(ids))[:8], "categoria": res.get("categoria"),
           "resposta": str(res.get("resposta") or "")[:240], "ms": round((time.time() - t0) * 1000), "model": GROQ_MODEL,
           "tokens": (data.get("usage") or {}).get("total_tokens")}
    _ai_cache[q.lower()] = (time.time(), out)
    return out


def snapshot(eid=None):
    evs = events_list()
    with lock:
        out = {k: state[k] for k in ("updatedAt", "warnings", "weather", "fieldVersion", "resumedAt") if k in state}
        out["errors"] = dict(state["errors"])
        out["events"] = evs
        e, m = state["events"].get(eid), events.get(eid)
        if e and m:
            out["event"] = {k: m.get(k) for k in ("id", "nome", "tipo", "icone", "nomeadoEm", "origem", "sempre", "quando")}
            summ = next((s for s in evs if s["id"] == eid), {})
            out["event"].update(passada=summ.get("passada", False), kind=m.get("kind", "tempestade"),
                                stats=summ.get("stats"), quando=summ.get("quando") or m.get("quando"), alert=summ.get("alert", 0))
            out["detail"] = event_detail(eid) if m.get("dyn") else None
            out["event"]["hasTrack"] = bool(m.get("centro"))
            out["news"], out["obs"] = e["news"], e["obs"]
            out["track"] = e["track"] or {"points": [], "nowIndex": -1, "note":
                                         "A calcular a trajetória…" if m.get("centro") else
                                         "Este evento não tem trajetória: ainda não se sabe onde está o centro "
                                         "(foi encontrado nas notícias ou é a vista geral)."}
            # só as falhas das fontes que este tipo de evento usa
            uses = {"sismo": {"quakes"}, "sismos": {"quakes"}, "incendio": {"fires", "rcm"}, "incendios": {"fires", "rcm"},
                    "calor": {"warnings", "weather"}, "frio": {"warnings", "weather"},
                    "cheia": {"rivers"}, "cheias": {"rivers"}, "vulcao": {"volcanoes", "quakes"}, "vulcoes": {"volcanoes", "quakes"},
                    "tsunami": {"tsunamis"}, "tsunamis": {"tsunamis", "quakes"}, "seca": {"droughts"}, "secas": {"droughts"},
                    "guerra": {"wars"}, "guerras": {"wars"}, "ciberataques": {"kev"}, "ciber": set(), "terror": set(), "terrorismo": set(),
                    "surto": {"outbreaks"}, "surtos": {"outbreaks"}, "acidnuc": {"nuclear"}, "nucleares": {"nuclear"}}.get(m.get("kind"), {"warnings", "weather"})
            out["errors"] = {k: v for k, v in out["errors"].items() if k in uses}
            out["errors"].update(e["errors"])
        else:
            out.update(event=None, news=[], obs=None, track={"points": [], "nowIndex": -1})
        return json.dumps(out, ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)
        eid = (qs.get("e") or [""])[0]
        if path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            ip = self.client_address[0]
            remote = ip not in ("127.0.0.1", "::1")
            if remote:
                with lock:
                    viewers[ip] = viewers.get(ip, 0) + 1
            seen, seen_cmd = -1, cmd_seq
            try:
                while True:
                    with cond:
                        if seen == version and seen_cmd == cmd_seq:
                            cond.wait(timeout=25)
                        new_cmds = [c for c in cmds if c[0] > seen_cmd]
                    out = b""
                    for seq, name in new_cmds:  # comandos vindos do telemóvel
                        seen_cmd = seq
                        out += f"event: cmd\ndata: {name}\n\n".encode()
                    if seen != version:
                        seen = version
                        out += f"data: {snapshot(eid)}\n\n".encode()
                    # "ping" como evento (não comentário) para a TV saber que a ligação está viva
                    self.wfile.write(out or b"event: ping\ndata: 1\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            finally:
                if remote:
                    with lock:
                        viewers[ip] -= 1
                        if viewers[ip] <= 0:
                            viewers.pop(ip)
        if path == "/api/info":
            with lock:
                info = {"url": f"http://{lan_ip()}:{PORT}", "viewers": list(viewers)}
            return self._send(json.dumps(info).encode(), "application/json")
        if path == "/api/field":
            return self._send(FIELD["json"], "application/json")
        if path == "/api/state":
            return self._send(snapshot(eid or "charles").encode(), "application/json")
        if path == "/api/territory":  # barra de tempo: lista de datas, ou o mapa de uma data (?id=)
            sid = (qs.get("id") or [""])[0]
            body = (TERR_HIST["snaps"].get(sid) and dict(TERR_HIST["snaps"][sid], id=sid)) if sid else {"index": TERR_HIST["index"]}
            return self._send(json.dumps(body or {}, ensure_ascii=False).encode(), "application/json")
        if path == "/api/events":
            return self._send(json.dumps(events_list(), ensure_ascii=False).encode(), "application/json")
        if path == "/icon.png":
            with open(os.path.join(HERE, "icon.png"), "rb") as f:
                return self._send(f.read(), "image/png")
        page = "comando.html" if path.startswith("/comando") else "index.html"
        with open(os.path.join(HERE, "public", page), "rb") as f:
            self._send(f.read(), "text/html; charset=utf-8")

    def do_POST(self):
        """Telemóvel -> painel: POST /api/cmd  {"cmd": "right"} · pesquisa com IA: POST /api/ai-search {"q": "..."}"""
        global cmd_seq
        if self.path == "/api/ai-search":
            try:
                n = int(self.headers.get("Content-Length", 0))
                q = str(json.loads(self.rfile.read(min(n, 2000)) or b"{}").get("q", ""))
            except (ValueError, json.JSONDecodeError):
                q = ""
            res = ai_search(q) if len(q.strip()) >= 2 else {"ok": False, "error": "pedido vazio"}
            return self._send(json.dumps(res, ensure_ascii=False).encode(), "application/json")
        if self.path != "/api/cmd":
            self.send_response(404)
            return self.end_headers()
        try:
            n = int(self.headers.get("Content-Length", 0))
            cmd = str(json.loads(self.rfile.read(min(n, 1000)) or b"{}").get("cmd", ""))
        except (ValueError, json.JSONDecodeError):
            cmd = ""
        if cmd not in ALLOWED_CMDS and not re.fullmatch(GESTURE_RE, cmd):
            self.send_response(400)
            return self.end_headers()
        with cond:
            cmd_seq += 1
            cmds.append((cmd_seq, cmd))
            del cmds[:-50]
            cond.notify_all()
        self._send(b'{"ok":true}', "application/json")

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def set_windows_icon(title, ico):
    """pywebview no Windows ignora icon=, por isso aplicamos o .ico via Win32."""
    if sys.platform != "win32" or not os.path.exists(ico):
        return
    u32 = ctypes.windll.user32
    for _ in range(50):
        hwnd = u32.FindWindowW(None, title)
        if hwnd:
            break
        time.sleep(0.1)
    else:
        return
    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
    big = u32.LoadImageW(None, ico, IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
    small = u32.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
    u32.SendMessageW(hwnd, WM_SETICON, 1, big)
    u32.SendMessageW(hwnd, WM_SETICON, 0, small)


FW_RULE = "Radar Charles"

# ---------- não deixar o PC suspender enquanto o Radar está aberto ----------
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
awake = {"display": False}
awake_evt = threading.Event()


def keep_awake_loop():
    """O pedido ao Windows vale enquanto esta thread viver; renova-se a cada 30 s.
    Impede a suspensão por inatividade (não impede fechar a tampa ou suspender à mão)."""
    if sys.platform != "win32":
        return
    k32 = ctypes.windll.kernel32
    while True:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if awake["display"] else 0)
        k32.SetThreadExecutionState(flags)
        awake_evt.wait(30)
        awake_evt.clear()


class Api:
    """Funções chamadas pelo botão 'Enviar para TV' (só na janela pywebview)."""

    def __init__(self):
        self._window = None  # "_" para o pywebview não expor ao JS
        self._remote = None
        self._full = False

    def open_remote(self):
        """Abre (ou traz para a frente) a janela do comando no próprio PC."""
        import webview
        if self._remote is not None:
            try:
                self._remote.restore()
                self._remote.show()
                return True
            except Exception:
                self._remote = None
        self._remote = open_remote_window(webview)

        def closed():
            self._remote = None
        self._remote.events.closed += closed
        return True

    def keep_display(self, on):
        """Mantém o ecrã ligado (modo TV / a transmitir), além de não suspender."""
        awake["display"] = bool(on)
        awake_evt.set()
        return True

    def cast_screen(self):
        """Ecrã inteiro + abre o painel 'Transmitir' do Windows (Win+K)."""
        if self._window and not self._full:
            self._window.toggle_fullscreen()
            self._full = True
        time.sleep(0.6)
        if sys.platform == "win32":
            k = ctypes.windll.user32.keybd_event
            VK_LWIN, VK_K, UP = 0x5B, 0x4B, 0x2
            k(VK_LWIN, 0, 0, 0); k(VK_K, 0, 0, 0); k(VK_K, 0, UP, 0); k(VK_LWIN, 0, UP, 0)
        return True

    def enter_fullscreen(self):
        if self._window and not self._full:
            self._window.toggle_fullscreen()
            self._full = True
        return True

    def exit_fullscreen(self):
        if self._window and self._full:
            self._window.toggle_fullscreen()
            self._full = False
        return True

    def firewall_ok(self):
        if sys.platform != "win32":
            return True
        import subprocess
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FW_RULE}"],
                           capture_output=True, creationflags=0x08000000)
        return r.returncode == 0

    def allow_firewall(self):
        """Cria a regra de entrada para a porta do painel (redes privadas). O Windows pede confirmação."""
        if sys.platform != "win32":
            return True
        args = (f'advfirewall firewall add rule name="{FW_RULE}" dir=in action=allow '
                f'protocol=TCP localport={PORT} profile=private')
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", args, None, 0)
        return rc > 32  # <=32 = recusado/cancelado


REMOTE_TITLE = "Radar Charles — Comando"


def open_remote_window(webview):
    w = webview.create_window(REMOTE_TITLE, f"http://localhost:{PORT}/comando?pc=1", width=440, height=900,
                              min_size=(380, 600), background_color="#05070d")
    threading.Thread(target=set_windows_icon, args=(REMOTE_TITLE, os.path.join(HERE, "icon.ico")), daemon=True).start()
    return w


def main():
    if sys.stdout:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    if sys.platform == "win32":  # ícone próprio na barra de tarefas
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("radar.charles.dashboard")
    class Server(ThreadingHTTPServer):
        allow_reuse_address = sys.platform != "win32"  # no Windows isto deixava abrir 2x na mesma porta
    only_remote = "--comando" in sys.argv
    try:
        server = Server(("0.0.0.0", PORT), Handler)
    except OSError:
        if only_remote:  # o Radar já está aberto: abre só a janela do comando ligada a ele
            import webview
            open_remote_window(webview)
            webview.start(icon=os.path.join(HERE, "icon.ico" if sys.platform == "win32" else "icon.png"))
            return
        msg = (f"O Radar Charles já está aberto (porta {PORT}).\n\n"
               "Fecha a outra janela do Radar Charles e abre outra vez.")
        print(msg)
        if sys.platform == "win32" and "--no-window" not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, msg, "Radar Charles", 0x40)
        return
    load_events()
    load_war_cache()
    load_incidents()
    load_terr_cache()
    print("Eventos: " + ", ".join(f"{e['tipo']} {e['nome']}" for e in events.values()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=keep_awake_loop, daemon=True).start()
    print(f"\nRadar Charles:  http://localhost:{PORT}")
    print(f"Na TV (mesma rede Wi-Fi):  http://{lan_ip()}:{PORT}")
    print(f"Comando no telemóvel:      http://{lan_ip()}:{PORT}/comando\n")

    if "--no-window" in sys.argv:
        threading.Event().wait()
        return

    import webview
    title = "Radar Charles — Tempo Real"
    api = Api()
    if only_remote:  # "Radar Charles - Comando.bat" sem o painel aberto: servidor + só o comando
        open_remote_window(webview)
        webview.start(icon=os.path.join(HERE, "icon.ico" if sys.platform == "win32" else "icon.png"))
        return
    win = webview.create_window(title, f"http://localhost:{PORT}", width=1600, height=900,
                                min_size=(1000, 620), background_color="#05070d", js_api=api)
    api._window = win

    def on_start():
        set_windows_icon(title, os.path.join(HERE, "icon.ico"))

    icon = os.path.join(HERE, "icon.ico" if sys.platform == "win32" else "icon.png")
    webview.start(on_start, icon=icon)


if __name__ == "__main__":
    main()
