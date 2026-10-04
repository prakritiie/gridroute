-- Cleaning rules (spec §5.2), applied in order to one month in table `t`.
-- data/clean.py runs each "-- rule:" block as its own statement and logs how many rows it removed,
-- so every count in the data card traces back to exactly one rule here.
-- Rule 3 uses straight-line distance x 1.3; it switches to OSRM road distance once those are precomputed.

-- rule: zero_or_null_coords
-- Rule 5a. Coordinates of exactly 0 (or missing) are meter glitches, not trips at Null Island.
DELETE FROM t
WHERE pickup_lat IS NULL OR pickup_lng IS NULL OR dropoff_lat IS NULL OR dropoff_lng IS NULL
   OR pickup_lat = 0 OR pickup_lng = 0 OR dropoff_lat = 0 OR dropoff_lng = 0;

-- rule: outside_nyc_bbox
-- Rule 1. Both ends inside the service area (lat 40.47-40.92, lng -74.27 to -73.68).
DELETE FROM t
WHERE NOT (pickup_lat  BETWEEN 40.47 AND 40.92 AND pickup_lng  BETWEEN -74.27 AND -73.68
       AND dropoff_lat BETWEEN 40.47 AND 40.92 AND dropoff_lng BETWEEN -74.27 AND -73.68);

-- rule: duration_out_of_range
-- Rule 2. Between 60 s and 3 h. Negative and zero durations land here too.
DELETE FROM t
WHERE pickup_ts IS NULL OR dropoff_ts IS NULL
   OR date_diff('second', pickup_ts, dropoff_ts) NOT BETWEEN 60 AND 10800;

-- rule: too_short
-- Rule 4. Straight-line distance >= 300 m; shorter trips are mostly meter errors.
DELETE FROM t
WHERE 2 * 6371000 * asin(sqrt(
        pow(sin(radians(dropoff_lat - pickup_lat) / 2), 2)
      + cos(radians(pickup_lat)) * cos(radians(dropoff_lat)) * pow(sin(radians(dropoff_lng - pickup_lng) / 2), 2)
      )) < 300;

-- rule: implausible_speed
-- Rule 3. Implied speed between 1 and 100 km/h over straight-line x 1.3.
DELETE FROM t
WHERE (1.3 * 2 * 6371000 * asin(sqrt(
        pow(sin(radians(dropoff_lat - pickup_lat) / 2), 2)
      + cos(radians(pickup_lat)) * cos(radians(dropoff_lat)) * pow(sin(radians(dropoff_lng - pickup_lng) / 2), 2)
      )) / 1000) / (date_diff('second', pickup_ts, dropoff_ts) / 3600.0) NOT BETWEEN 1 AND 100;

-- rule: exact_duplicates
-- Rule 5b. Identical rows (same timestamps and coordinates to the last digit) counted once.
CREATE OR REPLACE TEMP TABLE t AS SELECT DISTINCT * FROM t;
