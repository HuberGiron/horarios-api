import asyncio
import re
import unicodedata
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from lxml import etree, html

SOURCE_URL = "https://enlinea.uia.mx/Consulta_Horarios/horarios.cfm"
COORDINACIONES = {
    "2320": "Mecatrónica y Sistemas Ciberfísicos",
    "2310": "Tecnologías de Cómputo y Telecomunicaciones",
    "4200": "Física",
    "2600": "Mecánica y Eléctrica",
    "4300": "Matemáticas",
    "5400": "Habilidades Verbales",
    "6075": "Reflexión Universitaria · Eje 1",
    "6076": "Reflexión Universitaria · Eje 2",
    "6077": "Reflexión Universitaria · Eje 3",
    "6078": "Reflexión Universitaria · Eje 4",
    "6100": "Servicio Social",
    "6101": "Taller de Integración Universitaria",
}
ENCABEZADOS = ("materia", "grupo", "modalidad", "sigla", "materia/profesor", "dia/salon", "horario")
DIAS = {
    "lunes": "Lunes",
    "martes": "Martes",
    "miercoles": "Miércoles",
    "jueves": "Jueves",
    "viernes": "Viernes",
    "sabado": "Sábado",
    "domingo": "Domingo",
}
SOURCE_HEADERS = {
    "Accept": "text/html",
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
    "User-Agent": "Mecatronica-IBERO-Horarios/1.0",
}
RESPONSE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "X-Content-Type-Options": "nosniff",
}

app = FastAPI(title="Horarios MANRESA", docs_url=None, redoc_url=None, openapi_url=None)


def normalizar(texto: str) -> str:
    return "".join(
        letra for letra in unicodedata.normalize("NFD", texto.casefold())
        if not unicodedata.combining(letra)
    )


def texto_celda(elemento: html.HtmlElement) -> str:
    return " ".join(" ".join(elemento.itertext()).split())


def extraer_horarios(documento: str) -> tuple[str, list[dict]]:
    # La fuente publica la materia, el día, el profesor y el salón en filas
    # sucesivas. Conservar índices de celda evita desplazar campos vacíos.
    pagina = html.fromstring(documento)
    periodos = [
        texto.removeprefix("Horarios ")
        for nodo in pagina.xpath("//pre")
        if (texto := texto_celda(nodo)).startswith("Horarios ")
    ]
    if len(periodos) != 1 or not re.search(r"\b\d{4}\b", periodos[0]):
        raise ValueError("No se encontró el periodo de oferta")

    tablas = pagina.xpath("//table[.//th]")
    for tabla in tablas:
        filas = [
            fila.xpath("./td|./th")
            for fila in tabla.xpath("./tr|./thead/tr|./tbody/tr|./tfoot/tr")
        ]
        for indice, fila in enumerate(filas):
            columnas = tuple(normalizar(texto_celda(celda)) for celda in fila)
            if columnas == ENCABEZADOS:
                return periodos[0], extraer_grupos(filas[indice + 1:])

    raise ValueError("No se encontró la tabla de horarios")


def extraer_grupos(filas: list[list[html.HtmlElement]]) -> list[dict]:
    grupos = []
    grupo = None
    sesion = None
    for fila in filas:
        valores = [texto_celda(celda) for celda in fila]
        if not any(valores):
            continue
        if all(not valor.strip("* ") for valor in valores):
            grupo = sesion = None
            continue
        if valores[0].isascii() and valores[0].isdigit():
            if len(valores) < 7 or not valores[1] or not valores[4]:
                raise ValueError("La tabla contiene una fila de grupo incompleta")
            clave, letra, modalidad, sigla, nombre, dia, horario = valores[:7]
            grupo = {
                "materia": clave, "grupo": letra, "modalidad": modalidad,
                "sigla": sigla, "nombre": nombre, "profesores": [],
                "sesiones": [], "notas": [],
            }
            grupos.append(grupo)
            sesion = agregar_sesion(grupo, dia, horario)
            continue
        if grupo is None:
            raise ValueError("La tabla contiene una fila sin grupo")

        if len(fila) == 5 and fila[4].get("colspan") == "3":
            if valores[4] and valores[4] not in grupo["notas"]:
                grupo["notas"].append(valores[4])
            continue
        if len(valores) < 7 or any(valores[:4]):
            raise ValueError("Cambió la estructura de las sesiones")

        profesor, dia_o_salon, horario = valores[4:7]
        if normalizar(dia_o_salon) in DIAS or horario:
            sesion = agregar_sesion(grupo, dia_o_salon, horario)
        elif dia_o_salon:
            if sesion is None:
                sesion = agregar_sesion(grupo, "", "", salon=dia_o_salon)
            else:
                sesion["salon"] = dia_o_salon
        if profesor and profesor not in grupo["profesores"]:
            grupo["profesores"].append(profesor)

    return grupos


def agregar_sesion(grupo: dict, dia: str, horario: str, *, salon: str = "") -> dict | None:
    if not (dia or horario or salon):
        return None
    sesion = {"dia": DIAS.get(normalizar(dia), dia), "horario": horario, "salon": salon}
    grupo["sesiones"].append(sesion)
    return sesion


def decodificar(respuesta: httpx.Response) -> str:
    # ColdFusion publica una etiqueta ISO-8859-1 aun cuando el cuerpo es UTF-8.
    try:
        return respuesta.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        codificacion = respuesta.charset_encoding or "windows-1252"
        if codificacion.lower().replace("_", "-") in {"utf8", "utf-8"}:
            codificacion = "windows-1252"
        try:
            return respuesta.content.decode(codificacion)
        except (LookupError, UnicodeDecodeError):
            return respuesta.content.decode("windows-1252", errors="replace")


async def consultar_coordinacion(client: httpx.AsyncClient, clave: str, nombre: str) -> dict:
    fuente = {
        "id": clave,
        "nombre": nombre,
        "url": f"{SOURCE_URL}?cve_coordinacion={clave}",
    }
    try:
        respuesta = await asyncio.wait_for(
            client.get(SOURCE_URL, params={"cve_coordinacion": clave, "_consulta": uuid4().hex}),
            timeout=17,
        )
        respuesta.raise_for_status()
        periodo, grupos = extraer_horarios(decodificar(respuesta))
    except (TimeoutError, httpx.TimeoutException):
        return {**fuente, "error": "La fuente tardó demasiado en responder."}
    except httpx.HTTPStatusError as error:
        return {**fuente, "error": f"La fuente respondió con HTTP {error.response.status_code}."}
    except httpx.RequestError:
        return {**fuente, "error": "No se pudo conectar con la fuente oficial."}
    except (ValueError, etree.LxmlError):
        return {**fuente, "error": "No se pudo leer la tabla publicada por la fuente."}
    return {**fuente, "periodo": periodo, "grupos": grupos}


@app.get("/api/horarios/manresa")
async def horarios_manresa() -> JSONResponse:
    async with httpx.AsyncClient(
        headers=SOURCE_HEADERS, timeout=httpx.Timeout(15, connect=7), follow_redirects=False,
    ) as client:
        fuentes = await asyncio.gather(*(
            consultar_coordinacion(client, clave, nombre)
            for clave, nombre in COORDINACIONES.items()
        ))

    disponibles = sum("error" not in fuente for fuente in fuentes)
    datos = {
        "consultadoEn": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fuentes": fuentes,
    }
    return JSONResponse(datos, status_code=200 if disponibles else 502, headers=RESPONSE_HEADERS)
