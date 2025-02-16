# met.api.no-proxy
A simple python proxy to fetch the current weather and reduce responses
from met.api.no with the intention to use the responses in Garmin watches
with limited memory capacity.

> NOTE: To avoid getting blocked by the API, use a valid user agent!
> Read the terms of service and FAQ of api.met.no:
> - https://api.met.no/doc/TermsOfService
> - https://api.met.no/doc/FAQ

# How to use
The fastest way to try it out is by running the `build-and-run-in-docker` if you have
docker installed. Note that it is important to use a unique USER_AGENT identifier that
contains your e-mail too. This is required by the api.met.no terms of service.

```bash
$ PROXY_USER_AGENT="app-identifier your@email.com" ./build-and-run-in-docker
```

Once the server is up and running you can use curl and jq to try it out by querying
the proxy for a latitude and longtitude:

```bash
$ curl 'http://localhost:8080?lat=59.9679&lon=10.7300' | jq .
{
  "longitude": 10.73,
  "latitude": 59.9679,
  "radar_coverage": true,
  "precipitation_amount": 0.0,
  "precipitation_rate": [
    [
      1739748900,
      0.0
    ],
    [
      1739749200,
      0.0
    ],
    [
      1739749500,
      0.0
    ],
    [
      1739749800,
      0.0
    ],
    [
      1739750100,
      0.0
    ],
    [
      1739750400,
      0.0
    ],
    [
      1739750700,
      0.0
    ],
    [
      1739751000,
      0.0
    ],
    [
      1739751300,
      0.0
    ],
    [
      1739751600,
      0.0
    ],
    [
      1739751900,
      0.0
    ],
    [
      1739752200,
      0.0
    ],
    [
      1739752500,
      0.0
    ],
    [
      1739752800,
      0.0
    ],
    [
      1739753100,
      0.0
    ],
    [
      1739753400,
      0.0
    ],
    [
      1739753700,
      0.0
    ],
    [
      1739754000,
      0.0
    ],
    [
      1739754300,
      0.0
    ],
    [
      1739754600,
      0.0
    ],
    [
      1739754900,
      0.0
    ],
    [
      1739755200,
      0.0
    ],
    [
      1739755500,
      0.0
    ]
  ],
  "air_temperature": -9.4,
  "relative_humidity": 70.7,
  "wind_from_direction": 183.5,
  "wind_speed": 0.8,
  "symbol_code": "fair_night",
  "location_name": "Lat, Lon: 59.9679, 10.73"
}
```

If you want to get reverse geocoding information for the provided latitude and longtitude
you must register for an API key at https://locationiq.com/. This is an optional feature:

```bash
$ curl 'http://localhost:8080?lat=59.9679&lon=10.7300&locationIqApiKey=<your-api-key>' | jq .
```
