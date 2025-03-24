#!/usr/bin/env python3

import asyncio
import datetime
import json
import os
import sys
import threading
import time
import traceback
from enum import StrEnum
from http import client
from pprint import pprint
from threading import Event, Lock
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
    TypedDict
)
from urllib.error import HTTPError

import aiohttp
from aiohttp import web
from multidict import MultiMapping

hostName = os.environ.get("BIND_ADDR", "0.0.0.0")
serverPort = int(os.environ.get("BIND_PORT", 8080))
userAgentDefault = os.environ.get("PROXY_USER_AGENT", "https://github.com/cyberang3l/met.api.no-proxy")
allowOverrideUserAgent = bool(int(os.environ.get("ALLOW_OVERRIDE_USER_AGENT", 0)))
maxItemsInCache = int(os.environ.get("MAX_ITEMS_IN_CACHE", 10000))
maxItemsIn422Cache = int(os.environ.get("MAX_ITEMS_IN_CACHE_422", 100000))
debug = os.environ.get("DEBUG", "0").lower() == "1"
cacheWebKey = os.environ.get("CACHE_WEB_KEY", "")

cacheLock = Lock()
cache = {}

# Nowcast returns 422 if the location is outside the Nordics (Norway, Sweden, Denmark, Finland)
# Cache those responses in a different cache table indefinitely as long as the cache size is below the limit.
cache422Lock = Lock()
cache422 = {}

signalCleanupThreadExit = Event()


def cleanupCacheThread():
    while not signalCleanupThreadExit.is_set():
        with cacheLock:
            for key in list(cache.keys()):
                expireTime = cache[key][0]
                if expireTime < time.time():
                    del cache[key]
        signalCleanupThreadExit.wait(60)


class bcolors(StrEnum):
    PURPLE = '\033[95m'
    BLUE = '\033[94m'
    WHITE = '\033[97m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    BROWN = '\033[33m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def pstderr(*args, color: str = bcolors.ENDC):
    print(f"[{datetime.datetime.now().strftime('%F %H:%M:%S')}] -{color}", *args, bcolors.ENDC, file=sys.stderr)


def printColor(*args, color: str = bcolors.ENDC):
    pstderr(*args, color=color)


class MetAPIType(StrEnum):
    NOWCAST = "nowcast"
    LOCATIONFORECAST = "locationforecast"
    METALERTS = "metalerts"


Timestamp = int
PrecipitationRate = float


class CuratedResponse(TypedDict, total=False):
    longitude: float
    latitude: float
    air_temperature: float
    relative_humidity: float
    wind_from_direction: float
    wind_speed: float
    symbol_code: str
    radar_coverage: bool
    precipitation_amount: float
    precipitation_rate: List[Tuple[Timestamp, PrecipitationRate]]
    location_name: str
    warning_icon: Optional[str]
    ultraviolet_index_clear_sky: Optional[float]


async def requestFromMetAPI(lat: float, lon: float, apitype: MetAPIType, userAgent: str, session: aiohttp.ClientSession) -> Tuple[int, Dict]:
    # Default to locationforecast
    url = f"https://api.met.no/weatherapi/locationforecast/2.0/complete.json?lat={lat:.4f}&lon={lon:.4f}"
    if apitype == MetAPIType.NOWCAST:
        url = f"https://api.met.no/weatherapi/nowcast/2.0/complete.json?lat={lat:.4f}&lon={lon:.4f}"
    elif apitype == MetAPIType.METALERTS:
        url = f"https://api.met.no/weatherapi/metalerts/2.0/current.json?lat={lat:.4f}&lon={lon:.4f}"

    async with session.get(url, headers={'User-Agent': userAgent}, raise_for_status=True) as resp:
        data = await resp.read()

    try:
        # Try to decode the response as JSON
        jsonData = json.loads(data)

        # Convert GMT time from string "Mon, 03 Feb 2025 21:42:10 GMT" to unix timestamp
        # And store it in the cache - we use the expire timestamp to invalidate the cache
        # when needed
        timestr = resp.headers.get("Expires")
        expireTimestamp = 0
        if timestr is not None:
            timestr = timestr.replace("GMT", "+0000")
            expireTimestamp = int(datetime.datetime.strptime(timestr, "%a, %d %b %Y %H:%M:%S %z").timestamp())

        # If decoding is successful, return the content
        return expireTimestamp, jsonData
    except json.JSONDecodeError as e:
        printColor(f"Received response: {data.decode('utf-8')}", color=bcolors.BROWN)
        printColor(f"Error decoding response as JSON: {e}", color=bcolors.RED)

    raise HTTPError(url, 500, "Invalid JSON response", client.HTTPMessage(), None)


async def requestFromLocationIQ(lat: float, lon: float, apiKey: str, session: aiohttp.ClientSession) -> Dict:
    if not apiKey:
        return {}
    url = f"https://eu1.locationiq.com/v1/reverse.php?key={apiKey}&lat={lat}&lon={lon}&format=json"
    async with session.get(url) as resp:
        return await resp.json()


async def requestFromNveAvalanche(lat: float, lon: float, session: aiohttp.ClientSession) -> Dict:
    date = datetime.date.today()
    url = f"https://api01.nve.no/hydrology/forecast/avalanche/v6.3.0/api/AvalancheWarningByCoordinates/Simple/{lat}/{lon}/no/{date}/{date}"
    async with session.get(url) as resp:
        return await resp.json()


def prepareResponse(lat: float, lon: float, nowcastResp: Dict, locationForecastResp: Dict, locationIqResp: Dict, warningIcon: Optional[str]) -> CuratedResponse:
    """
    Function that will take the response from the Met API and LocationIQ and prepare it
    for the Garmin watch.
    """

    resp: CuratedResponse = {
        "longitude": lon,
        "latitude": lat,
        "radar_coverage": False,
        "precipitation_amount": 0.0,
        "precipitation_rate": [],
        "ultraviolet_index_clear_sky": None,
    }

    if not nowcastResp and not locationForecastResp:
        raise ValueError("No weather data available")

    if nowcastResp:
        resp["radar_coverage"] = True if nowcastResp["properties"]["meta"]["radar_coverage"] == "ok" else False

        instantDetails = nowcastResp["properties"]["timeseries"][0]["data"]["instant"]["details"]
        resp["air_temperature"] = instantDetails["air_temperature"]
        resp["relative_humidity"] = instantDetails["relative_humidity"]
        resp["wind_from_direction"] = instantDetails["wind_from_direction"]
        resp["wind_speed"] = instantDetails["wind_speed"]

        next_1_hours = nowcastResp["properties"]["timeseries"][0]["data"]["next_1_hours"]
        resp["symbol_code"] = next_1_hours["summary"]["symbol_code"]
        resp["precipitation_amount"] = next_1_hours["details"]["precipitation_amount"]

        resp["precipitation_rate"] = []
        if resp["radar_coverage"]:
            for timeseries in nowcastResp["properties"]["timeseries"]:
                unixTimestamp = int(datetime.datetime.strptime(timeseries["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp())
                precipitation_rate: float = timeseries["data"]["instant"]["details"]["precipitation_rate"]
                resp["precipitation_rate"].append((unixTimestamp, precipitation_rate))

    if locationForecastResp:
        for timeseries in locationForecastResp["properties"]["timeseries"]:
            now = time.time()
            unixTimestamp = int(datetime.datetime.strptime(timeseries["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp())
            if now >= unixTimestamp and now < unixTimestamp + 3600:
                instantDetails = timeseries["data"]["instant"]["details"]
                resp["ultraviolet_index_clear_sky"] = instantDetails["ultraviolet_index_clear_sky"]
                if not nowcastResp:
                    resp["air_temperature"] = instantDetails["air_temperature"]
                    resp["relative_humidity"] = instantDetails["relative_humidity"]
                    resp["wind_from_direction"] = instantDetails["wind_from_direction"]
                    resp["wind_speed"] = instantDetails["wind_speed"]
                    next_1_hours = timeseries["data"]["next_1_hours"]
                    resp["symbol_code"] = next_1_hours["summary"]["symbol_code"]
                break
            else:
                continue

    resp["location_name"] = f"Lat, Lon: {lat}, {lon}"
    if debug:
        pprint(locationIqResp, compact=True)
    if locationIqResp and "address" in locationIqResp:
        if "locality" in locationIqResp["address"]:
            resp["location_name"] = locationIqResp["address"]["locality"]
        elif "pitch" in locationIqResp["address"]:
            resp["location_name"] = locationIqResp["address"]["pitch"]
        elif "road" in locationIqResp["address"]:
            resp["location_name"] = locationIqResp["address"]["road"]
        elif "neighbourhood" in locationIqResp["address"]:
            resp["location_name"] = locationIqResp["address"]["neighbourhood"]
        elif "suburb" in locationIqResp["address"]:
            resp["location_name"] = locationIqResp["address"]["suburb"]
        elif "municipality" in locationIqResp["address"]:
            resp["location_name"] = locationIqResp["address"]["municipality"]
        elif "village" in locationIqResp["address"]:
            resp["location_name"] = locationIqResp["address"]["village"]
        elif "city" in locationIqResp["address"]:
            resp["location_name"] = locationIqResp["address"]["city"]
        elif "display_name" in locationIqResp:
            resp["location_name"] = locationIqResp["display_name"]
        else:
            if "error" not in locationIqResp:
                printColor(f"LocationIQ response to debug: {locationIqResp}", color=bcolors.RED)

    resp["warning_icon"] = warningIcon

    return resp


def getMetAlertWarningIcon(metAlertResp: Dict):
    try:
        # These are the exceptions from just `type.lower()` in the table at
        # https://github.com/nrkno/yr-warning-icons/tree/master?tab=readme-ov-file#mapping
        warningTypeMappings = {
            "blowingsnow": "snow",
            "gale": "wind",
            "icing": "generic",
        }
        first = metAlertResp["features"][0]["properties"]
        warningType = first["event"].lower()
        warningType = warningTypeMappings.get(warningType, warningType)
        # This seems to be on the format "2; yellow; Moderate"
        warningColor = first["awareness_level"].split("; ")[1]
        return f"icon-warning-{warningType}-{warningColor}".lower()
    except (KeyError, IndexError, TypeError):
        return None


def getNveAvalancheWarningIcon(nveAvalancheResp: Dict):
    try:
        first = nveAvalancheResp[0]
        dangerLevel = first["DangerLevel"]
        levelToColor = {
            '2': 'yellow',
            '3': 'orange',
            '4': 'red',
            '5': 'red',
        }
        warningColor = levelToColor[dangerLevel]
        # Provide the same name as on https://github.com/nrkno/yr-warning-icons/
        return f"icon-warning-avalanches-{warningColor}"
    except (KeyError, IndexError, TypeError):
        return None


async def getHolisticResponse(lat: float, lon: float, userAgent: str, locationIqApiKey: str, session: aiohttp.ClientSession) -> bytes:
    """
    Function that will try to fetch all the information, compact it, and return in
    in a single request/response.

    1. First the nowcast api is queried
    2. If the nowcast query fails, the locationforecast api is queried instead
    3. If the locationIqApiKey is provided by the requester, we also try to get
       the location name or address that corresponds to the lat, lon of the query.
       If not, the lat, lon is used instead as the "locationName".
    """

    expireTimestamp = 0
    nowcast = {}
    with cache422Lock:
        if (lat, lon, MetAPIType.NOWCAST) not in cache422:
            try:
                expireTimestamp, nowcast = await requestFromMetAPI(lat, lon, MetAPIType.NOWCAST, userAgent, session)
            except aiohttp.ClientResponseError as e:
                if e.status == 422:
                    printColor(f"Received 422 nowcast response for {lat}, {lon} - adding location to cache422", color=bcolors.RED)
                    if len(cache422) >= maxItemsIn422Cache:
                        cache422.popitem()
                    cache422[(lat, lon, MetAPIType.NOWCAST)] = True
                # Do not raise the exception here, as we want to try the locationforecast api

    # Start async requests in parallel
    nveAvalancheResp = asyncio.create_task(requestFromNveAvalanche(lat, lon, session))
    metAlertResp = asyncio.create_task(requestFromMetAPI(lat, lon, MetAPIType.METALERTS, userAgent, session))
    locationIQResp = asyncio.create_task(requestFromLocationIQ(lat, lon, locationIqApiKey, session))
    locationForecastResp = asyncio.create_task(requestFromMetAPI(lat, lon, MetAPIType.LOCATIONFORECAST, userAgent, session))

    locationForecast = {}
    try:
        expireTimestamp, locationForecast = await locationForecastResp
    except BaseException:
        if not nowcast:
            # We don't have any weather data at all, so re-raise
            raise
        printColor(traceback.format_exc(), color=bcolors.BROWN)

    # Check for avalanche warnings first, then metalert warnings
    warningIcon = None
    try:
        warningIcon = getNveAvalancheWarningIcon(await nveAvalancheResp)
    except BaseException:
        printColor(traceback.format_exc(), color=bcolors.BROWN)

    if warningIcon is None:
        try:
            _, data = await metAlertResp
            warningIcon = getMetAlertWarningIcon(data)
        except BaseException:
            printColor(traceback.format_exc(), color=bcolors.BROWN)

    locationIQ = {}
    try:
        # If this fails it is not critical, so we can ignore it - we'll return
        # the lat, lon as the location name
        locationIQ = await locationIQResp
    except BaseException:
        printColor(traceback.format_exc(), color=bcolors.BROWN)

    resp = prepareResponse(lat, lon, nowcast, locationForecast, locationIQ, warningIcon)

    respBytes = json.dumps(resp).encode()

    printColor(f"Caching response - cache will be valid for {int(expireTimestamp - time.time())} seconds", color=bcolors.YELLOW)
    with cacheLock:
        if len(cache) >= maxItemsInCache:
            cache.popitem()
        cache[(lat, lon)] = (expireTimestamp, respBytes)
    return respBytes


def parseRequest(qs: MultiMapping[str]) -> Tuple[float, float, str]:
    if 'lat' not in qs or 'lon' not in qs:
        printColor(qs, color=bcolors.RED)
        raise web.HTTPBadRequest(reason="Error: expecting GET request with lat and lon parameters")

    lat, lon = float(qs['lat']), float(qs['lon'])
    if lat < -90 or lat > 90 or lon < -180 or lon > 180:
        raise web.HTTPBadRequest(reason="Error: lat and lon must be between -90 and 90 and -180 and 180 respectively")

    locationIqKey = qs.get("locationIqApiKey", "")

    return lat, lon, locationIqKey


async def handleCacheInfoRequest(request: web.Request) -> web.Response:
    reqCacheWebKey = request.query.get("cacheWebKey", "")
    if cacheWebKey == "" or reqCacheWebKey != cacheWebKey:
        raise web.HTTPBadRequest()

    try:
        if request.path == "/cache":
            with cacheLock:
                return web.Response(text=json.dumps({str(k): str(v[0]) for k, v in cache.items()}, indent=2), content_type="application/json")
        with cache422Lock:
            return web.Response(text=json.dumps([str(k) for k in cache422.keys()], indent=2), content_type="application/json")

    except web.HTTPError as e:
        printColor(f"HTTP Error requesting {e}", color=bcolors.RED)
        return e
    except BaseException:
        printColor(traceback.format_exc(), color=bcolors.RED)
        return web.HTTPRequestTimeout(reason="Error: Request Timeout")


async def handleRequest(request: web.Request) -> web.Response:
    userAgent = userAgentDefault
    if allowOverrideUserAgent:
        userAgent = request.headers.get("User-Agent", userAgentDefault)

    printColor(
        f" - Serving Incoming request {bcolors.BOLD}{request.path_qs} with User-Agent: {userAgent}",
        color=bcolors.PURPLE)

    try:
        lat, lon, locationIqApiKey = parseRequest(request.query)

        with cacheLock:
            dataExpires, data = cache.get((lat, lon), (None, None))

        if data is None or dataExpires < time.time():
            data = await getHolisticResponse(lat, lon, userAgent, locationIqApiKey, request.app['client_session'])
        else:
            printColor(f"Serving cached data for {request.path_qs} - cache expires in {int(dataExpires - time.time())} seconds", color=bcolors.YELLOW)

        printColor(
            f" - Serving data for {request.path_qs}", color=bcolors.BOLD + bcolors.GREEN)
        if debug:
            pprint(json.loads(data), compact=True)
            sys.stdout.flush()

        return web.Response(text=data.decode('utf-8'), content_type="application/json")
    except web.HTTPError as e:
        printColor(f"HTTP Error requesting {e}", color=bcolors.RED)
        return e
    except BaseException:
        printColor(traceback.format_exc(), color=bcolors.RED)
        return web.HTTPRequestTimeout(reason="Error: Request Timeout")


async def onStartup(app):
    app['client_session'] = aiohttp.ClientSession()


async def onCleanup(app):
    await app['client_session'].close()


if __name__ == "__main__":
    if debug:
        printColor("Debug enabled", color=bcolors.YELLOW)
    # Start the cleanup thread
    cleanupThread = threading.Thread(target=cleanupCacheThread)
    cleanupThread.start()

    printColor(f"api.met.no proxy started http://{hostName}:{serverPort}", color=bcolors.WHITE)
    printColor(f"Default user-agent: '{userAgentDefault}'", color=bcolors.YELLOW)
    printColor(f"Accepting requests http://{hostName}:{serverPort}?lat=XX.XXXX&lon=YY.YYYY&locationIqApiKey=<API-KEY>", color=bcolors.WHITE)

    # Start the server
    app = web.Application()
    app.router.add_get("/", handleRequest)
    app.router.add_get("/cache", handleCacheInfoRequest)
    app.router.add_get("/cache422", handleCacheInfoRequest)
    app.on_startup.append(onStartup)
    app.on_cleanup.append(onCleanup)
    web.run_app(app, host=hostName, port=serverPort)

    signalCleanupThreadExit.set()
    cleanupThread.join()

    printColor("Server stopped.", color=bcolors.WHITE)
