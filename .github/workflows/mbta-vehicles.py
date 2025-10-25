import requests
import json
import sys
from datetime import datetime
import os

# Configuration - These will come from environment variables (GitHub Secrets)
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
for vehicle in vehicles:
    attrs = vehicle['attributes']
    rels = vehicle['relationships']
    
    vehicle_id = vehicle['id']
    route_id = rels['route']['data']['id'] if rels.get('route') and rels['route'].get('data') else 'unknown'
    trip_id = rels['trip']['data']['id'] if rels.get('trip') and rels['trip'].get('data') else 'unknown'
    stop_id = rels['stop']['data']['id'] if rels.get('stop') and rels['stop'].get('data') else 'unknown'
    direction_id = attrs.get('direction_id', '')
    current_status = attrs.get('current_status', 'unknown')
    
    stop_name = stops.get(stop_id, 'unknown')
    headsign = trips.get(trip_id, 'unknown')
    
    # Use updated_at as timestamp (convert to Unix seconds)
    updated_at_str = attrs.get('updated_at')
    if updated_at_str:
        updated_at = int(datetime.fromisoformat(updated_at_str.rstrip('Z')).timestamp())
    else:
        updated_at = int(datetime.utcnow().timestamp())
    
    # Tags (escaped if needed)
    tags = (
        f"id={vehicle_id},"
        f"route_id={route_id},"
        f"trip_id={trip_id},"
        f"stop_id={stop_id},"
        f"direction_id={direction_id},"
        f"current_status={current_status},"
        f"stop_name=\"{stop_name.replace('\"', '\\\"')}\","
        f"headsign=\"{headsign.replace('\"', '\\\"')}\""
    )
    
    # Fields
    fields = (
        f"latitude={attrs.get('latitude', 0.0)},"
        f"longitude={attrs.get('longitude', 0.0)},"
        f"bearing={attrs.get('bearing', 0)},"
        f"speed={attrs.get('speed', 0)},"
        f"position_latency={attrs.get('position_latency', 0)}"
    )
    
    line = f"mbta_vehicle,{tags} {fields} {updated_at}"
    line_protocol_lines.append(line)

line_protocol = '\n'.join(line_protocol_lines)

# 4. Write to InfluxDB Cloud
write_url = f"{INFLUX_URL}/api/v2/write?org={INFLUX_ORG}&bucket={INFLUX_BUCKET}&precision=s"
headers = {
    "Authorization": f"Token {INFLUX_TOKEN}",
    "Content-Type": "text/plain; charset=utf-8"
}
write_response = requests.post(write_url, headers=headers, data=line_protocol)

if write_response.status_code == 204:
    print(f"✓ Successfully wrote {len(vehicles)} vehicle locations to InfluxDB")
else:
    print(f"✗ Failed to write to InfluxDB: {write_response.status_code} - {write_response.text}")
    sys.exit(1)