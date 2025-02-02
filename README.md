# met.api.no-proxy
A simple python proxy to reduce responses from met.api.no with the intention to use
the responses in Garmin watches with limited memory capacity.

The responses maintain the same json schema, but return less results. For a brief
moment api.met.no released a mini API that was working with a Garmin watch, so if
this API comes back, this proxy won't be needed any more.

> NOTE: To avoid getting blocked by the API, use a valid user agent!
> Read the terms of service and FAQ of api.met.no:
> - https://api.met.no/doc/TermsOfService
> - https://api.met.no/doc/FAQ
