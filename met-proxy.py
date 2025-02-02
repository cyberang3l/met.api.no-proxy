#!/usr/bin/env python3

import datetime
import json
import os
import sys
import time
import traceback
from copy import deepcopy
from enum import StrEnum
from http import client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

hostName = os.environ.get("BIND_ADDR", "0.0.0.0")
serverPort = int(os.environ.get("BIND_PORT", 8080))
userAgentDefault = os.environ.get("PROXY_USER_AGENT", "https://github.com/cyberang3l/met.api.no-proxy")
allowOverrideUserAgent = bool(int(os.environ.get("ALLOW_OVERRIDE_USER_AGENT", 0)))


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


class HttpRequestHandler(BaseHTTPRequestHandler):
    def parseIncomingRequest(self) -> Tuple[float, float, MetAPIType]:
        data = urlparse(self.path)
        qs = parse_qs(data.query)
        if 'lat' not in qs or 'lon' not in qs:
            printColor(data, color=bcolors.RED)
            printColor(qs, color=bcolors.RED)
            raise ValueError(
                "Error: expecting GET request with lat and lon parameters")

        path = data.path.replace("/", "")
        lat, lon = float(qs['lat'][0]), float(qs['lon'][0])

        if lat < -90 or lat > 90 or lon < -180 or lon > 180:
            raise ValueError(
                "Error: lat and lon must be between -90 and 90 and -180 and 180 respectively")

        return lat, lon, MetAPIType(path)

    @staticmethod
    def reduceLocationForecastResponse(data: Dict) -> bytes:
        # Reduce the locationforecast response to only the relevant parts
        # so that the response can fit into the Garmin watch memory
        reducedData = {}
        reducedData["geometry"] = deepcopy(data["geometry"])
        reducedData["properties"] = {}
        reducedData["properties"]["meta"] = deepcopy(data["properties"]["meta"])
        reducedData["properties"]["timeseries"] = []
        for timeseries in data["properties"]["timeseries"]:
            # Convert UTC time format from string that looks like 2025-02-02T21:27:49Z to unix timestamp
            unixTimestamp = datetime.datetime.strptime(timeseries["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
            now = time.time()
            if unixTimestamp > now - 3600 and unixTimestamp < now + 7200:
                reducedData["properties"]["timeseries"].append(deepcopy(timeseries))

        return json.dumps(reducedData).encode()

    @staticmethod
    def requestFromMetAPI(lat: float, lon: float, apitype: MetAPIType, userAgent: str) -> bytes:
        # Default to locationforecast
        url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact.json?lat={lat:.4f}&lon={lon:.4f}"
        if apitype == MetAPIType.NOWCAST:
            url = f"https://api.met.no/weatherapi/nowcast/2.0/complete.json?lat={lat:.4f}&lon={lon:.4f}"
        req = Request(url)
        req.add_header('User-Agent', userAgent)
        resp = urlopen(req, timeout=1)
        if resp.getcode() != 200:
            raise HTTPError(url, resp.getcode(), resp.msg, resp.headers, resp)

        try:
            data = resp.read()
            # Try to decode the response as JSON
            j = json.loads(data)
            if apitype == MetAPIType.LOCATIONFORECAST:
                data = HttpRequestHandler.reduceLocationForecastResponse(j)
            # If decoding is successful, return the content
            return data
        except json.JSONDecodeError as e:
            printColor(f"Received response: {resp.read().decode('utf-8')}", color=bcolors.BROWN)
            printColor(f"Error decoding response as JSON: {e}", color=bcolors.RED)

        raise HTTPError(url, 500, "Invalid JSON response", client.HTTPMessage(), None)

    def do_GET(self):
        userAgent = userAgentDefault
        if allowOverrideUserAgent:
            userAgent = self.headers.get("User-Agent", userAgentDefault)
        printColor(
            f" - Serving Incoming request {bcolors.BOLD}{self.path} with User-Agent: {userAgent}",
            color=bcolors.PURPLE)

        try:
            lat, lon, apitype = self.parseIncomingRequest()
            data = HttpRequestHandler.requestFromMetAPI(lat, lon, apitype, userAgent)
            self.send_response(200)
            self.send_header("Content-type", f"application/json")
            self.end_headers()
            printColor(
                f" - Serving data for {self.path}", color=bcolors.BOLD + bcolors.GREEN)
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
    webServer = ThreadingHTTPServer(
        (hostName, serverPort), HttpRequestHandler)
    printColor(f"api.met.no proxy started http://{hostName}:{serverPort}", color=bcolors.WHITE)
    printColor(f"Default user-agent: '{userAgentDefault}'", color=bcolors.YELLOW)
    for apitype in MetAPIType:
        printColor(f"Accepting requests http://{hostName}:{serverPort}/{apitype}?lat=XX.XXXX&lon=YY.YYYY", color=bcolors.WHITE)

    try:
        webServer.serve_forever()
    except KeyboardInterrupt:
        pass

    webServer.server_close()

    printColor("Server stopped.", color=bcolors.WHITE)
