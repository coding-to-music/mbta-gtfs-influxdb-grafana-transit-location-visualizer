import requests
import json
import sys
from datetime import datetime
import os

# Configuration - Access secrets via environment variables
MBTA_API_KEY = os.getenv('MBTA_API_KEY', '')  # Optional
INFLUX_URL = os.getenv('INFLUX_URL')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN')
INFLUX_ORG = os.getenv('INFLUX_ORG')
INFLUX_BUCKET = os.getenv('INFLUX_BUCKET')

# MBTA Vehicles API endpoint
MBTA_URL = "https://api-v3.mbta.com/vehicles?include=stop,trip&sort=updated_at"
if MBTA_API_KEY:
    MBTA_URL += f"&api_key={MBTA_API_KEY}"

print("Fetching MBTA vehicle locations...")

# 1. Fetch MBTA vehicle data
response = requests.get(MBTA_URL)
if response.status_code != 200:
    print(f"✗ API error: {response.status_code} - {response.text}")
    sys.exit(1)

data = response.json()
vehicles = data.get('data', [])
included = data.get('included', [])

if not vehicles:
    print("✗ No vehicles found")
    sys.exit(1)

print(f"✓ Found {len(vehicles)} vehicles")

# 2. Build lookup maps for stops and trips
stops = {item['id']: item['attributes']['name'] for item in included if item['type'] == 'stop'}
trips = {item['id']: item['attributes']['headsign'] for item in included if item['type'] == 'trip'}

# 3. Transform to InfluxDB Line Protocol
line_protocol_lines = []
skipped_vehicles = []
for vehicle in vehicles:
    attrs = vehicle['attributes']
    rels = vehicle.get('relationships', {})
    
    vehicle_id = vehicle['id']
    
    # Validate relationships
    route_id = rels.get('route', {}).get('data', {}).get('id', 'unknown')
    trip_id = rels.get('trip', {}).get('data', {}).get('id', 'unknown')
    stop_id = rels.get('stop', {}).get('data', {}).get('id', 'unknown')
    
    # Skip if critical relationships are missing
    if route_id == 'unknown' and trip_id == 'unknown' and stop_id == 'unknown':
        skipped_vehicles.append(f"Vehicle {vehicle_id}: Missing route, trip, and stop relationships")
        continue
    
    direction_id = str(attrs.get('direction_id', ''))  # Ensure string
    current_status = attrs.get('current_status', 'unknown')
    stop_name = stops.get(stop_id, 'unknown')
    headsign = trips.get(trip_id, 'unknown')
    
    # Use updated_at as timestamp (convert to Unix seconds)
    updated_at_str = attrs.get('updated_at')
    if updated_at_str:
        updated_at = int(datetime.fromisoformat(updated_at_str.rstrip('Z')).timestamp())
    else:
        updated_at = int(datetime.utcnow().timestamp())
    
    # Tags (escaped for special characters)
    tags = (
        f"id={vehicle_id},"
        f"route_id={route_id},"
        f"trip_id={trip_id},"
        f"stop_id={stop_id},"
        f"direction_id={direction_id},"
        f"current_status={current_status},"
        f"stop_name=\"{stop_name.replace('\"', '\\\"').replace(',', '\\,')}\","
        f"headsign=\"{headsign.replace('\"', '\\\"').replace(',', '\\,')}\""
    )
    
    # Fields: Only include non-None values
    fields_list = []
    if attrs.get('latitude') is not None:
        fields_list.append(f"latitude={attrs['latitude']}")
    if attrs.get('longitude') is not None:
        fields_list.append(f"longitude={attrs['longitude']}")
    if attrs.get('bearing') is not None:
        fields_list.append(f"bearing={attrs['bearing']}")
    if attrs.get('speed') is not None:
        fields_list.append(f"speed={attrs['speed']}")
    if attrs.get('position_latency') is not None:
        fields_list.append(f"position_latency={attrs['position_latency']}")
    
    # Only create line if there are valid fields
    if fields_list:
        fields = ",".join(fields_list)
        line = f"mbta_vehicle,{tags} {fields} {updated_at}"  # Space between tags and fields
        line_protocol_lines.append(line)
    else:
        skipped_vehicles.append(f"Vehicle {vehicle_id}: No valid fields to write")

# Log skipped vehicles
if skipped_vehicles:
    print(f"Skipped {len(skipped_vehicles)} vehicles:")
    for skip_msg in skipped_vehicles[:5]:  # Limit to 5 for brevity
        print(skip_msg)

# If no valid lines, exit early
if not line_protocol_lines:
    print("✗ No valid data to write to InfluxDB")
    sys.exit(1)

line_protocol = '\n'.join(line_protocol_lines)

# Debug: Print Line Protocol for inspection (first 5 lines)
print("Line Protocol (first 5 lines):")
for line in line_protocol_lines[:5]:
    print(line)

# 4. Write to InfluxDB Cloud
write_url = f"{INFLUX_URL}/api/v2/write?org={INFLUX_ORG}&bucket={INFLUX_BUCKET}&precision=s"
headers = {
    "Authorization": f"Token {INFLUX_TOKEN}",
    "Content-Type": "text/plain; charset=utf-8"
}
write_response = requests.post(write_url, headers=headers, data=line_protocol)

if write_response.status_code == 204:
    print(f"✓ Successfully wrote {len(line_protocol_lines)} vehicle locations to InfluxDB")
else:
    print(f"✗ Failed to write to InfluxDB: {write_response.status_code} - {write_response.text}")
    sys.exit(1)