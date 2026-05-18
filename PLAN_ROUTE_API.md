# ChargeRout Backend — `/plan-route` Integration Guide

A complete contract specification for the iOS / mobile client. The backend is
a FastAPI service that, given a start point, destination, vehicle ID, and
state-of-charge parameters, returns a fully-planned EV route including all
charging stops, per-leg navigation maneuvers, energy usage, and decoded
polylines ready to draw on a map.

---

## Base URL

```
http://<host>:<port>
```

Replace with whatever the backend is reachable at (typical local dev:
`http://localhost:8000`). The path below is relative to that base.

No authentication. No rate limit. JSON in / JSON out.

---

## Endpoint

### `POST /plan-route`

Plans the full trip in one round trip. The server runs the Valhalla routing,
computes per-segment energy via the physics model, picks charging stops along
the route via the ChargeIndex API, and returns the final assembled route.

**Request headers**

```
Content-Type: application/json
Accept: application/json
```

**Status codes**

| Code | Meaning |
|------|---------|
| 200  | Plan calculated successfully. |
| 400  | Bad input or planning failure. JSON body: `{ "detail": "..." }`. Common causes: no charging window found on the route, no valid charger in window, unknown vehicle ID falls back silently — does **not** 400. |
| 422  | FastAPI / Pydantic validation error (wrong types, missing required fields, `initial_soc` out of `[0.01, 1.0]`). |
| 500  | Unexpected server error. Safe to retry once. |

Error body shape (400 / 500):

```json
{ "detail": "Kein valides Lade-Fenster auf der Route gefunden." }
```

Pydantic 422 body has the standard `{ "detail": [{"loc": [...], "msg": "..."}, ...] }`.

---

## Request body

```json
{
  "start_lat": 48.2082,
  "start_lon": 16.3725,
  "dest_lat": 47.8095,
  "dest_lon": 13.0550,
  "vehicle_id": "tesla_model_3_lr",
  "initial_soc": 0.85,
  "target_soc": 0.10,
  "window_start_soc": 0.20,
  "window_end_soc": 0.05,
  "price_time_weight": 3.0,
  "consumption_factor": 1.0,
  "speed_factor": 1.0,
  "degradation": 0.0
}
```

### Field reference

| Field                | Type    | Required | Default               | Range          | Description |
|----------------------|---------|----------|-----------------------|----------------|-------------|
| `start_lat`          | number  | yes      | —                     | [-90, 90]      | WGS84 latitude of start point. |
| `start_lon`          | number  | yes      | —                     | [-180, 180]    | WGS84 longitude of start point. |
| `dest_lat`           | number  | yes      | —                     | [-90, 90]      | WGS84 latitude of destination. |
| `dest_lon`           | number  | yes      | —                     | [-180, 180]    | WGS84 longitude of destination. |
| `vehicle_id`         | string  | no       | `"tesla_model_3_lr"`  | see list below | Selects vehicle physics + charging curve. Unknown IDs silently fall back to `"generic_ev"`. |
| `initial_soc`        | number  | no       | `1.0`                 | `[0.01, 1.0]`  | State of charge at start. `0.85` = 85 %. **Validation enforced** — values outside the range return `422`. |
| `target_soc`         | number  | no       | `0.10`                | `[0, 1]`       | Desired SoC at the destination (reserve buffer). |
| `window_start_soc`   | number  | no       | `0.20`                | `[0, 1]`       | SoC threshold at which the planner starts looking for a charging station. |
| `window_end_soc`     | number  | no       | `0.05`                | `[0, 1]`       | SoC threshold by which the driver must have reached a charger (latest-possible point). |
| `price_time_weight`  | number? | no       | `3.0`                 | `>= 0`         | Pain-score weight: 1 € equals X minutes of detour pain. `1.0` = business traveller (prefers fast), `3.0` = budget-conscious. `null` falls back to `.env`. |
| `consumption_factor` | number  | no       | `1.0`                 | `> 0`          | Multiplier on calculated consumption. `1.1` = 10 % more (e.g. winter / roof box). |
| `speed_factor`       | number  | no       | `1.0`                 | `> 0`          | Multiplier on driving speed. |
| `degradation`        | number  | no       | `0.0`                 | `[0, 1]`       | Battery degradation. `0.05` = 5 % capacity lost. |

> **SoC ordering invariant:** `target_soc ≤ window_end_soc ≤ window_start_soc ≤ initial_soc`.
> The backend does not currently enforce this ordering — pass sane values from
> the client.

### Vehicle IDs

| `vehicle_id`           | Display name                  | Battery (kWh) | Max charge (kW) |
|------------------------|-------------------------------|---------------|-----------------|
| `tesla_model_3_lr`     | Tesla Model 3 Long Range      | 82.0          | 250             |
| `tesla_model_y_lr`     | Tesla Model Y Long Range      | 78.1          | 250             |
| `vw_id4_pro`           | VW ID.4 Pro                   | 77.0          | 135             |
| `generic_ev`           | Generisches E-Auto (Standard) | 75.0          | 150             |

Unknown IDs do **not** error — they fall back to `generic_ev`. If the UI lets
the user pick, restrict the picker to this list to avoid silent fallback.

---

## Response body

Single JSON object with **five top-level keys**: `summary`, `chargers`,
`route_geometry`, `navigation`, `leg_metrics`.

```json
{
  "summary": { ... },
  "chargers": [ ... ],
  "route_geometry": [ ... ],
  "navigation": [ ... ],
  "leg_metrics": [ ... ]
}
```

### `summary` — single object, headline figures

| Field                       | Type   | Description |
|-----------------------------|--------|-------------|
| `vehicle`                   | string | Display name of the planned vehicle (`vehicle.name`). |
| `total_distance_km`         | number | Total trip length, km, 2 decimals. |
| `drive_time_min`            | number | Pure driving minutes (no charging), 1 decimal. |
| `charge_time_min`           | number | Sum of all charging stops, 1 decimal. |
| `total_trip_time_min`       | number | `drive_time_min + charge_time_min`, 1 decimal. |
| `soc_at_destination_pct`    | number | Predicted SoC at destination, percent (`0–100`), 1 decimal. |
| `num_stops`                 | int    | Number of charging stops in the plan. `0` = no stops needed. |
| `remaining_leg_distance_km` | number | Distance of the final leg from the last charger to the destination, km. |
| `trip_energy_kwh`           | number | Total energy consumed across all legs, kWh, 2 decimals. |

### `chargers` — array, one entry per stop, in driving order

May be empty (`num_stops = 0`).

| Field                  | Type            | Description |
|------------------------|-----------------|-------------|
| `operator`             | string          | Operator name (legacy alias of `operator_name`). |
| `operator_name`        | string          | Canonical operator name as reported by the NAP. |
| `max_power_kw`         | number          | Station's nameplate max power, kW, 1 decimal. |
| `effective_charge_kw`  | number          | The min of station max and vehicle max — what the car will actually draw, kW. |
| `lat`                  | number          | WGS84 latitude of the charger. |
| `lon`                  | number          | WGS84 longitude of the charger. |
| `source_id`            | string \| null  | ChargeIndex NAP-assigned ID, e.g. `"6b26447f-...-256d"`. Stable across re-plans; use as a key for favourites/selection state. |
| `charged_kwh`          | number          | Energy delivered at this stop, kWh, 2 decimals. |
| `charge_time_min`      | number          | Time at this stop, minutes (uses real charging-curve simulation), 1 decimal. |
| `soc_at_arrival_pct`   | number          | SoC on arrival at the charger, percent, 1 decimal. |
| `soc_after_charge_pct` | number          | SoC when leaving, percent, 1 decimal. |
| `detour_min`           | number          | Extra minutes vs. straight-through. `0` if charger is on the line. |
| `estimated_cost`       | number          | EUR for this stop (= `price_per_kwh_used × charged_kwh`), 2 decimals. |
| `price_per_kwh_used`   | number          | EUR/kWh actually used in the cost calculation. See `price_source` below for provenance. |
| `price_source`         | string          | Where `price_per_kwh_used` came from. One of: `"chargeindex_live"` (real NAP price), `"manual_override"` (hit in `operator_prices.json`), `"default_fallback"` (no data — used the system default of **0.89 €/kWh**). **Surface this in the UI** — fallback-priced stops should be visibly flagged as estimated. |
| `top_5_alternatives`   | array           | Top 5 ranked stations in this charging window, including the chosen one at index 0. See structure below. Use this for an "alternatives" UI so the driver can swap to a different station without re-planning. |

#### `chargers[*].top_5_alternatives[*]`

Five entries per stop (or fewer if the window had fewer chargers). Index 0 is the
chosen charger (`is_chosen: true`); the rest are next-best alternatives in
descending ranking order. Numbers are from the ranking estimates (cheap, no
extra Valhalla call per alt) — they are slightly less precise than the
top-level chosen-charger numbers but consistent across the array.

| Field                | Type           | Description |
|----------------------|----------------|-------------|
| `rank`               | int            | 1-based rank within the window. `1` for the chosen one. |
| `is_chosen`          | boolean        | `true` for the entry at index 0 (the planner's pick), `false` for the others. |
| `operator_name`      | string         | Operator name. |
| `max_power_kw`       | number         | Station max power, kW. |
| `lat`                | number         | WGS84 latitude. |
| `lon`                | number         | WGS84 longitude. |
| `city`               | string \| null | City as reported by the NAP. |
| `source_id`          | string \| null | ChargeIndex source ID. |
| `detour_min`             | number         | Extra minutes vs. driving straight. |
| `charge_time_min`        | number         | Estimated minutes at this charger. |
| `kwh_to_charge`          | number         | Estimated energy to deliver here, kWh. |
| `soc_at_arrival_pct`     | number         | Estimated SoC on arrival at this alternative, percent, 1 decimal. From the cheap ranking estimate — for the chosen charger (`is_chosen: true`) the top-level field of the same name is the precise Valhalla-based value, and may differ by a few percent. |
| `soc_after_charge_pct`   | number         | Estimated SoC when leaving this alternative, percent, 1 decimal. All alternatives in the same window charge to the same target SoC (the configured `MAX_CHARGE_SOC`, default 80 %). The chosen charger's top-level `soc_after_charge_pct` may be slightly higher due to the smart-low-SoC bonus. |
| `estimated_cost`         | number         | EUR for the session (`price_per_kwh_used × kwh_to_charge`). |
| `price_per_kwh_used`     | number         | EUR/kWh used in this alternative's cost. |
| `price_source`           | string         | Same enum as the top-level field. |
| `score`                  | number         | Pain-per-kWh score. Lower = better. Useful for showing the "delta vs. chosen" in UI. |

### `route_geometry` — array, one entry per leg

A "leg" is the drive between two consecutive waypoints
(`start → stop1 → stop2 → ... → destination`). For a 2-stop trip there are 3
legs.

| Field                | Type   | Description |
|----------------------|--------|-------------|
| `leg_index`          | int    | 0-based, in driving order. |
| `distance_km`        | number | Leg distance, km, 2 decimals. |
| `drive_time_min`     | number | Leg drive time, minutes, 2 decimals. |
| `energy_usage_kwh100`| number | Energy use over the leg, kWh per 100 km, 2 decimals. |
| `elevation_gain_m`   | number | Cumulative climb, metres, 1 decimal. |
| `elevation_loss_m`   | number | Cumulative descent, metres, 1 decimal. |
| `net_elevation_m`    | number | End elevation minus start elevation, metres, 1 decimal. |
| `encoded_shape`      | string | Valhalla-flavour polyline, **precision 6** (not Google's precision 5). |
| `decoded_shape`      | array  | `[[lat, lon], [lat, lon], ...]` — the polyline already decoded server-side for convenience. **Note ordering: `[lat, lon]`, not `[lon, lat]`.** |

> Use `encoded_shape` if your map SDK can decode polyline-6 (Mapbox, MapLibre)
> — it's smaller over the wire. Use `decoded_shape` if you'd otherwise have to
> roll your own decoder.

### `navigation` — array, one entry per leg, with turn-by-turn

Mirrors `route_geometry` for distance/time/energy/elevation, plus:

| Field           | Type   | Description |
|-----------------|--------|-------------|
| `title`         | string | Localised German leg title, e.g. `"Etappe 1: Fahrt zum Ladestopp 1"` or `"Etappe 2: Fahrt zum Ziel"`. |
| `maneuvers`     | array  | Turn-by-turn list. Each entry: `{ "distance_km": number, "instruction": string }`. Instructions are localised German strings from Valhalla. |

### `leg_metrics` — array, one entry per leg

Same fields as `navigation` minus `maneuvers`. Useful for a metrics-only view
that doesn't need turn-by-turn.

---

## Concrete examples

### Request

```bash
curl -X POST http://localhost:8000/plan-route \
  -H 'Content-Type: application/json' \
  -d '{
    "start_lat": 48.2082, "start_lon": 16.3725,
    "dest_lat":  47.8095, "dest_lon":  13.0550,
    "vehicle_id": "tesla_model_3_lr",
    "initial_soc": 0.30,
    "price_time_weight": 3.0
  }'
```

### Response — abridged

```json
{
  "summary": {
    "vehicle": "Tesla Model 3 Long Range",
    "total_distance_km": 296.45,
    "drive_time_min": 178.3,
    "charge_time_min": 22.7,
    "total_trip_time_min": 201.0,
    "soc_at_destination_pct": 12.4,
    "num_stops": 1,
    "remaining_leg_distance_km": 142.18,
    "trip_energy_kwh": 53.87
  },
  "chargers": [
    {
      "operator": "EnBW mobility+ AG & Co. KG",
      "operator_name": "EnBW mobility+ AG & Co. KG",
      "max_power_kw": 300.0,
      "effective_charge_kw": 250.0,
      "lat": 48.1185,
      "lon": 14.8703,
      "source_id": "AT*ELL*L04281",
      "charged_kwh": 41.2,
      "charge_time_min": 22.7,
      "soc_at_arrival_pct": 8.3,
      "soc_after_charge_pct": 58.5,
      "detour_min": 1.2,
      "estimated_cost": 26.78,
      "price_per_kwh_used": 0.65,
      "price_source": "chargeindex_live",
      "top_5_alternatives": [
        { "rank": 1, "is_chosen": true,  "operator_name": "EnBW mobility+ AG & Co. KG", "max_power_kw": 300, "lat": 48.1185, "lon": 14.8703, "city": "Amstetten", "source_id": "AT*ELL*L04281", "detour_min": 1.2, "charge_time_min": 22.7, "kwh_to_charge": 41.2, "soc_at_arrival_pct": 8.3,  "soc_after_charge_pct": 58.5, "estimated_cost": 26.78, "price_per_kwh_used": 0.65, "price_source": "chargeindex_live",  "score": 2.18 },
        { "rank": 2, "is_chosen": false, "operator_name": "IONITY",                     "max_power_kw": 350, "lat": 48.1101, "lon": 14.8810, "city": "Amstetten", "source_id": "AT*ION*L00081", "detour_min": 2.4, "charge_time_min": 19.3, "kwh_to_charge": 40.9, "soc_at_arrival_pct": 8.6,  "soc_after_charge_pct": 58.5, "estimated_cost": 28.22, "price_per_kwh_used": 0.69, "price_source": "chargeindex_live",  "score": 2.34 },
        { "rank": 3, "is_chosen": false, "operator_name": "Tesla, Inc.",                "max_power_kw": 250, "lat": 48.1062, "lon": 14.8693, "city": "Amstetten", "source_id": "AT*TES*L00012", "detour_min": 1.8, "charge_time_min": 21.1, "kwh_to_charge": 41.1, "soc_at_arrival_pct": 8.4,  "soc_after_charge_pct": 58.5, "estimated_cost": 36.58, "price_per_kwh_used": 0.89, "price_source": "default_fallback", "score": 2.81 }
      ]
    }
  ],
  "route_geometry": [
    {
      "leg_index": 0,
      "distance_km": 154.27,
      "drive_time_min": 92.1,
      "energy_usage_kwh100": 18.5,
      "elevation_gain_m": 421.3,
      "elevation_loss_m": 198.7,
      "net_elevation_m": 222.6,
      "encoded_shape": "y{rjHcrk_Bp...",
      "decoded_shape": [[48.2082, 16.3725], [48.207, 16.371], ...]
    },
    { "leg_index": 1, "...": "..." }
  ],
  "navigation": [
    {
      "leg_index": 0,
      "title": "Etappe 1: Fahrt zum Ladestopp 1",
      "distance_km": 154.27,
      "drive_time_min": 92.1,
      "energy_usage_kwh100": 18.5,
      "elevation_gain_m": 421.3,
      "elevation_loss_m": 198.7,
      "net_elevation_m": 222.6,
      "maneuvers": [
        { "distance_km": 0.4, "instruction": "Auf der Wiedner Hauptstraße nach Süden fahren." },
        { "distance_km": 12.8, "instruction": "Rechts auf die A23 abbiegen." }
      ]
    }
  ],
  "leg_metrics": [
    { "leg_index": 0, "title": "Etappe 1: Fahrt zum Ladestopp 1", "distance_km": 154.27, "...": "..." }
  ]
}
```

---

## iOS / Swift integration

### Codable models

```swift
import Foundation
import CoreLocation

// MARK: - Request

struct PlanRouteRequest: Codable {
    let start_lat: Double
    let start_lon: Double
    let dest_lat: Double
    let dest_lon: Double
    let vehicle_id: String
    let initial_soc: Double           // 0.01 ... 1.0
    let target_soc: Double?
    let window_start_soc: Double?
    let window_end_soc: Double?
    let price_time_weight: Double?
    let consumption_factor: Double?
    let speed_factor: Double?
    let degradation: Double?
}

// MARK: - Response

struct PlanRouteResponse: Codable {
    let summary: Summary
    let chargers: [Charger]
    let route_geometry: [RouteLeg]
    let navigation: [NavigationLeg]
    let leg_metrics: [LegMetric]
}

struct Summary: Codable {
    let vehicle: String
    let total_distance_km: Double
    let drive_time_min: Double
    let charge_time_min: Double
    let total_trip_time_min: Double
    let soc_at_destination_pct: Double
    let num_stops: Int
    let remaining_leg_distance_km: Double
    let trip_energy_kwh: Double
}

enum PriceSource: String, Codable {
    case chargeindexLive  = "chargeindex_live"
    case manualOverride   = "manual_override"
    case defaultFallback  = "default_fallback"

    /// Show the user a "geschätzt"/estimate badge when this is true.
    var isEstimated: Bool { self == .defaultFallback }
}

struct ChargerAlternative: Codable, Identifiable {
    let rank: Int
    let is_chosen: Bool
    let operator_name: String
    let max_power_kw: Double
    let lat: Double
    let lon: Double
    let city: String?
    let source_id: String?
    let detour_min: Double
    let charge_time_min: Double
    let kwh_to_charge: Double
    let soc_at_arrival_pct: Double
    let soc_after_charge_pct: Double
    let estimated_cost: Double
    let price_per_kwh_used: Double
    let price_source: PriceSource
    let score: Double

    var id: String { source_id ?? "\(lat),\(lon)" }
    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

struct Charger: Codable, Identifiable {
    var id: String { source_id ?? "\(lat),\(lon)" }
    let operator_name: String
    let max_power_kw: Double
    let effective_charge_kw: Double
    let lat: Double
    let lon: Double
    let source_id: String?
    let charged_kwh: Double
    let charge_time_min: Double
    let soc_at_arrival_pct: Double
    let soc_after_charge_pct: Double
    let detour_min: Double
    let estimated_cost: Double
    let price_per_kwh_used: Double
    let price_source: PriceSource
    let top_5_alternatives: [ChargerAlternative]

    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: lat, longitude: lon)
    }
}

struct ElevationStats: Codable {
    let elevation_gain_m: Double
    let elevation_loss_m: Double
    let net_elevation_m: Double
}

struct RouteLeg: Codable {
    let leg_index: Int
    let distance_km: Double
    let drive_time_min: Double
    let energy_usage_kwh100: Double
    let elevation_gain_m: Double
    let elevation_loss_m: Double
    let net_elevation_m: Double
    let encoded_shape: String
    let decoded_shape: [[Double]]   // [[lat, lon], ...]

    var coordinates: [CLLocationCoordinate2D] {
        decoded_shape.compactMap { pair in
            guard pair.count == 2 else { return nil }
            return CLLocationCoordinate2D(latitude: pair[0], longitude: pair[1])
        }
    }
}

struct Maneuver: Codable {
    let distance_km: Double
    let instruction: String
}

struct NavigationLeg: Codable {
    let leg_index: Int
    let title: String
    let distance_km: Double
    let drive_time_min: Double
    let energy_usage_kwh100: Double
    let elevation_gain_m: Double
    let elevation_loss_m: Double
    let net_elevation_m: Double
    let maneuvers: [Maneuver]
}

struct LegMetric: Codable {
    let leg_index: Int
    let title: String
    let distance_km: Double
    let drive_time_min: Double
    let energy_usage_kwh100: Double
    let elevation_gain_m: Double
    let elevation_loss_m: Double
    let net_elevation_m: Double
}
```

### URLSession client

```swift
struct PlanRouteAPIError: Error {
    let status: Int
    let detail: String
}

actor PlanRouteAPI {
    let baseURL: URL                  // e.g. URL(string: "http://localhost:8000")!
    private let session: URLSession = .shared
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    func planRoute(_ body: PlanRouteRequest) async throws -> PlanRouteResponse {
        var request = URLRequest(url: baseURL.appendingPathComponent("plan-route"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try encoder.encode(body)
        // Long-running call — server does multiple Valhalla + ChargeIndex hops.
        request.timeoutInterval = 60

        let (data, resp) = try await session.data(for: request)
        guard let http = resp as? HTTPURLResponse else {
            throw PlanRouteAPIError(status: -1, detail: "no HTTP response")
        }
        guard http.statusCode == 200 else {
            // Try to parse FastAPI's { "detail": "..." }
            let detail = (try? JSONDecoder().decode(
                [String: String].self, from: data
            ))?["detail"] ?? String(data: data, encoding: .utf8) ?? ""
            throw PlanRouteAPIError(status: http.statusCode, detail: detail)
        }
        return try decoder.decode(PlanRouteResponse.self, from: data)
    }
}
```

### MapKit polyline rendering

```swift
import MapKit

extension RouteLeg {
    func polyline() -> MKPolyline {
        var coords = coordinates
        return MKPolyline(coordinates: &coords, count: coords.count)
    }
}

// In your MKMapViewDelegate / SwiftUI Map view:
let polylines = response.route_geometry.map { $0.polyline() }
mapView.addOverlays(polylines)

// And one annotation per charger:
for charger in response.chargers {
    let pin = MKPointAnnotation()
    pin.coordinate = charger.coordinate
    pin.title = charger.operator_name
    pin.subtitle = "\(Int(charger.max_power_kw)) kW · \(String(format: "%.2f", charger.estimated_cost)) €"
    mapView.addAnnotation(pin)
}
```

---

## Implementation notes for the mobile dev

1. **Coordinate ordering is `[lat, lon]` in `decoded_shape`.** This is opposite
   to GeoJSON's `[lon, lat]`. If the mobile client also talks to the
   ChargeIndex API directly elsewhere, watch out — that one uses GeoJSON
   ordering.

2. **Polyline precision is 6, not 5.** Google Maps / Apple's standard polyline
   format is precision 5. Valhalla uses 6. If you decode `encoded_shape`
   yourself, divide the integer deltas by `1e6`, not `1e5`. Or ignore
   `encoded_shape` entirely and just consume `decoded_shape`.

3. **Round-trip latency.** A typical plan-route call for a 300 km route with 1
   stop takes 3–6 seconds (multiple Valhalla calls + ChargeIndex lookup +
   physics simulation). Show a progress UI; do not block the main thread.
   Consider a 60 s client timeout.

4. **Empty `chargers[]` is normal.** When `summary.num_stops == 0`, no charging
   is needed — the trip is one straight leg. UI should not show a "stops
   missing" error in that case.

5. **`price_per_kwh_used` is only trustworthy when `price_source == "chargeindex_live"`.**
   Use the `PriceSource.isEstimated` helper on the Swift model — when `true`,
   the price is the system default of **0.89 €/kWh** (no NAP data available
   for this operator/station). Surface this with a visible flag in the UI
   ("Preis geschätzt") so the user knows not to plan the household budget
   around it. The `"manual_override"` case is also a manual estimate
   maintained in the backend's `operator_prices.json`.

6. **Stable charger ID via `source_id`.** Use `source_id` (the ChargeIndex
   NAP identifier) as the primary key for favourites, selection state, or
   cross-referencing across re-plans. It's stable per station. Falls back to
   `"lat,lon"` only if the API didn't return one — should be rare.

7. **All language strings are German.** `summary.vehicle`,
   `navigation[*].title`, and every `maneuvers[*].instruction` are German
   strings emitted by Valhalla and the backend. If you need other locales,
   that's a backend change (Valhalla supports `language` parameter).

8. **Errors are user-presentable in German.** The `detail` field on a 400
   often reads `"Kein valides Lade-Fenster auf der Route gefunden."` or
   `"Kein geeigneter Ladestopp im Lade-Fenster gefunden."` — these can be
   surfaced directly to the user.

9. **`initial_soc` is the only validated numeric.** Other ratios (`target_soc`
   etc.) accept any double. Sanity-check on the client before posting.

10. **Geographic coverage.** Charger lookup currently uses the ChargeIndex
    API, which today serves Austria (AT) live, Germany (DE) pipeline. Routes
    in countries outside coverage will fail with "Kein geeigneter Ladestopp"
    if a charging stop is needed. Routes that don't need a stop work
    everywhere Valhalla covers.

11. **Top-5 alternatives are ranked but not re-routed.** The `top_5_alternatives`
    array is sorted by the planner's pain-score for that window. The numbers
    on each alternative come from a cheap distance/energy estimate, not a
    full Valhalla round-trip, so they are within a few percent of reality
    but not byte-identical to the top-level fields on the chosen charger.
    Good enough for an "alternatives" picker; if the user actually swaps to
    an alternative, re-issue `/plan-route` with that charger forced (not yet
    supported — would need a backend feature flag).
