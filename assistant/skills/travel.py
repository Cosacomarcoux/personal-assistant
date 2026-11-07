"""Travel: cheapest flights and hotels (Google Flights/Hotels via SerpApi), trip planning,
budgets and translation."""
import httpx

from .base import Skill, Tool, schema

API = "https://serpapi.com/search.json"
CLASSES = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
STOPS = {"any": 0, "nonstop": 1, "max_1": 2, "max_2": 3}


def _serpapi(ctx, params: dict) -> dict:
    key = ctx.config.serpapi_api_key
    if not key:
        raise RuntimeError("Flight/hotel search needs the SERPAPI_API_KEY setting (see README). "
                           "Meanwhile, use web_search.")
    r = httpx.get(API, params={**params, "api_key": key, "hl": "en"}, timeout=60)
    data = r.json()
    if r.status_code != 200 or data.get("error"):
        raise RuntimeError(f"search failed: {data.get('error', r.text[:200])}")
    return data


def _hm(minutes) -> str:
    return f"{int(minutes) // 60}h{int(minutes) % 60:02d}" if minutes else "?"


def _flight(o: dict) -> dict:
    legs = o.get("flights", [])
    return {
        "price": o.get("price"),
        "total_time": _hm(o.get("total_duration")),
        "stops": max(len(legs) - 1, 0),
        "airlines": sorted({f.get("airline") for f in legs if f.get("airline")}),
        "legs": [f"{f['departure_airport'].get('id')} {f['departure_airport'].get('time', '')[-5:]} → "
                 f"{f['arrival_airport'].get('id')} {f['arrival_airport'].get('time', '')[-5:]}"
                 f" ({f.get('flight_number', '')}, {_hm(f.get('duration'))})" for f in legs],
        "layovers": [f"{l.get('id')} {_hm(l.get('duration'))}" + (" overnight" if l.get("overnight") else "")
                     for l in o.get("layovers", [])],
        "departs": legs[0]["departure_airport"].get("time") if legs else None,
    }


def flights_search(a, ctx):
    params = {
        "engine": "google_flights",
        "departure_id": a["from"].upper().replace(" ", ""),
        "arrival_id": a["to"].upper().replace(" ", ""),
        "outbound_date": a["depart_date"],
        "type": 1 if a.get("return_date") else 2,
        "adults": a.get("adults", 1),
        "travel_class": CLASSES.get(a.get("travel_class", "economy"), 1),
        "stops": STOPS.get(a.get("max_stops", "any"), 0),
        "currency": a.get("currency") or ctx.config.currency,
        "sort_by": 2 if a.get("sort", "price") == "price" else 1,
    }
    if a.get("return_date"):
        params["return_date"] = a["return_date"]
    if a.get("max_price"):
        params["max_price"] = int(a["max_price"])
    data = _serpapi(ctx, params)
    options = data.get("best_flights", []) + data.get("other_flights", [])
    options.sort(key=lambda o: o.get("price") or 10**9)
    insights = data.get("price_insights", {})
    return {
        "currency": params["currency"],
        "round_trip_prices": bool(a.get("return_date")),
        "price_level": insights.get("price_level"),
        "typical_price_range": insights.get("typical_price_range"),
        "cheapest": [_flight(o) for o in options[: int(a.get("limit", 6))]],
        "google_flights_link": data.get("search_metadata", {}).get("google_flights_url"),
    } if options else "No flights found for that search."


def hotels_search(a, ctx):
    params = {
        "engine": "google_hotels", "q": a["place"],
        "check_in_date": a["check_in"], "check_out_date": a["check_out"],
        "adults": a.get("adults", 1), "currency": a.get("currency") or ctx.config.currency,
        "sort_by": 3 if a.get("sort", "price") == "price" else 8,
    }
    if a.get("max_price_per_night"):
        params["max_price"] = int(a["max_price_per_night"])
    if a.get("min_rating"):
        params["rating"] = {3.5: 7, 4.0: 8, 4.5: 9}.get(float(a["min_rating"]), 7)
    data = _serpapi(ctx, params)
    hotels = [{
        "name": h.get("name"), "type": h.get("type"),
        "per_night": (h.get("rate_per_night") or {}).get("lowest"),
        "total": (h.get("total_rate") or {}).get("lowest"),
        "rating": h.get("overall_rating"), "reviews": h.get("reviews"),
        "stars": h.get("extracted_hotel_class"), "location_rating": h.get("location_rating"),
        "link": h.get("link"),
    } for h in data.get("properties", [])[: int(a.get("limit", 8))]]
    return {"currency": params["currency"], "hotels": hotels} if hotels else "No hotels found."


class TravelSkill(Skill):
    def __init__(self):
        super().__init__(
            name="Travel & languages",
            description="Cheapest flights and hotels, trip plans, travel budgets, translation.",
            instructions=(
                "FLIGHTS: use IATA codes; for the cheapest fare also try nearby airports "
                "(comma-separate them, e.g. 'TPE,TSA') and, if dates are flexible, a few dates "
                "(each search costs quota, about 250 a month, so normally max 6 searches per "
                "question). Prices with a return date are round-trip totals. Say whether the "
                "price is low/typical/high, mention long or overnight layovers, and always give "
                "the Google Flights link to book. You can't book or pay. "
                "HOTELS: hotels_search sorted by price, balance price against rating and location. "
                "TRIPS AND BUDGETS: check the calendar for conflicts, build a day-by-day plan, "
                "and a budget split into flights, stay, food, local transport and activities, "
                "using web_search for local prices, visas and opening hours. Find ways to stretch "
                "the budget (cheaper days, nearby airports, longer layovers if the user accepts). "
                "Offer to save the plan as a note and put the trip in the calendar. "
                "TRANSLATION: translate between any languages. When the user wants to say "
                "something to someone, give the translation, a simple pronunciation guide (e.g. "
                "pinyin for Mandarin; Taiwan uses Traditional characters), and keep it short so "
                "it can be read aloud. For photos of menus, signs or documents, translate what "
                "matters and explain it."
            ),
            tools=[
                Tool("flights_search", "Find the cheapest flights (Google Flights prices).",
                     schema({"from": {"type": "string", "description": "IATA code(s), e.g. 'TPE' or 'TPE,TSA'"},
                             "to": {"type": "string", "description": "IATA code(s), e.g. 'ARN'"},
                             "depart_date": {"type": "string", "description": "YYYY-MM-DD"},
                             "return_date": {"type": "string", "description": "YYYY-MM-DD; omit for one-way"},
                             "adults": {"type": "integer"},
                             "travel_class": {"type": "string", "enum": list(CLASSES)},
                             "max_stops": {"type": "string", "enum": list(STOPS)},
                             "max_price": {"type": "number"},
                             "currency": {"type": "string", "description": "e.g. ARS, USD, EUR"},
                             "sort": {"type": "string", "enum": ["price", "best"]},
                             "limit": {"type": "integer", "description": "Default 6"}},
                            ["from", "to", "depart_date"]), flights_search),
                Tool("hotels_search", "Find hotels/stays with prices (Google Hotels).",
                     schema({"place": {"type": "string", "description": "City or area, e.g. 'Tokyo Shinjuku'"},
                             "check_in": {"type": "string", "description": "YYYY-MM-DD"},
                             "check_out": {"type": "string", "description": "YYYY-MM-DD"},
                             "adults": {"type": "integer"},
                             "max_price_per_night": {"type": "number"},
                             "min_rating": {"type": "number", "enum": [3.5, 4.0, 4.5]},
                             "currency": {"type": "string"},
                             "sort": {"type": "string", "enum": ["price", "rating"]},
                             "limit": {"type": "integer", "description": "Default 8"}},
                            ["place", "check_in", "check_out"]), hotels_search),
            ],
        )
