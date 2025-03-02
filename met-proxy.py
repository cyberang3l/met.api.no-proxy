#!/usr/bin/env python3

import datetime
import json
import os
import sys
import threading
import time
import traceback
from enum import StrEnum
from http import client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

hostName = os.environ.get("BIND_ADDR", "0.0.0.0")
serverPort = int(os.environ.get("BIND_PORT", 8080))
userAgentDefault = os.environ.get("PROXY_USER_AGENT", "https://github.com/cyberang3l/met.api.no-proxy")
allowOverrideUserAgent = bool(int(os.environ.get("ALLOW_OVERRIDE_USER_AGENT", 0)))
maxItemsInCache = int(os.environ.get("MAX_ITEMS_IN_CACHE", 10000))
maxItemsIn422Cache = int(os.environ.get("MAX_ITEMS_IN_CACHE_422", 100000))
debug = os.environ.get("DEBUG", "0").lower() == "1"

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


def pstderr(*args):
    print(*args, file=sys.stderr)


def printColor(*args, color: str = bcolors.ENDC):
    pstderr(color, *args, bcolors.ENDC)


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


def requestFromMetAPI(lat: float, lon: float, apitype: MetAPIType, userAgent: str) -> Tuple[int, Dict]:    # Default to locationforecast
    url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact.json?lat={lat:.4f}&lon={lon:.4f}"
    if apitype == MetAPIType.NOWCAST:
        url = f"https://api.met.no/weatherapi/nowcast/2.0/complete.json?lat={lat:.4f}&lon={lon:.4f}"
    elif apitype == MetAPIType.METALERTS:
        url = f"https://api.met.no/weatherapi/metalerts/2.0/current.json?lat={lat:.4f}&lon={lon:.4f}"
    req = Request(url)
    req.add_header('User-Agent', userAgent)
    resp = urlopen(req, timeout=1)

    try:
        data = resp.read()

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
        printColor(f"Received response: {resp.read().decode('utf-8')}", color=bcolors.BROWN)
        printColor(f"Error decoding response as JSON: {e}", color=bcolors.RED)

    raise HTTPError(url, 500, "Invalid JSON response", client.HTTPMessage(), None)


def requestFromLocationIQ(lat: float, lon: float, apiKey: str) -> Dict:
    if not apiKey:
        return {}
    url = f"https://eu1.locationiq.com/v1/reverse.php?key={apiKey}&lat={lat}&lon={lon}&format=json"
    req = Request(url)
    resp = urlopen(req, timeout=1)
    return json.loads(resp.read())


def requestFromNveAvalance(lat: float, lon: float) -> Dict:
    date = datetime.date.today()
    url = f"https://api01.nve.no/hydrology/forecast/avalanche/v6.3.0/api/AvalancheWarningByCoordinates/Simple/{lat}/{lon}/no/{date}/{date}"
    req = Request(url)
    resp = urlopen(req, timeout=1)
    return json.loads(resp.read())


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
        "precipitation_rate": []
    }

    # Use either the nowcast or the locationforecast api
    if (nowcastResp):
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

    elif locationForecastResp:
        for timeseries in locationForecastResp["properties"]["timeseries"]:
            now = time.time()
            unixTimestamp = int(datetime.datetime.strptime(timeseries["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp())
            if now >= unixTimestamp and now < unixTimestamp + 3600:
                instantDetails = timeseries["data"]["instant"]["details"]
                resp["air_temperature"] = instantDetails["air_temperature"]
                resp["relative_humidity"] = instantDetails["relative_humidity"]
                resp["wind_from_direction"] = instantDetails["wind_from_direction"]
                resp["wind_speed"] = instantDetails["wind_speed"]

                next_1_hours = timeseries["data"]["next_1_hours"]
                resp["symbol_code"] = next_1_hours["summary"]["symbol_code"]
                break
            else:
                continue

    else:
        raise ValueError("No weather data available")

    resp["location_name"] = f"Lat, Lon: {lat}, {lon}"
    if locationIqResp:
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
        elif "display_name" in locationIqResp:
            resp["location_name"] = locationIqResp["display_name"]
        else:
            if "error" not in locationIqResp:
                printColor(f"LocationIQ response to debug: {locationIqResp}", color=bcolors.RED)

    resp["warning_icon"] = warningIcon

    return resp


def getMetAlertWarningIcon(metAlertResp: Dict):
    try:
        first = metAlertResp["features"][0]["properties"]
        warningType = first["event"]
        # This seems to be on the format "2; yellow; Moderate"
        warningColor = first["awareness_level"].split("; ")[1]
        # Provide the same name as on https://github.com/nrkno/yr-warning-icons/
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


def getHolisticResponse(lat: float, lon: float, userAgent: str, locationIqApiKey: str) -> bytes:
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
                expireTimestamp, nowcast = requestFromMetAPI(lat, lon, MetAPIType.NOWCAST, userAgent)
            except HTTPError as e:
                if e.code == 422:
                    if len(cache422) >= maxItemsIn422Cache:
                        cache422.popitem()
                    cache422[(lat, lon, MetAPIType.NOWCAST)] = True
                # Do not raise the exception here, as we want to try the locationforecast api

    locationForecast = {}
    if not nowcast:
        # If the nowcast query fails, try the locationforecast api
        # Do not encapsulate this in a try-except block, as if this fails too,
        # we don't have any weather data at all and we want to return an error
        expireTimestamp, locationForecast = requestFromMetAPI(lat, lon, MetAPIType.LOCATIONFORECAST, userAgent)

    # Check for avalanche warnings first, then metalert warnings
    warningIcon = None
    try:
        nveAvalancheResp = requestFromNveAvalance(lat, lon)
        warningIcon = getNveAvalancheWarningIcon(nveAvalancheResp)
    except BaseException:
        printColor(traceback.format_exc(), color=bcolors.BROWN)

    if warningIcon is None:
        try:
            _, metAlertResp = requestFromMetAPI(lat, lon, MetAPIType.METALERTS, userAgent)
            warningIcon = getMetAlertWarningIcon(metAlertResp)
        except BaseException:
            printColor(traceback.format_exc(), color=bcolors.BROWN)

    locationIQ = {}
    try:
        # If this fails it is not critical, so we can ignore it - we'll return
        # the lat, lon as the location name
        locationIQ = requestFromLocationIQ(lat, lon, locationIqApiKey)
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


class HttpRequestHandler(BaseHTTPRequestHandler):
    def parseIncomingRequest(self) -> Tuple[float, float, str]:
        data = urlparse(self.path)
        path = data.path.replace("/", "")
        if path:
            raise ValueError(
                "Error: expecting GET request without path")

        qs = parse_qs(data.query)
        if 'lat' not in qs or 'lon' not in qs:
            printColor(data, color=bcolors.RED)
            printColor(qs, color=bcolors.RED)
            raise ValueError(
                "Error: expecting GET request with lat and lon parameters")

        lat, lon = float(qs['lat'][0]), float(qs['lon'][0])
        if lat < -90 or lat > 90 or lon < -180 or lon > 180:
            raise ValueError(
                "Error: lat and lon must be between -90 and 90 and -180 and 180 respectively")

        locationIqKey = ""
        try:
            locationIqKey = qs['locationIqApiKey'][0]
        except KeyError:
            pass

        return lat, lon, locationIqKey

    def do_GET(self):
        userAgent = userAgentDefault
        if allowOverrideUserAgent:
            userAgent = self.headers.get("User-Agent", userAgentDefault)
        printColor(
            f" - Serving Incoming request {bcolors.BOLD}{self.path} with User-Agent: {userAgent}",
            color=bcolors.PURPLE)

        try:
            lat, lon, locationIqApiKey = self.parseIncomingRequest()
            with cacheLock:
                dataExpires, data = cache.get((lat, lon), (None, None))
            if data is None or dataExpires < time.time():
                data = getHolisticResponse(lat, lon, userAgent, locationIqApiKey)
            else:
                printColor(f"Serving cached data for {self.path} - cache expires in {int(dataExpires - time.time())} seconds", color=bcolors.YELLOW)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            printColor(
                f" - Serving data for {self.path}", color=bcolors.BOLD + bcolors.GREEN)
            if debug:
                pprint(json.loads(data), compact=True)
                sys.stdout.flush()

            self.wfile.write(data)
        except BrokenPipeError:
            printColor(
                "Broken pipe - won't respond to the client",
                color=bcolors.RED)
        except HTTPError as e:
            printColor(f"HTTP Error requesting {e.url}: {e}", color=bcolors.RED)
            self.send_error(e.code, e.msg)
        except BaseException:
            printColor(traceback.format_exc(), color=bcolors.RED)
            self.send_error(408)


if __name__ == "__main__":
    if debug:
        printColor("Debug enabled", color=bcolors.YELLOW)
    # Start the cleanup thread
    cleanupThread = threading.Thread(target=cleanupCacheThread)
    cleanupThread.start()

    # Start the server
    webServer = ThreadingHTTPServer(
        (hostName, serverPort), HttpRequestHandler)
    printColor(f"api.met.no proxy started http://{hostName}:{serverPort}", color=bcolors.WHITE)
    printColor(f"Default user-agent: '{userAgentDefault}'", color=bcolors.YELLOW)
    for apitype in MetAPIType:
        printColor(f"Accepting requests http://{hostName}:{serverPort}?lat=XX.XXXX&lon=YY.YYYY&locationIqApiKey=<API-KEY>", color=bcolors.WHITE)

    try:
        webServer.serve_forever()
    except KeyboardInterrupt:
        pass

    signalCleanupThreadExit.set()

    webServer.server_close()
    cleanupThread.join()

    printColor("Server stopped.", color=bcolors.WHITE)
