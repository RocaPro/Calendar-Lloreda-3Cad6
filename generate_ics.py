import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# ==================================================
# CONFIGURACIÓN DEL EQUIPO  (lo único que hay que tocar)
# ==================================================

TEAM_NAME = 'A.D. Lloreda "A"'      # tal cual aparece en la web de Asturfutbol
TEAM_CODE = "4412"                  # solo se usa para que cada evento tenga un ID único
CATEGORIA = "3ª Cadete"
OUTPUT_FILE = "lloreda-a.ics"

# Estos valores salen de la URL de la página de jornadas (ver instrucciones)
COD_PRIMARIA = "1000120"
COMPETICION = "22880353"
GRUPO = "23079986"
TEMPORADA = "22"
TIPO_JUEGO = "1"
NUM_JORNADAS = 30                   # nº de jornadas de la liga

DURACION_PARTIDO = timedelta(hours=1, minutes=45)

PETICIONES_SESION = 0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}

# ==================================================
# SESIÓN PERSISTENTE
# ==================================================

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SESSION_INICIADA = False
PAGINAS_NO_DISPONIBLES = []

DATE_RE = re.compile(r"\d{2}-\d{2}-\d{4}")
TIME_RE = re.compile(r"\d{1,2}:\d{2}")
RUIDO_RE = re.compile(r"^[\d\s:\-–]*$")
SIN_RIVAL = "Rival por confirmar"

TIPOS = [
    "Pista de Colegio", "Hierba artificial", "Hierba natural",
    "Césped artificial", "Césped natural", "Hierba", "Césped",
    "Polideportivo", "Cubierto", "Parquet", "Tierra", "Pista",
]
TIPOS_RE = "|".join(re.escape(t) for t in TIPOS)


def norm(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


# ==================================================
# CONSULTA DE JORNADAS
# ==================================================

def fetch_jornada(j):
    global SESSION
    global SESSION_INICIADA
    global PETICIONES_SESION

    # Renovar sesión cada 8 consultas
    if not SESSION_INICIADA or PETICIONES_SESION >= 8:
        SESSION.close()
        SESSION = requests.Session()
        SESSION.headers.update(HEADERS)

        inicio = SESSION.get("https://www.asturfutbol.es/", timeout=30)
        print("Nueva sesión:", inicio.status_code, len(inicio.text))

        SESSION_INICIADA = True
        PETICIONES_SESION = 0

    url = (
        "https://www.asturfutbol.es/pnfg/NPcd/NFG_CmpJornada"
        f"?cod_primaria={COD_PRIMARIA}&CodCompeticion={COMPETICION}"
        f"&CodGrupo={GRUPO}&CodTemporada={TEMPORADA}"
        f"&CodJornada={j}&Sch_Tipo_Juego={TIPO_JUEGO}&cod_agrupacion=1"
    )

    r = SESSION.get(
        url,
        headers={"Referer": "https://www.asturfutbol.es/"},
        timeout=30
    )

    PETICIONES_SESION += 1

    print(f"Jornada {j}: HTTP {r.status_code} | HTML {len(r.text)} bytes")

    r.raise_for_status()

    if not r.text.strip() or "NLogin" in r.url:
        PAGINAS_NO_DISPONIBLES.append(j)
        raise RuntimeError(f"Asturfutbol no ha entregado la jornada {j}")

    return BeautifulSoup(r.text, "lxml")


# ==================================================
# LOCALIZACIÓN DE BLOQUES DE PARTIDOS
# ==================================================

def bloques_partido(soup):
    """Elementos más pequeños que contienen a nuestro equipo y una sola fecha."""
    candidatos = []

    for tag in soup.find_all(True):
        flat = norm(tag.get_text(" ", strip=True))

        if TEAM_NAME in flat and len(DATE_RE.findall(flat)) == 1:
            candidatos.append(tag)

    ids = {id(t) for t in candidatos}

    return [
        t for t in candidatos
        if not any(id(d) in ids for d in t.find_all(True))
    ]


def campo_tras(flat, fin):
    """Texto que sigue al visitante, hasta el tipo de pista."""
    m = re.match(
        r"\s+(.{3,90}?)\s+(?:" + TIPOS_RE + r")\b",
        flat[fin:]
    )

    return m.group(1) if m else ""


def localizar(flat, date_str, nombre):
    m = re.search(
        re.escape(date_str)
        + r"(?:\s+\d{1,2}:\d{2})?\s+"
        + re.escape(nombre),
        flat
    )

    return m.end() if m else None


# ==================================================
# EXTRACCIÓN DEL PARTIDO
# ==================================================

def parse_block(tag, jornada, flat_pagina):
    flat = norm(tag.get_text(" ", strip=True))

    tokens = [
        norm(t)
        for t in tag.get_text("|", strip=True).split("|")
    ]

    tokens = [t for t in tokens if t]

    date_str = DATE_RE.search(flat).group(0)

    resto = flat[flat.find(date_str) + len(date_str):].strip()
    tm = TIME_RE.match(resto)
    time_str = tm.group(0) if tm else None

    di = next(
        i for i, t in enumerate(tokens)
        if date_str in t
    )

    antes = [
        t for t in tokens[:di]
        if not RUIDO_RE.match(t) and t != "Descansa"
    ]

    despues = [
        t for t in tokens[di + 1:]
        if not RUIDO_RE.match(t)
    ]

    en_casa = flat.find(TEAM_NAME) < flat.find(date_str)

    if en_casa:
        rival = despues[0] if despues else ""
    else:
        rival = antes[-1] if antes else ""

    if not rival or TEAM_NAME in rival or len(rival) > 60:
        rival = SIN_RIVAL

    campo = ""
    visitante = rival if en_casa else TEAM_NAME

    if visitante != SIN_RIVAL:
        fin = localizar(flat_pagina, date_str, visitante)

        if fin is not None:
            campo = campo_tras(flat_pagina, fin)

    return {
        "jornada": jornada,
        "date": date_str,
        "time": time_str,
        "rival": rival,
        "campo": campo,
        "en_casa": en_casa,
        "tokens": tokens,
        "via": "bloque",
    }


# ==================================================
# EXTRACCIÓN ALTERNATIVA DESDE TEXTO
# ==================================================

def parse_texto(flat, jornada):
    """Plan B: busca el partido en el texto plano de la página."""

    fecha = r"(\d{2}-\d{2}-\d{4})(?:\s+(\d{1,2}:\d{2}))?"
    marcador = r"(?:\d+\s*-\s*\d*\s+)?"
    equipo = re.escape(TEAM_NAME)

    # En casa
    m = re.search(
        equipo + r"\s+" + marcador + fecha
        + r'\s+(.{1,60}?"[A-Za-z0-9]")',
        flat
    )

    if m:
        return {
            "jornada": jornada,
            "date": m.group(1),
            "time": m.group(2),
            "rival": m.group(3),
            "campo": campo_tras(flat, m.end()),
            "en_casa": True,
            "tokens": [],
            "via": "texto",
        }

    # Fuera
    m = re.search(fecha + r"\s+" + equipo, flat)

    if not m:
        return None

    pre = re.sub(r"[\d\s\-–]+$", "", flat[:m.start()])

    ult = None

    for t in re.finditer(TIPOS_RE, pre):
        ult = t

    cand = pre[ult.end():].strip() if ult else ""

    if not cand or len(cand) > 60 or "Árbitro" in cand:
        cand = SIN_RIVAL

    return {
        "jornada": jornada,
        "date": m.group(1),
        "time": m.group(2),
        "rival": cand,
        "campo": campo_tras(flat, m.end()),
        "en_casa": False,
        "tokens": [],
        "via": "texto",
    }


# ==================================================
# INFORMACIÓN DE DEPURACIÓN
# ==================================================

def depurar(soup, flat):
    print(
        "  DEBUG: el equipo aparece en la página "
        "pero no se pudo leer el partido"
    )

    for k, m in enumerate(re.finditer(re.escape(TEAM_NAME), flat)):
        if k >= 2:
            break

        print(
            "  DEBUG texto:",
            flat[max(0, m.start() - 200): m.end() + 300]
        )

    s = soup.find(string=lambda x: x and TEAM_NAME in x)

    if s is not None and s.parent is not None:
        el = s.parent.parent if s.parent.parent is not None else s.parent

        print("  DEBUG html:", re.sub(r"\s+", " ", str(el))[:900])


# ==================================================
# GENERACIÓN DEL CALENDARIO ICS
# ==================================================

def esc(s):
    return (
        s.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    "TZID:Europe/Madrid",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def generate_ics(partidos):
    nombre_cal = f"{TEAM_NAME.replace(chr(34), '')} - {CATEGORIA}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Asturfutbol Cal//Lloreda A//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{nombre_cal}",
        "X-WR-TIMEZONE:Europe/Madrid",
    ]

    lines += VTIMEZONE

    for p in partidos:
        d = datetime.strptime(p["date"], "%d-%m-%Y")

        if p["en_casa"]:
            local, visitante = TEAM_NAME, p["rival"]
        else:
            local, visitante = p["rival"], TEAM_NAME

        summary = f"J{p['jornada']}: {local} vs {visitante}"

        lines += [
            "BEGIN:VEVENT",
            f"UID:j{p['jornada']}-{TEAM_CODE}@asturfutbol-cal",
            "DTSTAMP:20260101T000000Z",
        ]

        if p["time"]:
            dt = datetime.strptime(
                f"{p['date']} {p['time']}",
                "%d-%m-%Y %H:%M"
            )

            fin = dt + DURACION_PARTIDO

            lines += [
                f"DTSTART;TZID=Europe/Madrid:{dt.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID=Europe/Madrid:{fin.strftime('%Y%m%dT%H%M%S')}",
            ]

        else:
            summary += " (hora por confirmar)"

            lines += [
                f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            ]

        descripcion = (
            f"Jornada {p['jornada']} - {CATEGORIA}. "
            "Generado automáticamente desde Asturfutbol"
        )

        lines += [
            f"SUMMARY:{esc(summary)}",
            f"LOCATION:{esc(p['campo'])}",
            f"DESCRIPTION:{esc(descripcion)}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")

    return "\r\n".join(lines) + "\r\n"


# ==================================================
# PROGRAMA PRINCIPAL
# ==================================================

def main():
    if "REVISAR" in (COMPETICION, GRUPO):
        raise SystemExit(
            "Faltan COMPETICION y GRUPO: rellénalos en la configuración."
        )

    print("Buscando partidos de", TEAM_NAME)

    partidos = []
    debug_hecho = False

    for j in range(1, NUM_JORNADAS + 1):
        try:
            soup = fetch_jornada(j)

            flat = norm(soup.get_text(" ", strip=True))

            p = None

            bloques = bloques_partido(soup)

            if bloques:
                p = parse_block(bloques[0], j, flat)
            else:
                p = parse_texto(flat, j)

            if p is None:
                if TEAM_NAME not in flat:
                    print(f"Jornada {j}: sin partido (el equipo no aparece)")

                elif re.search(
                    r"Descansa\s+" + re.escape(TEAM_NAME),
                    flat
                ):
                    print(f"Jornada {j}: descansa")

                else:
                    print(f"Jornada {j}: NO SE PUDO LEER")

                    if not debug_hecho:
                        depurar(soup, flat)
                        debug_hecho = True

            else:
                partidos.append(p)

                print(
                    f"Jornada {j}: {p['date']} "
                    f"{p['time'] or '--:--'} | "
                    f"{'CASA' if p['en_casa'] else 'FUERA'} | "
                    f"{p['rival']} | {p['campo']} "
                    f"[{p['via']}]"
                )

                if p["rival"] == SIN_RIVAL and p["tokens"]:
                    print("    tokens:", p["tokens"])

        except Exception as e:
            print(f"Jornada {j}: ERROR {e}")

        time.sleep(0.5)

    # PROTECCIÓN: no generar un calendario incompleto
    if PAGINAS_NO_DISPONIBLES:
        raise SystemExit(
            "ERROR: No se pudo consultar correctamente "
            f"las jornadas {PAGINAS_NO_DISPONIBLES}. "
            "Se conserva el calendario anterior."
        )

    print(f"Total partidos encontrados: {len(partidos)}")

    if not partidos:
        raise SystemExit(
            "No se encontraron partidos; no se modifica el calendario."
        )

    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(generate_ics(partidos))

    print(f"Archivo generado: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
