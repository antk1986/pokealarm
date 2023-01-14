# Standard Library Imports
import collections
from datetime import datetime, timedelta
import calendar
import random
import logging
import time
import json
import traceback

# 3rd Party Imports
from requests.packages.urllib3.util import Retry
import requests
from gevent.lock import Semaphore

# Local Imports
from PokeAlarm import Unknown
from PokeAlarm.Utilities.GenUtils import synchronize_with

log = logging.getLogger("Gmaps")


class GMaps(object):

    # Available travel modes for Distance Matrix calls
    TRAVEL_MODES = frozenset(["walking", "biking", "driving", "transit"])

    # Maximum number of requests per second
    _queries_per_second = 50
    # How often to warn about going over query limit
    _warning_window = timedelta(minutes=1)

    def __init__(self, api_key, cache_fuzz_days, cache):
        self._key = api_key
        self._lock = Semaphore
        self._max_fuzz_days = cache_fuzz_days
        self._cache = cache

        # Create a session to handle connections
        self._session = self._create_session()

        # Sliding window for rate limiting
        self._window = collections.deque(maxlen=self._queries_per_second)
        self._time_limit = datetime.utcnow()

        # Memoization dicts
        self._dm_hist = {key: dict() for key in self.TRAVEL_MODES}

    def expiration(self):
        now = datetime.utcnow()
        _, month_days = calendar.monthrange(now.year, now.month)
        fuzz_days = round(self._max_fuzz_days * random.random())
        return now + timedelta(days=month_days + fuzz_days)

    # TODO: Move into utilities
    @staticmethod
    def _create_session(retry_count=3, pool_size=3, backoff=0.25):
        """Create a session to use connection pooling."""

        # Create a session for connection pooling and
        session = requests.Session()

        # Reattempt connection on these statuses
        status_forcelist = [500, 502, 503, 504]

        # Define a Retry object to handle failures
        retry_policy = Retry(
            total=retry_count, backoff_factor=backoff, status_forcelist=status_forcelist
        )

        # Define an Adapter, to limit pool and implement retry policy
        adapter = requests.adapters.HTTPAdapter(
            max_retries=retry_policy, pool_connections=pool_size, pool_maxsize=pool_size
        )

        # Apply Adapter for all HTTPS (no HTTP for you!)
        session.mount("https://", adapter)

        return session

    def _make_request(self, service, params=None):
        """Make a request to the GMAPs API."""
        # Rate Limit - All APIs use the same quota
        if len(self._window) == self._queries_per_second:
            # Calculate elapsed time since start of window
            elapsed_time = time.time() - self._window[0]
            if elapsed_time < 1:
                # Sleep off the difference
                time.sleep(1 - elapsed_time)

        # Create the correct url
        url = f"https://maps.googleapis.com/maps/api/{service}/json"

        # Add in the API key
        if params is None:
            params = {}
        params["key"] = self._key

        # Use the session to send the request
        log.debug("%s request sending.", service)
        self._window.append(time.time())
        request = self._session.get(url, params=params, timeout=3)

        if not request.ok:
            log.debug(
                "Response body: %s",
                json.dumps(request.json(), indent=4, sort_keys=True),
            )
            # Raise HTTPError
            request.raise_for_status()

        log.debug(
            "%s request completed successfully with response %s.",
            service,
            request.status_code,
        )
        body = request.json()
        if body["status"] == "OK" or body["status"] == "ZERO_RESULTS":
            return body
        elif body["status"] == "OVER_QUERY_LIMIT":
            raise UserWarning("API Quota exceeded.")
        else:
            raise ValueError(f"Unexpected response status:\n {body}")

    @synchronize_with()
    def geocode(self, address, language="en"):
        # type: (str, str) -> tuple
        """Returns 'lat,lng' associated with the name of the place."""
        # Check for memoized results
        address = address.lower()
        latlng = self._cache.geocode(address)
        if latlng is not None:
            return latlng
        try:
            # Set parameters and make the request
            params = {"address": address, "language": language}
            response = self._make_request("geocode", params)
            # Extract the results and format into a dict
            response = response.get("results", [])
            response = response[0] if len(response) > 0 else {}
            response = response.get("geometry", {})
            response = response.get("location", {})
            if "lat" in response and "lng" in response:
                latlng = float(response["lat"]), float(response["lng"])
                # Memoize the results
                self._cache.geocode(address, latlng, self.expiration())
        except requests.exceptions.HTTPError as e:
            log.error("Geocode failed with HTTPError: %s", e.message)
        except requests.exceptions.Timeout as e:
            log.error("Geocode failed with connection issues: %s", e.message)
        except UserWarning:
            log.error("Geocode failed because of exceeded quota.")
        except Exception as e:
            log.error(
                "Geocode failed because unexpected error has occurred: %s - %s",
                type(e).__name__,
                e,
            )
            log.error("Stack trace: \n %s", traceback.format_exc())
        # Send back tuple
        return latlng

    _reverse_geocode_defaults = {
        "street_num": Unknown.SMALL,
        "street": Unknown.REGULAR,
        "address": Unknown.REGULAR,
        "address_eu": Unknown.REGULAR,
        "postal": Unknown.REGULAR,
        "neighborhood": Unknown.REGULAR,
        "sublocality": Unknown.REGULAR,
        "city": Unknown.REGULAR,
        "county": Unknown.REGULAR,
        "state": Unknown.REGULAR,
        "country": Unknown.REGULAR,
    }

    @synchronize_with()
    def reverse_geocode(self, latlng, language="en"):
        # type: (tuple, str) -> dict
        """Returns the reverse geocode DTS associated with 'lat,lng'."""
        latlng = f"{latlng[0]:.5f},{latlng[1]:.5f}"
        # Check for memoized results
        dts = self._cache.reverse_geocode(latlng)
        if dts is not None:
            return dts
        # Get defaults in case something happens
        dts = self._reverse_geocode_defaults.copy()
        try:
            # Set parameters and make the request
            params = {"latlng": latlng, "language": language}
            response = self._make_request("geocode", params)
            # Extract the results and format into a dict
            response = response.get("results", [])
            response = response[0] if len(response) > 0 else {}
            details = {}
            for item in response.get("address_components"):
                for category in item["types"]:
                    details[category] = item["short_name"]

            # Note: for addresses on unnamed roads, EMPTY is preferred for
            # 'street_num' and 'street' to avoid DTS looking weird
            dts["street_num"] = details.get("street_number", Unknown.EMPTY)
            dts["street"] = details.get("route", Unknown.EMPTY)
            dts["address"] = f'{dts["street_num"]} {dts["street"]}'
            dts["address_eu"] = f'{dts["street"]} {dts["street_num"]}'
            # Europeans are backwards
            dts["postal"] = details.get("postal_code", Unknown.REGULAR)
            dts["neighborhood"] = details.get("neighborhood", Unknown.REGULAR)
            dts["sublocality"] = details.get("sublocality", Unknown.REGULAR)
            dts["city"] = details.get(
                "locality", details.get("postal_town", Unknown.REGULAR)
            )
            dts["county"] = details.get("administrative_area_level_2", Unknown.REGULAR)
            dts["state"] = details.get("administrative_area_level_1", Unknown.REGULAR)
            dts["country"] = details.get("country", Unknown.REGULAR)

            # Memoize the results
            self._cache.reverse_geocode(latlng, dts, self.expiration())
        except requests.exceptions.HTTPError as e:
            log.error("Reverse Geocode failed with HTTPError: %s", e.message)
        except requests.exceptions.Timeout as e:
            log.error("Reverse Geocode failed with connection issues: %s", e.message)
        except UserWarning:
            log.error("Reverse Geocode failed because of exceeded quota.")
        except Exception as e:
            log.error(
                "Reverse Geocode failed because unexpected error has occurred: %s - %s",
                type(e).__name__,
                e,
            )
            log.error("Stack trace: \n %s", traceback.format_exc())
        # Send back dts
        return dts

    @synchronize_with()
    def distance_matrix(self, mode, origin, dest, lang, units):
        # Check for valid mode
        if mode not in self.TRAVEL_MODES:
            raise ValueError(f"DM doesn't support mode '{mode}'.")
        # Estimate to about ~1 meter of accuracy
        origin = f"{origin[0]:.5f},{origin[1]:.5f}"
        dest = f"{dest[0]:.5f},{dest[1]:.5f}"

        # Check for memorized results
        key = f"{origin}:{dest}"
        if key in self._dm_hist:
            return self._dm_hist[key]

        # Set defaults in case something happens
        dist_key = f"{mode}_distance"
        dur_key = f"{mode}_duration"
        dts = {dist_key: Unknown.REGULAR, dur_key: Unknown.REGULAR}
        try:
            # Set parameters and make the request
            params = {
                "mode": mode,
                "origins": origin,
                "destinations": dest,
                "language": lang,
                "units": units,
            }

            # Extract the results and format into a dict
            response = self._make_request("distancematrix", params)
            response = response.get("rows", [])
            response = response[0] if len(response) > 0 else {}
            response = response.get("elements", [])
            response = response[0] if len(response) > 0 else {}

            # Set the DTS
            dts[dist_key] = response.get("distance", {}).get("text", Unknown.REGULAR)
            dts[dur_key] = response.get("duration", {}).get("text", Unknown.REGULAR)
        except requests.exceptions.HTTPError as e:
            log.error("Distance Matrix failed with HTTPError: %s", e.message)
        except requests.exceptions.Timeout as e:
            log.error("Distance Matrix failed with connection issues: %s", e.message)
        except UserWarning:
            log.error("Distance Matrix failed because of exceeded quota.")
        except Exception as e:
            log.error(
                "Distance Matrix failed because unexpected error has occurred: %s - %s",
                type(e).__name__,
                e,
            )
            log.error("Stack trace: \n %s", traceback.format_exc())
        # Send back DTS
        return dts
